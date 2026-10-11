"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date,datetime, timedelta, timezone
from typing import Optional,Literal
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
import re
from datetime import date
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from decimal import Decimal, ROUND_HALF_UP



load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



# --------------------------------------------------------------------------- #
# MongoDB indexes and application lifecycle
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes when the application starts.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )

    # Support employee filtering by department and sorted employee codes.
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="idx_employees_department_emp_code",
    )

    # Support attendance date-range queries and descending date sorting.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_emp_code",
    )

    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)




# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    # Convert punch-in to IST before comparing local shift times.
    punch_time = punch_in.astimezone(IST)

    start = punch_time.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
    )

    elapsed_seconds = (punch_time - start).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)

    # Late only when strictly more than 10 minutes late.
    return elapsed_minutes if elapsed_seconds > 600 else 0




def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))




# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="forbid")
    
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email_length(cls, value: str) -> str:
        if len(value) > 120:
            raise ValueError("email must be at most 120 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must be in HH:MM format")
        return value


    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value



class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value

        if isinstance(value, bool) or value < 100_000_000_000:
            raise ValueError("punched_at must be an epoch-millisecond timestamp")

        try:
            datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError("punched_at is outside the supported timestamp range")

        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value

        if isinstance(value, bool) or value < 100_000_000_000:
            raise ValueError(
                "punched_at must be an epoch-millisecond timestamp"
            )

        try:
            datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError(
                "punched_at is outside the supported timestamp range"
            )

        return value

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #



@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    # Store created_at as a UTC timestamp.
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    return doc





@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Confirm that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Convert the supplied epoch milliseconds to UTC.
    # If omitted, use the current UTC time.
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at // 1000,
            tz=timezone.utc,
        )
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Determine the attendance date using IST.
    ts_ist = ts.astimezone(IST)
    date_str = ts_ist.date().isoformat()

    # ABSENT and LEAVE records must not have punch-in timestamps.
    if body.status in ("ABSENT", "LEAVE"):
        raise HTTPException(
            status_code=422,
            detail="ABSENT and LEAVE statuses cannot be punched in",
        )

    # Use the unique (emp_code, date) index for concurrency safety.
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc.pop("_id", None)

    # API contract excludes internal MongoDB IDs.
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(ts.timestamp() * 1000),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}





@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_out = datetime.fromtimestamp(
            body.punched_at // 1000,
            tz=timezone.utc,
        )
    else:
        punched_out = datetime.now(timezone.utc).replace(microsecond=0)

    punched_out_ist = punched_out.astimezone(IST)

    # Find the most recent open attendance record at or before punch-out.
    # This also allows overnight shifts to be closed the following day.
    open_record = db.attendance_logs.find_one(
    {
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None},
        "punch_out": None,
    },
    sort=[("punch_in", -1)],
    )
    

    if not open_record:
        closed_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_out},
            "punch_out": {"$ne": None},
        },
        sort=[("punch_in", -1)],
        )

        if closed_record:
            raise HTTPException(
                status_code=409,
                detail="Attendance record already punched out",
            )

        raise HTTPException(
            status_code=404,
            detail="No open attendance record found",
        )

    punch_in_time = open_record["punch_in"]

    # PyMongo normally returns naive UTC datetimes unless tz_aware=True.
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    punch_in_time = punch_in_time.replace(microsecond=0)
    punched_out = punched_out.replace(microsecond=0)

    elapsed_seconds = (punched_out - punch_in_time).total_seconds()

    if elapsed_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    # Round work hours to 2 decimal places using ROUND_HALF_UP.
    hours_decimal = (
        Decimal(str(elapsed_seconds)) / Decimal("3600")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    work_hours = float(hours_decimal)

    # Calculate the applicable shift-end instant.
    shift_end_hour, shift_end_minute = map(
        int, emp["shift_end"].split(":")
    )

    punch_in_ist = punch_in_time.astimezone(IST)
    shift_end = punch_in_ist.replace(
        hour=shift_end_hour,
        minute=shift_end_minute,
        second=0,
        microsecond=0,
    )

    # An overnight shift ends on the following calendar day.
    shift_start_hour, shift_start_minute = map(
        int, emp["shift_start"].split(":")
    )
    if (shift_end_hour, shift_end_minute) <= (
        shift_start_hour, shift_start_minute
    ):
        shift_end += timedelta(days=1)

    # If the punch-in is before the shift-end clock time on an
    # overnight shift, shift_end may need to be on the next day.
    if shift_end <= punch_in_ist:
        shift_end += timedelta(days=1)

    shift_end_utc = shift_end.astimezone(timezone.utc)
    overtime_seconds = (punched_out - shift_end_utc).total_seconds()

    # Overtime is counted only if the employee works at least 30
    # minutes beyond the shift end.
    if overtime_seconds >= 30 * 60:
        overtime_minutes = int(overtime_seconds // 60)
    else:
        overtime_minutes = 0

    half_day = work_hours < 4.50

    # Conditional update prevents two concurrent punch-outs from
    # both closing the same attendance record.
    result = db.attendance_logs.update_one(
        {
            "_id": open_record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out",
        )

    return {
        "emp_code": open_record["emp_code"],
        "date": open_record["date"],
        "status": open_record["status"],
        "punch_in": int(punch_in_time.timestamp() * 1000),
        "punch_out": int(punched_out.timestamp() * 1000),
        "work_hours": work_hours,
        "late_minutes": open_record.get("late_minutes", 0),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": open_record.get("history", []),
    }






# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
