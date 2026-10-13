import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import mongomock
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

import app.main as app_main
from app.main import app, dt_to_epoch_ms, epoch_ms_to_dt


@pytest.fixture
def mock_app_client():
    mock_client = mongomock.MongoClient()
    mock_db = mock_client.attendance_db

    # Patch client and db in app.main
    orig_client = app_main.client
    orig_db = app_main.db
    app_main.client = mock_client
    app_main.db = mock_db

    # Ensure unique indexes
    mock_db.employees.create_index([("emp_code", 1)], unique=True)
    mock_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)

    with TestClient(app) as test_client:
        yield test_client, mock_db

    app_main.client = orig_client
    app_main.db = orig_db


def test_health_ok_and_failure(mock_app_client):
    client, _ = mock_app_client
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    # Simulate MongoDB failure
    class BrokenAdmin:
        def command(self, *args, **kwargs):
            raise Exception("Connection lost")

    class BrokenClient:
        admin = BrokenAdmin()

    orig_client = app_main.client
    app_main.client = BrokenClient()
    try:
        res_fail = client.get("/health")
        assert res_fail.status_code == 503
        assert res_fail.json() == {"detail": "MongoDB unavailable"}
    finally:
        app_main.client = orig_client


def test_employee_crud_and_validation(mock_app_client):
    client, mock_db = mock_app_client
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }

    # 1. Successful creation
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    body = res.json()
    assert body["emp_code"] == "EMP0001"
    assert "_id" not in body
    assert isinstance(body["created_at"], int)

    # 2. Duplicate emp_code -> 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409
    assert res_dup.json() == {"detail": "emp_code already exists"}

    # 3. Validation: bad emp_code regex -> 422
    res_bad = client.post("/employees", json={**emp_payload, "emp_code": "BAD123"})
    assert res_bad.status_code == 422

    # 4. Validation: equal shift_start and shift_end -> 422
    res_shifts = client.post("/employees", json={**emp_payload, "emp_code": "EMP0002", "shift_end": "09:30"})
    assert res_shifts.status_code == 422

    # 5. List employees with pagination and department filter
    client.post("/employees", json={**emp_payload, "emp_code": "EMP0003", "department": "Sales"})
    res_list = client.get("/employees?page=1&page_size=10")
    assert res_list.status_code == 200
    data = res_list.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0003"

    res_dept = client.get("/employees?department=Engineering")
    assert res_dept.status_code == 200
    assert res_dept.json()["total"] == 1
    assert res_dept.json()["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_rules_and_boundary(mock_app_client):
    client, _ = mock_app_client
    # Create employee
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Unknown employee -> 404
    r_unknown = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r_unknown.status_code == 404

    # Punch in 09:40:00 IST (exactly 10 min grace period boundary) -> late_minutes = 0
    # 2026-07-06 09:40:00 IST = 2026-07-06 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(dt_094000),
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    body = res_in.json()
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 0
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(dt_094000)
    })
    assert res_dup.status_code == 409

    # Punch in 09:40:01 IST on next day -> strictly > 10 min -> late_minutes = 10
    dt_094001 = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    res_in2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(dt_094001)
    })
    assert res_in2.status_code == 201
    assert res_in2.json()["late_minutes"] == 10

    # Punch in 10:15:59 IST on July 8 -> late_minutes = 45
    dt_101559 = datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc)
    res_in3 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(dt_101559)
    })
    assert res_in3.status_code == 201
    assert res_in3.json()["late_minutes"] == 45


