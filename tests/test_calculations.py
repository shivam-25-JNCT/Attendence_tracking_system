import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from datetime import datetime, timezone, timedelta, time
from pydantic import ValidationError

from app.main import (
    IST,
    round_half_up,
    dt_to_epoch_ms,
    epoch_ms_to_dt,
    now_utc_truncated,
    calculate_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    calculate_working_days,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
)


def test_round_half_up():
    """R4, R8: Standard half-up rounding (not banker's rounding)."""
    assert round_half_up(2.525, 2) == 2.53
    assert round_half_up(2.524, 2) == 2.52
    assert round_half_up(2.535, 2) == 2.54  # Banker's would give 2.54, but 2.525 gives 2.53
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234


def test_truncation_to_whole_seconds():
    """R1: Instants are truncated to whole seconds."""
    dt = epoch_ms_to_dt(1783312500999)
    assert dt.microsecond == 0
    # Must equal 1783312500000 in epoch ms
    assert dt_to_epoch_ms(dt) == 1783312500000


def test_calculate_attendance_date_normal_shift():
    """R1: Normal shift attendance date is IST calendar date."""
    # 2026-07-06 09:28 IST (03:58 UTC)
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    date_str = calculate_attendance_date(dt_utc, "09:30", "18:30")
    assert date_str == "2026-07-06"


def test_calculate_attendance_date_overnight_shift():
    """
    R1: Overnight shift (shift_end <= shift_start, e.g. 22:00 - 06:00):
    punch-in strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    # 2026-07-07 05:30 IST (earlier than 06:00) -> belongs to 2026-07-06
    dt_utc1 = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
    assert calculate_attendance_date(dt_utc1, "22:00", "06:00") == "2026-07-06"

    # 2026-07-06 21:55 IST (after 06:00) -> belongs to 2026-07-06
    dt_utc2 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)  # 21:55 IST
    assert calculate_attendance_date(dt_utc2, "22:00", "06:00") == "2026-07-06"

    # 2026-07-07 06:00 IST (equal to shift_end, not earlier) -> belongs to 2026-07-07
    dt_utc3 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)  # 06:00 IST
    assert calculate_attendance_date(dt_utc3, "22:00", "06:00") == "2026-07-07"


def test_late_minutes_calculation():
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    Shift 09:30:
    09:40:00 -> 0
    09:40:01 -> 10
    10:15:59 -> 45
    """
    # 09:28:00 IST (early)
    early = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(early, "09:30", "2026-07-06") == 0

    # 09:40:00 IST (exactly 10 min, within grace period)
    at_10 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(at_10, "09:30", "2026-07-06") == 0

    # 09:40:01 IST (strictly > 10 min) -> 10
    at_10_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(at_10_01, "09:30", "2026-07-06") == 10

    # 10:15:59 IST -> 45
    at_45_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(at_45_59, "09:30", "2026-07-06") == 45


def test_overtime_calculation():
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    # Shift 09:30 - 18:30. Punch out 18:35 (5 min) -> 0 (< 30)
    out_5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)  # 18:35 IST
    assert compute_overtime(out_5, "18:30", "09:30", "2026-07-06") == 0

    # Punch out 18:59:59 (29 min 59 sec) -> 0 (< 30)
    out_29 = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    assert compute_overtime(out_29, "18:30", "09:30", "2026-07-06") == 0

    # Punch out 19:00:00 (exactly 30 min) -> 30 (>= 30)
    out_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(out_30, "18:30", "09:30", "2026-07-06") == 30

    # Punch out 19:10:00 (40 min) -> 40
    out_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(out_40, "18:30", "09:30", "2026-07-06") == 40

    # Overnight shift 22:00 - 06:00. Date 2026-07-06. Shift end is 2026-07-07 06:00 IST.
    # Punch out 2026-07-07 06:40 IST (01:10 UTC) -> 40 min
    out_night = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(out_night, "06:00", "22:00", "2026-07-06") == 40


def test_work_hours_and_half_day():
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    R5: half_day is true when rounded work_hours < 4.50.
    """
    # Sample 1: 03:58:00Z to 13:05:00Z = 32820 sec = 9.11666... -> 9.12
    t_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    wh = compute_work_hours(t_in, t_out)
    assert wh == 9.12
    assert (wh < 4.50) is False

    # Sample 4: 04:00:00Z to 07:30:00Z = 3.50 hours -> half_day = True
    t_in4 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    t_out4 = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)
    wh4 = compute_work_hours(t_in4, t_out4)
    assert wh4 == 3.50
    assert (wh4 < 4.50) is True

    # Boundary 4.495 hours -> rounded is 4.50 -> half_day = False (< 4.50)
    # 4.495 * 3600 = 16182 seconds
    t_boundary = t_in + timedelta(seconds=16182)
    wh_bound = compute_work_hours(t_in, t_boundary)
    assert wh_bound == 4.50
    assert (wh_bound < 4.50) is False

    # 4.494 * 3600 = 16178.4 -> 16178 seconds = 4.4938... -> 4.49 -> half_day = True
    t_bound2 = t_in + timedelta(seconds=16178)
    wh_bound2 = compute_work_hours(t_in, t_bound2)
    assert wh_bound2 == 4.49
    assert (wh_bound2 < 4.50) is True


def test_working_days_calculation():
    """
    R7: Working days. Monday - Friday, no holiday calendar;
    counted from joined_on for mid-month joiners.
    """
    # July 2026 has 31 days. Starts on Wednesday, ends on Friday.
    # Total Mon-Fri in July 2026 = 23 days.
    assert calculate_working_days("2026-07", "2026-01-05") == 23

    # Employee joined mid-month: 2026-07-13 (Monday).
    # From July 13 to July 31:
    # Week 1: 13-17 (5)
    # Week 2: 20-24 (5)
    # Week 3: 27-31 (5)
    # Total = 15 working days.
    assert calculate_working_days("2026-07", "2026-07-13") == 15

    # Employee joined after the month: 2026-08-01
    assert calculate_working_days("2026-07", "2026-08-01") == 0


def test_pydantic_models_validation():
    """Test schema validation per openapi.yaml."""
    # Valid employee
    emp = EmployeeCreate(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-05"
    )
    assert emp.emp_code == "EMP0001"

    # Shift start equal to shift end -> ValidationError
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05"
        )

    # Invalid email
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="not-an-email",
            department="Engineering",
            joined_on="2026-01-05"
        )

    # Invalid emp_code
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="BADCODE",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05"
        )

    # EpochMillis: Reject below 100000000000 (seconds-looking values)
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1700000000)

    # Valid EpochMillis
    p_in = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000)
    assert p_in.punched_at == 1783312500000
