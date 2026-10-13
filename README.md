# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, attendance regularization, and workforce analytics, built with **FastAPI, Python, and MongoDB**.

## Features

- Employee creation and paginated employee listing.
- Attendance punch-in and punch-out with IST and overnight-shift handling.
- Attendance filtering, pagination, and manual regularization with audit history.
- Monthly employee and department analytics.
- Late-arrival leaderboard with competition ranking.
- Department attendance trends with daily gap filling and moving averages.
- MongoDB indexes and explain execution statistics.
- Automated calculation and API tests.

## Tech Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+
- Pydantic
- Pytest

## Prerequisites

- Python 3.11 or newer
- MongoDB Atlas account or a compatible MongoDB instance
- Git

## Setup

Create and activate a virtual environment on Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file in the project root:

```env
MONGO_URI=your_mongodb_connection_string
MONGO_DB=attendance_db
```

Replace the placeholder with your MongoDB connection string. Never commit `.env` or expose database credentials.

## Run the API

```powershell
uvicorn app.main:app --port 8000 --reload
```

Check the health endpoint:

`http://127.0.0.1:8000/health`

Open the interactive API documentation:

`http://127.0.0.1:8000/docs`

## Run Tests

Run calculation tests:

```powershell
pytest tests/test_calculations.py -v
```

Run the complete test suite:

```powershell
pytest tests/ -v
```

Tests that require a reachable MongoDB instance must be run with a correctly configured database. Check skipped tests separately; skipped tests do not count as passing tests.

## Configuration

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. A local `.env` file can be used for development. Real environment variables should take precedence.

## API Overview

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Database readiness |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List and filter attendance |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily department trend |
| GET | `/admin/explain/{endpoint}` | Query execution statistics |

See the interactive Swagger documentation at `/docs` for request parameters and schemas.

## Important Business Rules

- Attendance dates and shift times follow IST.
- API timestamps use epoch milliseconds; MongoDB stores instants as BSON datetimes.
- Attendance calculations follow the assignment's R1–R10 rules.
- Analytics use stored derived attendance fields and MongoDB aggregation pipelines.
- Attendance history records manual corrections.

## Limitations

MongoDB must be reachable for database-backed endpoints and integration tests. Verify the results of all tests against the configured database before submission.