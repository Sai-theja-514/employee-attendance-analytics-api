
from pydantic import ValidationError

from app.main import EmployeeIn, PunchInIn, PunchOutIn, CorrectionIn


def test_employee_rejects_invalid_email():
    try:
        EmployeeIn(
            emp_code="EMP123458",
            name="Test Employee",
            email="not-an-email",
            department="Engineering",
            joined_on="2026-10-13",
        )
        assert False, "Invalid email should be rejected"
    except ValidationError:
        pass


def test_punch_in_rejects_string_timestamp():
    try:
        PunchInIn(
            emp_code="EMP123458",
            punched_at="1792018200000",
        )
        assert False, "String timestamp should be rejected"
    except ValidationError:
        pass


def test_punch_out_rejects_float_timestamp():
    try:
        PunchOutIn(
            emp_code="EMP123458",
            punched_at=1792018200000.5,
        )
        assert False, "Float timestamp should be rejected"
    except ValidationError:
        pass


def test_correction_rejects_string_timestamp():
    try:
        CorrectionIn(
            punch_in="1792018200000",
            regularized_by="admin",
            reason="Testing strict timestamp validation",
        )
        assert False, "String timestamp should be rejected"
    except ValidationError:
        pass