def test_overnight_shift_punch_in(mock_app_client):
    client, _ = mock_app_client
    # Overnight employee: 22:00 - 06:00
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera Nair",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10"
    })

    # Punch in at 05:30 IST on July 7 (< 06:00) -> belongs to July 6
    dt_night = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": dt_to_epoch_ms(dt_night)
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_punch_out_rules_and_calculations(mock_app_client):
    client, _ = mock_app_client
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Unknown employee -> 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404

    # No punch-in found -> 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404

    # Punch in at 09:28:00 IST (03:58:00 UTC) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(in_dt)
    })

    # Error: punch_out <= punch_in -> 422
    res_err1 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(in_dt)
    })
    assert res_err1.status_code == 422

    # Error: punch_out > 24 hours -> 422
    res_err2 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(in_dt + timedelta(hours=25))
    })
    assert res_err2.status_code == 422

    # Punch out at 18:35:00 IST (13:05:00 UTC):
    # 9h 7m = 9.12 hours (half-up)
    # Overtime: 5 minutes (< 30 min minimum) -> 0
    # Half-day: 9.12 >= 4.50 -> False
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(out_dt)
    })
    assert res_out.status_code == 200
    body = res_out.json()
    assert body["work_hours"] == 9.12
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False

    # Second punch-out on same record -> 409
    res_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(out_dt)
    })
    assert res_dup.status_code == 409


def test_regularize_attendance_and_legacy_records(mock_app_client):
    client, mock_db = mock_app_client
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in (late 35 min: 10:05 IST)
    in_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(in_dt)
    })

    # Punch out at 19:10 IST (40 min overtime)
    out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": dt_to_epoch_ms(out_dt)
    })

    # 1. 404 for unknown employee or date
    assert client.patch("/attendance/EMP9999/2026-07-06", json={"reason": "Test reason", "regularized_by": "admin"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-07-20", json={"reason": "Test reason", "regularized_by": "admin"}).status_code == 404

    # 2. 422 if request changes nothing
    res_nochange = client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "Test no changes",
        "regularized_by": "admin"
    })
    assert res_nochange.status_code == 422

    # 3. 422 if punch_in leaves attendance date
    res_wrongdate = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": dt_to_epoch_ms(in_dt + timedelta(days=1)),
        "reason": "Wrong day punch",
        "regularized_by": "admin"
    })
    assert res_wrongdate.status_code == 422

    # 4. Correct punch_in to 09:28 IST (03:58 UTC) -> late_minutes resets to 0
    new_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    res_patch = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": dt_to_epoch_ms(new_in),
        "reason": "Biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res_patch.status_code == 200
    b = res_patch.json()
    assert b["late_minutes"] == 0
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "Biometric glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["punch_in"] == {"from": dt_to_epoch_ms(in_dt), "to": dt_to_epoch_ms(new_in)}

    # 5. Setting LEAVE clears punch times and recomputes derived fields
    res_leave = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "LEAVE",
        "reason": "Applied medical leave",
        "regularized_by": "hr.manager"
    })
    assert res_leave.status_code == 200
    b_leave = res_leave.json()
    assert b_leave["status"] == "LEAVE"
    assert b_leave["punch_in"] is None
    assert b_leave["punch_out"] is None
    assert b_leave["work_hours"] is None
    assert b_leave["late_minutes"] == 0
    assert b_leave["overtime_minutes"] == 0
    assert len(b_leave["history"]) == 2

    # 6. Legacy record test: document in Mongo WITHOUT history field can be regularized cleanly
    mock_db.employees.insert_one({
        "emp_code": "EMP0002",
        "name": "Legacy Emp",
        "email": "legacy@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })
    legacy_in = datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc)
    legacy_out = datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": legacy_in,
        "punch_out": legacy_out,
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0
        # Notice: NO history and NO half_day field
    })
    res_legacy = client.patch("/attendance/EMP0002/2026-07-14", json={
        "status": "WFH",
        "reason": "Correct status to WFH",
        "regularized_by": "manager"
    })
    assert res_legacy.status_code == 200
    assert res_legacy.json()["status"] == "WFH"
    assert len(res_legacy.json()["history"]) == 1


def test_attendance_listing_and_filters(mock_app_client):
    client, _ = mock_app_client
    # Validation: date_from > date_to -> 422
    res_err = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_err.status_code == 422
    assert res_err.json() == {"detail": "date_from cannot be after date_to"}

    # Validation: page_size > 100 -> 422
    assert client.get("/attendance?page_size=101").status_code == 422
    assert client.get("/attendance?page=0").status_code == 422
