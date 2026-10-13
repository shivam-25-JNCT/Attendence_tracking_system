"""
Employee Attendance & Analytics API
Production-quality implementation matching openapi.yaml and DATA_MODEL.md.
"""
from __future__ import annotations

import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from bson import SON, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True,serverSelectionTimeoutMS=5000,)
try:
    print(client.admin.command("ping"))
    print("MongoDB connected successfully!")
except Exception as e:
    print(type(e).__name__, e)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes and Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes exist idempotently
    client.admin.command("ping")

    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Rounding and Time Utilities
# --------------------------------------------------------------------------- #
def validate_real_date(value: str) -> str:
    """Accept only a real ISO calendar date: YYYY-MM-DD."""
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Date must be a valid calendar date in YYYY-MM-DD format")
    return value

def validate_iso_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid calendar date: {value}",
        )
    return value

def round_half_up(val: Union[float, Decimal], decimals: int = 2) -> float:
    """R8: Standard HALF-UP rounding (not banker's rounding)."""
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Converts epoch ms to UTC datetime truncated to whole seconds (R1)."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Current UTC datetime truncated to whole seconds (R1)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def calculate_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is 10 minutes.
    A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start, not since grace ended.
    """
    h, m = map(int, shift_start.split(":"))
    start_dt = datetime.strptime(f"{attendance_date} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    d_hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(d_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    if shift_end <= shift_start:
        next_day = datetime.strptime(attendance_date, "%Y-%m-%d").date() + timedelta(days=1)
        end_dt = datetime.strptime(f"{next_day.isoformat()} {end_h:02d}:{end_m:02d}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    else:
        end_dt = datetime.strptime(f"{attendance_date} {end_h:02d}:{end_m:02d}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)

    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(...,min_length=6, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_employee_dates_and_shifts(self):
        validate_real_date(self.joined_on)
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePageResponse(BaseModel):
    items: List[EmployeeResponse]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntryResponse(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecordResponse(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntryResponse]


class AttendancePageResponse(BaseModel):
    items: List[AttendanceRecordResponse]
    total: int
    page: int
    page_size: int


def serialize_history(history_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    serialized = []
    for entry in history_list:
        changes_copy = {}
        for field, change in entry.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_epoch_ms(to_val)
            changes_copy[field] = {"from": from_val, "to": to_val}

        at_val = entry.get("at")
        if isinstance(at_val, datetime):
            at_val = dt_to_epoch_ms(at_val)

        serialized.append({
            "at": at_val,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes_copy
        })
    return serialized


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", []))
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"])
    }


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Builders (Shared between Endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> Tuple[str, str, int]:
    y, m = map(int, month_str.split("-"))
    days = calendar.monthrange(y, m)[1]
    return f"{month_str}-01", f"{month_str}-{days:02d}", days


def calculate_working_days(month_str: str, joined_on: str) -> int:
    """R7: Mon-Fri working days, counted from joined_on for mid-month joiners."""
    start_date_str, end_date_str, _ = get_month_range(month_str)
    if joined_on > end_date_str:
        return 0

    effective_start = max(start_date_str, joined_on)
    start_dt = datetime.strptime(effective_start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    working_days = 0
    curr = start_dt
    while curr <= end_dt:
        if curr.weekday() < 5:  # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = None
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status_filter:
        query["status"] = status_filter
    return query


def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "is_mon_fri": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]
                    ]
                }
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
                                    {"$eq": ["$is_mon_fri", True]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "all_emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                            "$logs.late_minutes",
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                            1,
                            0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$all_emps"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking: ties share rank, next rank skipped (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "date": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1]
                }
            }
        },
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": {"$first": "$date_dt"}
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"curr_date": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$curr_date"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "att"
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_dt": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6]
                    ]
                },
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$att.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$att.late_count"}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable"
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {"message": "Welcome to the Employee Attendance & Analytics API!"}

@app.post("/employees", status_code=status.HTTP_201_CREATED, response_model=EmployeeResponse)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    created_at = now_utc_truncated()
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists"
        )

    return serialize_employee_doc(doc)


@app.get("/employees", response_model=EmployeePageResponse)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query: Dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [serialize_employee_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, response_model=AttendanceRecordResponse)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    punch_in_dt = epoch_ms_to_dt(body.punched_at) if body.punched_at else now_utc_truncated()
    attendance_date = calculate_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, response_model=AttendanceRecordResponse)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    punch_out_dt = epoch_ms_to_dt(body.punched_at) if body.punched_at else now_utc_truncated()

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, 
         "punch_in": {"$lte": punch_out_dt},
         "punch_out": None
         },
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        existing = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_dt},
        },
        sort=[("punch_in", DESCENDING)],
    )
        if existing:
            raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No open attendance record found; the latest record may already be closed",
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate timing: punched_at <= punch_in or more than 24 hours after it -> 422
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid punch-out time: must be after punch-in and within 24 hours"
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = (work_hours < 4.50)

    # Atomic update to avoid race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    return serialize_attendance_doc(updated_doc)


@app.get("/attendance", response_model=AttendancePageResponse)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be after date_to"
        )

    query = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordResponse)
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    # Current values
    curr_status = rec["status"]
    curr_punch_in = rec.get("punch_in")
    curr_punch_out = rec.get("punch_out")
    if curr_punch_in and curr_punch_in.tzinfo is None:
        curr_punch_in = curr_punch_in.replace(tzinfo=timezone.utc)
    if curr_punch_out and curr_punch_out.tzinfo is None:
        curr_punch_out = curr_punch_out.replace(tzinfo=timezone.utc)

    curr_work_hours = rec.get("work_hours")
    curr_late_minutes = rec.get("late_minutes", 0)
    curr_overtime_minutes = rec.get("overtime_minutes", 0)
    curr_half_day = rec.get("half_day", False)

    # Target values calculation
    target_status = body.status if body.status is not None else curr_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Setting status ABSENT or LEAVE clears both punch times; cannot supply punch_in or punch_out"
            )
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status
        target_punch_in = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else curr_punch_in
        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires punch_in"
            )

        calc_date = calculate_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_param:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date"
            )

        target_punch_out = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else curr_punch_out
        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours"
                )
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], emp["shift_start"], date_param)
            target_half_day = (target_work_hours < 4.50)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_param)

    # Detect changed fields
    changes: Dict[str, Dict[str, Any]] = {}
    if target_status != curr_status:
        changes["status"] = {"from": curr_status, "to": target_status}
    if target_punch_in != curr_punch_in:
        changes["punch_in"] = {"from": curr_punch_in, "to": target_punch_in}
    if target_punch_out != curr_punch_out:
        changes["punch_out"] = {"from": curr_punch_out, "to": target_punch_out}
    if target_work_hours != curr_work_hours:
        changes["work_hours"] = {"from": curr_work_hours, "to": target_work_hours}
    if target_late_minutes != curr_late_minutes:
        changes["late_minutes"] = {"from": curr_late_minutes, "to": target_late_minutes}
    if target_overtime_minutes != curr_overtime_minutes:
        changes["overtime_minutes"] = {"from": curr_overtime_minutes, "to": target_overtime_minutes}
    if target_half_day != curr_half_day:
        changes["half_day"] = {"from": curr_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changed nothing"
        )

    now_ts = now_utc_truncated()
    new_history_entry = {
        "at": now_ts,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Atomic update with history length match (or missing field for legacy documents) to prevent lost updates under concurrency
    curr_history_len = len(rec.get("history", []))
    if curr_history_len == 0:
        filter_doc = {
            "_id": rec["_id"],
            "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]
        }
    else:
        filter_doc = {
            "_id": rec["_id"],
            "history": {"$size": curr_history_len}
        }

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day
        },
        "$push": {
            "history": new_history_entry
        }
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_doc,
        update_doc,
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance history"
        )

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = calculate_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = r["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = round_half_up(float(avg_wh), 2)
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    if to_dt < from_dt:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date cannot be before 'from' date")

    diff_days = (to_dt.date() - from_dt.date()).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        rate = r["attendance_rate"]
        if rate is not None:
            rate = round_half_up(float(rate), 4)

        m_avg = r["moving_avg_7d"]
        if m_avg is not None:
            m_avg = round_half_up(float(m_avg), 4)

        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return {
        "department": department,
        "items": items
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be after date_to")

        query = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
        cmd = {
            "find": "attendance_logs",
            "filter": query,
            "sort": SON([("date", DESCENDING), ("emp_code", ASCENDING)]),
            "skip": (page - 1) * page_size,
            "limit": page_size
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")

        pipeline = build_department_summary_pipeline(month, department)
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")

        f_dt = datetime.strptime(from_date, "%Y-%m-%d")
        t_dt = datetime.strptime(to_date, "%Y-%m-%d")
        if t_dt < f_dt or (t_dt.date() - f_dt.date()).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date range")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }
