import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from dotenv import load_dotenv
from bson import json_util
import json

load_dotenv()

from app.main import app, db, IST, dt_to_epoch_ms, round_half_up


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    try:
        # Check if database is reachable before running suite
        client_check = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
        client_check.admin.command("ping")
    except Exception as e:
        pytest.skip(f"MongoDB Atlas unreachable due to infrastructure/network access restriction: {type(e).__name__} - {e}")

    with TestClient(app) as test_client:
        yield test_client


def test_health(setup_test_db):
    client = setup_test_db
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee_success_and_duplicate(setup_test_db):
    client = setup_test_db
    emp_data = {
        "emp_code": "EMP9901",
        "name": "Test Engineer",
        "email": "test.eng@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01"
    }
    # Clean up if already exists
    db.employees.delete_one({"emp_code": "EMP9901"})

    res = client.post("/employees", json=emp_data)
    assert res.status_code == 201
    body = res.json()
    assert body["emp_code"] == "EMP9901"
    assert "_id" not in body
    assert isinstance(body["created_at"], int)
    assert body["created_at"] >= 100_000_000_000

    # Duplicate create must return 409
    res_dup = client.post("/employees", json=emp_data)
    assert res_dup.status_code == 409


def test_create_employee_validation(setup_test_db):
    client = setup_test_db
    # Invalid emp_code regex
    res = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Invalid Code",
        "email": "inv@example.com",
        "department": "QA",
        "joined_on": "2026-06-01"
    })
    assert res.status_code == 422

    # Equal shift_start and shift_end
    res2 = client.post("/employees", json={
        "emp_code": "EMP9902",
        "name": "Equal Shifts",
        "email": "equal@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-06-01"
    })
    assert res2.status_code == 422


def test_list_employees(setup_test_db):
    client = setup_test_db
    res = client.get("/employees?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert len(data["items"]) <= 5
    # Verify sorting
    emp_codes = [item["emp_code"] for item in data["items"]]
    assert emp_codes == sorted(emp_codes)

    # Department filter
    res_dept = client.get("/employees?department=Engineering")
    assert res_dept.status_code == 200
    for item in res_dept.json()["items"]:
        assert item["department"] == "Engineering"


def test_punch_in_and_late_minutes_boundary(setup_test_db):
    client = setup_test_db
    # Create employee EMP9910 with 09:30 shift
    db.employees.delete_one({"emp_code": "EMP9910"})
    db.attendance_logs.delete_many({"emp_code": "EMP9910"})

    client.post("/employees", json={
        "emp_code": "EMP9910",
        "name": "Boundary Test",
        "email": "boundary@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Test 1: Exactly at 09:40:00 IST (10 min grace period boundary) -> late_minutes = 0
    # 2026-07-20 09:40:00 IST = 2026-07-20 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc)
    ms_094000 = dt_to_epoch_ms(dt_094000)

    res1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9910",
        "punched_at": ms_094000,
        "status": "PRESENT"
    })
    assert res1.status_code == 201
    body1 = res1.json()
    assert body1["late_minutes"] == 0
    assert body1["date"] == "2026-07-20"

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9910",
        "punched_at": ms_094000,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Test 2: Exactly 09:40:01 IST -> strictly more than 10 min -> late_minutes = 10
    # 2026-07-21 09:40:01 IST = 2026-07-21 04:10:01 UTC
    dt_094001 = datetime(2026, 7, 21, 4, 10, 1, tzinfo=timezone.utc)
    ms_094001 = dt_to_epoch_ms(dt_094001)

    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9910",
        "punched_at": ms_094001,
        "status": "PRESENT"
    })
    assert res2.status_code == 201
    body2 = res2.json()
    assert body2["late_minutes"] == 10
    assert body2["date"] == "2026-07-21"

    # Test 3: 10:15:59 IST -> 45 min 59 sec -> late_minutes = 45
    # 2026-07-22 10:15:59 IST = 2026-07-22 04:45:59 UTC
    dt_101559 = datetime(2026, 7, 22, 4, 45, 59, tzinfo=timezone.utc)
    ms_101559 = dt_to_epoch_ms(dt_101559)

    res3 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9910",
        "punched_at": ms_101559,
        "status": "PRESENT"
    })
    assert res3.status_code == 201
    body3 = res3.json()
    assert body3["late_minutes"] == 45
    assert body3["date"] == "2026-07-22"


