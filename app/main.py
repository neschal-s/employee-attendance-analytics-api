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
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
import re
from datetime import date
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from decimal import Decimal, ROUND_HALF_UP
from calendar import monthrange
import json
from bson import json_util


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

    
    # Support headcount queries filtered by employee join date.
    db.employees.create_index(
        [("joined_on", 1)],
        name="idx_employees_joined_on",
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

def round_half_up(value, places=2):
    """Round a numeric value using the assignment's HALF_UP rule."""
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )



def count_working_days(start_date, end_date):
    """Count weekdays inclusively, Monday through Friday."""
    days = 0
    current = start_date

    while current <= end_date:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)

    return days



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





class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None

    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(
        cls, value: Optional[int]
    ) -> Optional[int]:
        if value is None:
            return value

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                "Timestamp must be an epoch-millisecond integer"
            )

        if value < 100_000_000_000:
            raise ValueError(
                "Timestamp must be an epoch-millisecond timestamp"
            )

        try:
            datetime.fromtimestamp(value // 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError(
                "Timestamp is outside the supported range"
            )

        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Reason must contain at least 5 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("regularized_by cannot be blank")
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date filters
    try:
        if date_from:
            date.fromisoformat(date_from)
        if date_to:
            date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    # Build MongoDB filters
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        q["date"] = date_filter

    if status:
        q["status"] = status

    # Count all records matching the filters
    total = db.attendance_logs.count_documents(q)

    # Filter, sort and paginate inside MongoDB
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = list(cursor)

    # Convert BSON datetimes to epoch milliseconds for the API
    for item in items:
        for field in ("punch_in", "punch_out"):
            value = item.get(field)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                item[field] = int(value.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




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









@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    body: RegularizeRequest,
    date_str: str = Path(alias="date"),
):

    # Validate the employee code and canonical YYYY-MM-DD date.
    if not re.fullmatch(r"EMP\d{4,6}", emp_code):
        raise HTTPException(status_code=422, detail="Invalid employee code")

    try:
        attendance_date = date.fromisoformat(date_str)
        if attendance_date.isoformat() != date_str:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format",
        )

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date_str}
    )
    if not record:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    # Only explicitly supplied fields may be changed.
    supplied = body.model_fields_set
    if not supplied.intersection({"status", "punch_in", "punch_out"}):
        raise HTTPException(
            status_code=422,
            detail="Request must change at least one attendance field",
        )

    old_status = record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    # Normalize stored BSON datetimes to UTC and whole seconds.
    def normalize_dt(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0)

    old_in = normalize_dt(old_in)
    old_out = normalize_dt(old_out)

    new_status = body.status if "status" in supplied else old_status
    new_in = (
        datetime.fromtimestamp(body.punch_in // 1000, timezone.utc)
        if "punch_in" in supplied and body.punch_in is not None
        else old_in
    )
    new_out = (
        datetime.fromtimestamp(body.punch_out // 1000, timezone.utc)
        if "punch_out" in supplied and body.punch_out is not None
        else old_out
    )

    # Explicit null timestamps are not accepted as a way to clear a punch.
    # ABSENT and LEAVE clear both timestamps automatically.
    
    if (
        ("punch_in" in supplied and body.punch_in is None)
        or ("punch_out" in supplied and body.punch_out is None)
    ):
        raise HTTPException(
            status_code=422,
            detail="punch_in and punch_out cannot be null when supplied",
        )

    if new_status in ("ABSENT", "LEAVE"):
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times with ABSENT or LEAVE",
            )
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        # The punch-in must fall on the record's IST attendance date.
        if new_in.astimezone(IST).date() != attendance_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date in IST",
            )

        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    # Recompute derived fields from the final status and punch times.
    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}
    if new_status in presence_statuses and new_in is not None:
        punch_in_ist = new_in.astimezone(IST)
        shift_start_hour, shift_start_minute = map(
            int, employee["shift_start"].split(":")
        )
        shift_start = punch_in_ist.replace(
            hour=shift_start_hour,
            minute=shift_start_minute,
            second=0,
            microsecond=0,
        )
        late_seconds = (punch_in_ist - shift_start).total_seconds()
        late_minutes = (
            int(late_seconds // 60) if late_seconds > 600 else 0
        )
    else:
        late_minutes = 0

    work_hours = None
    overtime_minutes = 0
    half_day = False

    if new_in is not None and new_out is not None:
        elapsed_seconds = (new_out - new_in).total_seconds()
        work_hours = float(
            (
                Decimal(str(elapsed_seconds)) / Decimal("3600")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        half_day = work_hours < 4.50

        shift_end_hour, shift_end_minute = map(
            int, employee["shift_end"].split(":")
        )
        shift_start_hour, shift_start_minute = map(
            int, employee["shift_start"].split(":")
        )
        shift_end = punch_in_ist.replace(
            hour=shift_end_hour,
            minute=shift_end_minute,
            second=0,
            microsecond=0,
        )

        if (shift_end_hour, shift_end_minute) <= (
            shift_start_hour, shift_start_minute
        ):
            shift_end += timedelta(days=1)
        if shift_end <= punch_in_ist:
            shift_end += timedelta(days=1)

        overtime_seconds = (
            new_out - shift_end.astimezone(timezone.utc)
        ).total_seconds()
        if overtime_seconds >= 1800:
            overtime_minutes = int(overtime_seconds // 60)

    old_derived = {
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_derived = {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # Construct history with only fields whose values actually changed.
    def epoch_ms(value):
        return int(value.timestamp() * 1000) if value is not None else None

    changes = {}

    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_in != new_in:
        changes["punch_in"] = {
            "from": epoch_ms(old_in),
            "to": epoch_ms(new_in),
        }
    if old_out != new_out:
        changes["punch_out"] = {
            "from": epoch_ms(old_out),
            "to": epoch_ms(new_out),
        }

    for field, new_value in new_derived.items():
        old_value = old_derived[field]
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change the attendance record",
        )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare the original editable fields and history to detect races.
    original_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "history": record.get("history", []),
    }

    result = db.attendance_logs.update_one(
        original_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                **new_derived,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": epoch_ms(normalize_dt(updated.get("punch_in"))),
        "punch_out": epoch_ms(normalize_dt(updated.get("punch_out"))),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": [
            {
                **entry,
                "at": epoch_ms(normalize_dt(entry["at"])),
            }
            for entry in updated.get("history", [])
        ],
    }























@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    # Verify the employee exists and retrieve their joining date.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1)
    month_end = date(year, month_number, monthrange(year, month_number)[1])

    # Working days begin on the later of month_start and joined_on.
    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start, joined_on)

    # Generate the weekday count using MongoDB's date operators.

    # Count weekdays inclusively from the effective start through month end.
    working_days = 0

    if effective_start <= month_end:
        working_days_result = list(db.employees.aggregate([
            {"$match": {"emp_code": emp_code}},
            {
                "$project": {
                    "days": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    (month_end - effective_start).days + 1,
                                ]
                            },
                            "as": "offset",
                            "in": {
                                "$dayOfWeek": {
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {
                                                    "dateString": effective_start.isoformat()
                                                }
                                            },
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                    "timezone": "UTC",
                                }
                            },
                        }
                    }
                }
            },
            {
                "$project": {
                    "working_days": {
                        "$size": {
                            "$filter": {
                                "input": "$days",
                                "as": "weekday",
                                "cond": {
                                    "$in": ["$$weekday", [2, 3, 4, 5, 6]]
                                },
                            }
                        }
                    }
                }
            },
        ]))

        if working_days_result:
            working_days = working_days_result[0]["working_days"]


    # Aggregate attendance metrics from stored derived values.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                    }
                                                }
                                            },
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    metrics = results[0] if results else {}

    present_days = metrics.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days * 100 / working_days, 2)
        if working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }





@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1)
    month_end = date(year, month_number, monthrange(year, month_number)[1])

    pipeline = [
        # Only employees who joined on or before the end of the month.
        {
            "$match": {
                "joined_on": {"$lte": month_end.isoformat()},
                **({"department": department} if department is not None else {}),
            }
        },
        # Join each employee's attendance records for the requested month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Calculate employee-level metrics while preserving employees with no logs.
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "logs": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "work_hours_values": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$$log.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$$log.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        # Group employee metrics by department.
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_values": {"$push": "$work_hours_values"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        # Flatten work-hour arrays and calculate the record-level average.
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "work_hours_values": {
                    "$reduce": {
                        "input": "$work_hours_values",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_values"}, 0]},
                        {"$avg": "$work_hours_values"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        item["present_days"] = round_half_up(item["present_days"], 2)

    return {"month": month, "items": items}






@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1).isoformat()
    month_end = date(
        year,
        month_number,
        monthrange(year, month_number)[1],
    ).isoformat()

    pipeline = [
        # Only include employees who exist, and apply department filtering
        # before ranking so ranks are calculated within that department.
        *(
            [{"$match": {"department": department}}]
            if department is not None
            else []
        ),
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "late_stats",
            }
        },
        {
            "$set": {
                "total_late_minutes": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.total_late_minutes", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        # Employees with zero late minutes are not on the leaderboard.
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Competition ranking: tied totals share a rank, and ranks are skipped.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        # Apply the limit after ranking to preserve ties at the cutoff.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}




def build_department_trend_pipeline(department, date_from, date_to):
    return [
        {
            "$documents": [
                {"date": (date_from + timedelta(days=i)).isoformat()}
                for i in range((date_to - date_from).days + 1)
            ]
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_data",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]},
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_data",
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$headcount_data.count", 0]}, 0]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_data.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_data.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {"$round": ["$attendance_rate", 4]},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]



@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str,
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    if date_to < date_from:
        raise HTTPException(
            status_code=422,
            detail="date_to must be on or after date_from",
        )

    if (date_to - date_from).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )

    employee_exists = db.employees.find_one(
        {"department": department},
        {"_id": 1},
    )

    if not employee_exists:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )


    start_date = date_from.isoformat()
    end_date = date_to.isoformat()


    pipeline = build_department_trend_pipeline(
        department, date_from, date_to
    )

    

    results = list(db.command({
        "aggregate": 1,
        "pipeline": pipeline,
        "cursor": {},
    })["cursor"]["firstBatch"])

    for item in results:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(
                item["attendance_rate"], 4
            )

        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(
                item["moving_avg_7d"], 4
            )

    return {
        "department": department,
        "items": results,
    }


    return {
        "department": department,
        "items": results,
    }








@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )
        if to < from_:
            raise HTTPException(
                status_code=422,
                detail="to must be on or after from",
            )
        if (to - from_).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range must not exceed 92 days",
            )


    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from:
            query["date"] = {"$gte": date_from.isoformat()}

        if date_to:
            query.setdefault("date", {}).update(
                {"$lte": date_to.isoformat()}
            )

        if status:
            query["status"] = status

        cursor = (
            db.attendance_logs
            .find(query, {"_id": 0})
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "projection": {"_id": 0},
            },
            "verbosity": "executionStats",
        })


        explain_json = json.loads(json_util.dumps(explain))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }


    
    if endpoint == "employee_monthly":
        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )

        if not employee:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1)
        month_end = date(
            year,
            month_number,
            monthrange(year, month_number)[1],
        )

        joined_on = date.fromisoformat(employee["joined_on"])
        effective_start = max(month_start, joined_on)

        working_days_pipeline = [
            {"$match": {"emp_code": emp_code}},
            {
                "$project": {
                    "days": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    max(
                                        0,
                                        (month_end - effective_start).days + 1,
                                    ),
                                ]
                            },
                            "as": "offset",
                            "in": {
                                "$dayOfWeek": {
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {
                                                    "dateString": effective_start.isoformat()
                                                }
                                            },
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                    "timezone": "UTC",
                                }
                            },
                        }
                    }
                }
            },
            {
                "$project": {
                    "working_days": {
                        "$size": {
                            "$filter": {
                                "input": "$days",
                                "as": "weekday",
                                "cond": {
                                    "$in": ["$$weekday", [2, 3, 4, 5, 6]]
                                },
                            }
                        }
                    }
                }
            },
        ]

        attendance_pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$gte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                2,
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                6,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": attendance_pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        explain_json = json.loads(json_util.dumps(explain))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json,
        }




    
    if endpoint == "department_summary":
        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1)
        month_end = date(
            year,
            month_number,
            monthrange(year, month_number)[1],
        )

        pipeline = [
            {
                "$match": {
                    "joined_on": {"$lte": month_end.isoformat()},
                    **(
                        {"department": department}
                        if department is not None
                        else {}
                    ),
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code",
                                            ]
                                        },
                                        {
                                            "$gte": [
                                                "$date",
                                                month_start.isoformat(),
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                "$date",
                                                month_end.isoformat(),
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "logs": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "log",
                                        "cond": {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$log.status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$log.date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$log.date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                            ]
                                        },
                                    }
                                },
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$log.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                            }
                        }
                    },
                    "work_hours_values": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    [
                                                        "PRESENT",
                                                        "WFH",
                                                        "ON_DUTY",
                                                    ],
                                                ]
                                            },
                                            {
                                                "$ne": [
                                                    "$$log.work_hours",
                                                    None,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {
                                            "$ifNull": [
                                                "$$log.late_minutes",
                                                0,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {
                                    "$ifNull": [
                                        "$$log.late_minutes",
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$eq": ["$$log.status", "LEAVE"]
                                },
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$eq": ["$$log.status", "ON_DUTY"]
                                },
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "work_hours_values": {
                        "$push": "$work_hours_values"
                    },
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {
                        "$sum": "$total_late_minutes"
                    },
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "work_hours_values": {
                        "$reduce": {
                            "input": "$work_hours_values",
                            "initialValue": [],
                            "in": {
                                "$concatArrays": ["$$value", "$$this"]
                            },
                        }
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {
                "$project": {
                    "department": 1,
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$size": "$work_hours_values"},
                                    0,
                                ]
                            },
                            {"$avg": "$work_hours_values"},
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        explain_json = json.loads(json_util.dumps(explain))

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json,
        }



    
    if endpoint == "late_leaderboard":
        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1).isoformat()
        month_end = date(
            year,
            month_number,
            monthrange(year, month_number)[1],
        ).isoformat()

        pipeline = [
            *(
                [{"$match": {"department": department}}]
                if department is not None
                else []
            ),
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "total_late_minutes": {
                                    "$sum": {
                                        "$ifNull": ["$late_minutes", 0]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$gt": [
                                                    {
                                                        "$ifNull": [
                                                            "$late_minutes",
                                                            0,
                                                        ]
                                                    },
                                                    0,
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "late_stats",
                }
            },
            {
                "$set": {
                    "total_late_minutes": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$late_stats.total_late_minutes",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                    "late_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$late_stats.late_count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        explain_json = json.loads(json_util.dumps(explain))

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json,
        }




    
    if endpoint == "department_trend":
        start_date = from_.isoformat()
        end_date = to.isoformat()


        pipeline = build_department_trend_pipeline(
            department, from_, to
        )
       
                
        explain = db.command({
            "aggregate": 1,
            "pipeline": pipeline,
            "cursor": {},
            "explain": True,
        })

        
        explain_json = json.loads(json_util.dumps(explain))



        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json,
        }


    raise HTTPException(
        status_code=501,
        detail="Explain query implementation is not added yet",
    )





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
