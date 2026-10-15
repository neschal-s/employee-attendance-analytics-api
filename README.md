# Attendance Management API

A REST API built with **FastAPI** and **MongoDB** for employee management, daily attendance, shift-based attendance calculations, and attendance analytics.

## Features

- Employee creation and employee listing with filtering and pagination.
- Attendance punch-in and punch-out with duplicate and concurrency protection.
- Attendance listing with date, employee, and status filters.
- Attendance regularization with change history.
- Monthly employee attendance summaries.
- Department-level attendance summaries and headcount analytics.
- Late-arrival leaderboard with ranking and tie handling.
- Daily department attendance trends with moving averages.
- MongoDB query-plan inspection through an explain endpoint.
- Environment-based MongoDB configuration and indexes created at application startup.

## Requirements

- Python 3.10 or newer.
- MongoDB 6.0 or newer.
- Dependencies listed in `requirements.txt`.

## Setup

Clone the repository and navigate to the project directory.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, run the following in the current terminal and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## MongoDB Configuration

Set the following environment variables before starting the application:

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

For example, in Windows PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

For Linux/macOS:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

Ensure MongoDB is running and accessible before starting the API. For MongoDB Atlas, set `MONGO_URI` to your connection URI and keep credentials out of source control.

## Run the Application

From the repository root, run:

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

- **Swagger UI:** http://localhost:8000/docs
- **OpenAPI schema:** http://localhost:8000/openapi.json
- **Health endpoint:** http://localhost:8000/health

Check the health endpoint:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

A successful response is:

```json
{"status":"ok"}
```

## Database Indexes

The application creates its indexes at startup. These include unique indexes for employee codes and employee/date attendance records, along with indexes supporting employee filtering, join-date queries, and attendance-date queries.

## API Contract

Refer to `openapi.yaml` for the endpoint definitions and API contract. The implementation is contained in `app/main.py`.

## Assignment Documentation

- `REVIEW.md` — review of the original starter implementation and identified defects.
- `DECISIONS.md` — implementation and design decisions.
- `PROBLEM_STATEMENT.docx` — original assignment requirements.
- `DATA_MODEL.md` — MongoDB data model.
- `openapi.yaml` — API specification.

## Security

Do not commit `.env` files, MongoDB credentials, access tokens, or other secrets. Configure connection details through environment variables.