def test_overnight_punch_in(setup_test_db):
    client = setup_test_db
    # EMP9920 has overnight shift 22:00 - 06:00
    db.employees.delete_one({"emp_code": "EMP9920"})
    db.attendance_logs.delete_many({"emp_code": "EMP9920"})

    client.post("/employees", json={
        "emp_code": "EMP9920",
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punched in at 05:30 IST on July 7 (< 06:00 shift_end) -> belongs to July 6 shift
    # 2026-07-07 05:30:00 IST = 2026-07-07 00:00:00 UTC
    dt_night = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    ms_night = dt_to_epoch_ms(dt_night)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9920",
        "punched_at": ms_night,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    body = res.json()
    assert body["date"] == "2026-07-06"


def test_punch_out_calculations_and_errors(setup_test_db):
    client = setup_test_db
    db.employees.delete_one({"emp_code": "EMP9930"})
    db.attendance_logs.delete_many({"emp_code": "EMP9930"})

    client.post("/employees", json={
        "emp_code": "EMP9930",
        "name": "Punch Out Test",
        "email": "punchout@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 09:30:00 IST on 2026-07-23 (04:00:00 UTC)
    in_dt = datetime(2026, 7, 23, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP9930",
        "punched_at": dt_to_epoch_ms(in_dt),
        "status": "PRESENT"
    })

    # Error: punch_out <= punch_in
    res_err1 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9930",
        "punched_at": dt_to_epoch_ms(in_dt)
    })
    assert res_err1.status_code == 422

    # Error: punch_out > 24 hours after punch_in
    res_err2 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9930",
        "punched_at": dt_to_epoch_ms(in_dt + timedelta(hours=25))
    })
    assert res_err2.status_code == 422

    # Punch out at 19:15:00 IST (13:45:00 UTC)
    # Difference: 9h 45m = 9.75 hours
    # Shift end 18:30, out at 19:15 -> 45 minutes overtime (>= 30 min threshold)
    out_dt = datetime(2026, 7, 23, 13, 45, 0, tzinfo=timezone.utc)
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9930",
        "punched_at": dt_to_epoch_ms(out_dt)
    })
    assert res_out.status_code == 200
    body = res_out.json()
    assert body["work_hours"] == 9.75
    assert body["overtime_minutes"] == 45
    assert body["half_day"] is False

    # Second punch out -> 409 already punched out
    res_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9930",
        "punched_at": dt_to_epoch_ms(out_dt)
    })
    assert res_dup.status_code == 409


def test_regularize_attendance(setup_test_db):
    client = setup_test_db
    db.employees.delete_one({"emp_code": "EMP9940"})
    db.attendance_logs.delete_many({"emp_code": "EMP9940"})

    client.post("/employees", json={
        "emp_code": "EMP9940",
        "name": "Regularize Test",
        "email": "reg@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in on 2026-07-24 at 10:05 IST (late 35 min)
    in_dt = datetime(2026, 7, 24, 4, 35, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP9940",
        "punched_at": dt_to_epoch_ms(in_dt),
        "status": "PRESENT"
    })

    # Punch out at 18:30 IST (9.00 work hours, 0 overtime)
    out_dt = datetime(2026, 7, 24, 13, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP9940",
        "punched_at": dt_to_epoch_ms(out_dt)
    })

    # Regularize: no changes -> 422
    res_nochange = client.patch("/attendance/EMP9940/2026-07-24", json={
        "reason": "Accidental submit without changes",
        "regularized_by": "manager"
    })
    assert res_nochange.status_code == 422

    # Regularize: correct punch_in to 09:28 IST (03:58 UTC) -> late_minutes should become 0
    new_in_dt = datetime(2026, 7, 24, 3, 58, 0, tzinfo=timezone.utc)
    res_reg = client.patch("/attendance/EMP9940/2026-07-24", json={
        "punch_in": dt_to_epoch_ms(new_in_dt),
        "reason": "Fingerprint scanner failed, arrived on time",
        "regularized_by": "hr.admin"
    })
    assert res_reg.status_code == 200
    body = res_reg.json()
    assert body["late_minutes"] == 0
    assert len(body["history"]) == 1
    h = body["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "Fingerprint scanner failed, arrived on time"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"]["from"] == dt_to_epoch_ms(in_dt)
    assert h["changes"]["punch_in"]["to"] == dt_to_epoch_ms(new_in_dt)

    # Regularize to LEAVE clears punch times
    res_leave = client.patch("/attendance/EMP9940/2026-07-24", json={
        "status": "LEAVE",
        "reason": "Applied retroactive leave approval",
        "regularized_by": "hr.manager"
    })
    assert res_leave.status_code == 200
    b_leave = res_leave.json()
    assert b_leave["status"] == "LEAVE"
    assert b_leave["punch_in"] is None
    assert b_leave["punch_out"] is None
    assert b_leave["work_hours"] is None
    assert len(b_leave["history"]) == 2


def test_employee_monthly_analytics(setup_test_db):
    client = setup_test_db
    # Asha Rao (EMP0001) joined 2026-01-05
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 Mon-Fri
    assert data["present_days"] >= 0
    assert "attendance_pct" in data


def test_department_summary_analytics(setup_test_db):
    client = setup_test_db
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 1
    # Check department items
    for item in data["items"]:
        assert "department" in item
        assert "headcount" in item
        assert item["headcount"] > 0
        assert "present_days" in item
        assert "avg_work_hours" in item


def test_late_leaderboard_analytics(setup_test_db):
    client = setup_test_db
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert "items" in data
    # Verify standard competition ranking properties if items exist
    if data["items"]:
        ranks = [item["rank"] for item in data["items"]]
        assert ranks[0] == 1


def test_department_trend_analytics(setup_test_db):
    client = setup_test_db
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-08")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 4  # exactly 4 days: 5, 6, 7, 8
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08"]


def test_admin_explain_endpoints(setup_test_db):
    client = setup_test_db
    endpoints_to_test = [
        ("/admin/explain/attendance_list?page=1&page_size=20", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07", "attendance_logs"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07", "attendance_logs")
    ]

    for url, expected_collection in endpoints_to_test:
        res = client.get(url)
        assert res.status_code == 200, f"Failed explain for {url}: {res.text}"
        body = res.json()
        assert body["collection"] == expected_collection
        explain_json = json.dumps(body["explain"])
        # Must contain IXSCAN and not COLLSCAN
        assert "IXSCAN" in explain_json, f"Missing IXSCAN for {url}"
        assert "COLLSCAN" not in explain_json, f"COLLSCAN found for {url}"
