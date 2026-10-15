import json
from bson import json_util
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, EmailStr, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
    tzinfo=UTC,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------- Common helpers ----------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

    return parsed


def epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return now_utc()

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_EPOCH_MS
        or value > MAX_EPOCH_MS
    ):
        raise HTTPException(
            422,
            "Timestamp must be a valid epoch-millisecond integer",
        )

    return datetime.fromtimestamp(
        value / 1000,
        UTC,
    ).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def validate_clock(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ValueError("Time must use HH:MM format")
    return value


def shift_start_datetime(day: date, shift_start: str) -> datetime:
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def shift_end_datetime(
    day: date,
    shift_start: str,
    shift_end: str,
) -> datetime:
    hour, minute = map(int, shift_end.split(":"))
    result = datetime.combine(day, time(hour, minute), tzinfo=IST)

    if shift_end <= shift_start:
        result += timedelta(days=1)

    return result


def attendance_day(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> date:
    local_time = punch_in.astimezone(IST)
    result = local_time.date()

    # An early-morning punch may belong to the previous overnight shift.
    if (
        shift_end <= shift_start
        and local_time.strftime("%H:%M") < shift_end
    ):
        result -= timedelta(days=1)

    return result


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: date,
) -> int:
    local_punch = punch_in.astimezone(IST).replace(microsecond=0)
    shift_start_at = shift_start_datetime(attendance_date, shift_start)
    elapsed_seconds = int((local_punch - shift_start_at).total_seconds())

    # A grace period of 10 minutes; late minutes are not reduced by 10.
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: date,
) -> int:
    scheduled_end = shift_end_datetime(
        attendance_date,
        shift_start,
        shift_end,
    )
    elapsed_seconds = int((punch_out - scheduled_end).total_seconds())
    minutes = max(0, elapsed_seconds // 60)

    return minutes if minutes >= 30 else 0


def serialize_employee(document: dict) -> dict:
    result = {
        key: value
        for key, value in document.items()
        if key != "_id"
    }

    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = to_epoch_ms(result["created_at"])

    return result


def serialize_attendance(document: dict) -> dict:
    result = {
        key: value
        for key, value in document.items()
        if key not in {"_id", "id"}
    }

    for field in ("punch_in", "punch_out"):
        if field in result:
            result[field] = to_epoch_ms(result[field])

    result.setdefault("history", [])
    result.setdefault("half_day", False)

    for entry in result["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])

    return result


# ---------- Request models ----------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_clock(cls, value):
        return validate_clock(value)

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            epoch_ms(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            epoch_ms(value)
        return value


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    regularized_by: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            epoch_ms(value)
        return value


# ---------- Database indexes ----------

@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_employee_date_desc",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )


# ---------- Health ----------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


# ---------- Employees ----------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = now_utc()

    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}

    total = db.employees.count_documents(query)

    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------- Attendance punch-in ----------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found")

    punched_at = epoch_ms(body.punched_at)

    attendance_date = attendance_day(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
    )
    date_string = attendance_date.isoformat()

    document = {
        "emp_code": body.emp_code,
        "date": date_string,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at,
            employee["shift_start"],
            attendance_date,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Already punched in for this attendance date",
        )

    return serialize_attendance(document)


# ---------- Attendance punch-out ----------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found")

    punched_out_at = epoch_ms(body.punched_at)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_out_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found before this timestamp")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punched_in_at = record["punch_in"]

    if (
        punched_out_at <= punched_in_at
        or punched_out_at > punched_in_at + timedelta(hours=24)
    ):
        raise HTTPException(
            422,
            "Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(punched_in_at, punched_out_at)
    attendance_date = parse_date(record["date"])

    overtime_minutes = compute_overtime(
        punched_out_at,
        employee["shift_start"],
        employee["shift_end"],
        attendance_date,
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_out_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Already punched out")

    record.update({
        "punch_out": punched_out_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    })

    return serialize_attendance(record)


# ---------- Attendance listing ----------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid attendance status")
        query["status"] = status

    if date_from:
        parse_date(date_from, "date_from")

    if date_to:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422,
            "date_from must be on or before date_to",
        )

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)

    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------- Attendance correction / regularization ----------

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(
    emp_code: str,
    date_str: str,
    body: CorrectionIn,
):
    attendance_date = parse_date(date_str)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    old = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": attendance_date.isoformat(),
    })
    if not old:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else old["status"]

    old_in = old.get("punch_in")
    old_out = old.get("punch_out")

    new_in = (
        epoch_ms(body.punch_in)
        if body.punch_in is not None
        else old_in
    )
    new_out = (
        epoch_ms(body.punch_out)
        if body.punch_out is not None
        else old_out
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot include punch timestamps",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(422, "Presence status requires punch_in")

    if new_in is not None:
        if attendance_day(
            new_in,
            employee["shift_start"],
            employee["shift_end"],
        ) != attendance_date:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date",
            )

    if new_out is not None:
        if (
            new_in is None
            or new_out <= new_in
            or new_out > new_in + timedelta(hours=24)
        ):
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )

    late_minutes = (
        compute_late_minutes(
            new_in,
            employee["shift_start"],
            attendance_date,
        )
        if new_in is not None
        else 0
    )

    work_hours = (
        compute_work_hours(new_in, new_out)
        if new_in is not None and new_out is not None
        else None
    )

    overtime_minutes = (
        compute_overtime(
            new_out,
            employee["shift_start"],
            employee["shift_end"],
            attendance_date,
        )
        if new_out is not None
        else 0
    )

    half_day = work_hours is not None and work_hours < 4.50

    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for key, value in proposed.items():
        previous = old.get(
            key,
            False if key == "half_day" else None,
        )

        if previous != value:
            changes[key] = {
                "from": (
                    to_epoch_ms(previous)
                    if isinstance(previous, datetime)
                    else previous
                ),
                "to": (
                    to_epoch_ms(value)
                    if isinstance(value, datetime)
                    else value
                ),
            }

    if not changes:
        raise HTTPException(422, "Correction does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-swap: reject the correction if the record changed
    # after we read it, rather than losing another user's history.
    filter_doc = {"_id": old["_id"]}
    for key in proposed:
        filter_doc[key] = old.get(
            key,
            False if key == "half_day" else None,
        )

    result = db.attendance_logs.update_one(
        filter_doc,
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return serialize_attendance(updated)


# ---------- Analytics helpers ----------

def month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")

    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)

    return first, next_month - timedelta(days=1)


def weekdays_between(start: date, end: date) -> int:
    """Count Monday-Friday days, inclusive."""
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def month_working_days(joined_on: str, first: date, last: date) -> int:
    start = max(first, parse_date(joined_on, "joined_on"))
    return weekdays_between(start, last) if start <= last else 0


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    first, last = month_bounds(month)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    working_days = month_working_days(employee["joined_on"], first, last)

    logs = list(
        db.attendance_logs.aggregate([
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
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
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {
                                            "$not": [
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        [1, 7],
                                                    ]
                                                }
                                            ]
                                        },
                                        {"$ne": ["$half_day", True]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "half_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {"$eq": ["$half_day", True]},
                                        {
                                            "$not": [
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        [1, 7],
                                                    ]
                                                }
                                            ]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                }
            },
        ])
    )

    stats = logs[0] if logs else {}
    present_days = (
        stats.get("present_days", 0)
        + 0.5 * stats.get("half_days", 0)
    )
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "attendance_pct": attendance_pct,
    }


# ---------- Department summary analytics ----------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    first_str = first.isoformat()
    last_str = last.isoformat()

    employee_match = {"joined_on": {"$lte": last_str}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {
                                "$gte": first_str,
                                "$lte": last_str,
                            },
                        }
                    },
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$not": [
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [1, 7],
                                        ]
                                    }
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
                                                "$_is_weekday",
                                                {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                {"$ne": ["$half_day", True]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "half_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$_is_weekday",
                                                {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                {"$eq": ["$half_day", True]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "attendance_stats",
            }
        },
        {
            "$addFields": {
                "stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_stats", 0]},
                        {},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$add": [
                            {"$ifNull": ["$stats.present_days", 0]},
                            {
                                "$multiply": [
                                    0.5,
                                    {"$ifNull": ["$stats.half_days", 0]},
                                ]
                            },
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$stats.work_hours_count", 0]}
                },
                "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}
                },
                "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$stats.on_duty_count", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    output = []

    for item in results:
        count = item["work_hours_count"]
        output.append({
            "department": item["_id"],
            "headcount": item["headcount"],
            "present_days": round_half_up(item["present_days"], 2),
            "avg_work_hours": (
                round_half_up(item["work_hours_sum"] / count, 2)
                if count
                else None
            ),
            "late_count": item["late_count"],
            "total_late_minutes": item["total_late_minutes"],
            "leave_count": item["leave_count"],
            "on_duty_count": item["on_duty_count"],
        })

    return {"month": month, "items": output}


# ---------- Late-arrival leaderboard ----------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)

    employee_match = {}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {
                                "$gte": first.isoformat(),
                                "$lte": last.isoformat(),
                            },
                            "late_minutes": {"$gt": 0},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_late_minutes": {"$sum": "$late_minutes"},
                        }
                    },
                ],
                "as": "late_stats",
            }
        },
        {
            "$addFields": {
                "total_late_minutes": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.total_late_minutes", 0]},
                        0,
                    ]
                }
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "limit": limit,
        "items": items,
    }


# ---------- Department daily trend ----------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start:
        raise HTTPException(422, "'to' must be on or after 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    start_str = start.isoformat()
    end_str = end.isoformat()

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_str,
                    "$lte": end_str,
                }
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
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENT_STATUSES)]},
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
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": {
                    "$dateFromString": {"dateString": "$_id"}
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end + timedelta(days=1), time.min),
                    ],
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {"$dayOfWeek": "$day"},
                                [1, 7],
                            ]
                        }
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$trend_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$multiply": [
                                {"$divide": ["$present_count", "$headcount"]},
                                100,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_raw": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    if not rows:
        rows = []
        current_date = start

        while current_date <= end:
            is_working_day = current_date.weekday() < 5

            headcount = db.employees.count_documents({
                "department": department,
                "joined_on": {"$lte": current_date.isoformat()},
            })

            rows.append({
                "date": current_date.isoformat(),
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": 0,
                "late_count": 0,
                "attendance_rate": (
                    0.0 if is_working_day and headcount > 0 else None
                ),
                "moving_avg_raw": None,
            })

            current_date += timedelta(days=1)

    # Densify operates on existing input rows. If the range has no logs at
    # all, the aggregation returns no rows; this is handled in a later fix.
    output = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving_avg = row.get("moving_avg_raw")

        output.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": round_half_up(row["present_count"], 2),
            "late_count": row["late_count"],
            "attendance_rate": (
                round_half_up(rate, 4) if rate is not None else None
            ),
            "moving_avg_7d": (
                round_half_up(moving_avg, 4)
                if moving_avg is not None
                else None
            ),
        })

    return {
        "department": department,
        "from": start_str,
        "to": end_str,
        "items": output,
    }



# ---------- Admin query explain ----------

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
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
):
    """Explain the main query used by a named endpoint."""

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if status:
            if status not in STATUSES:
                raise HTTPException(422, "Invalid attendance status")
            query["status"] = status

        if date_from:
            parse_date(date_from, "date_from")

        if date_to:
            parse_date(date_to, "date_to")

        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422, "date_from must be on or before date_to"
            )

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        command = {
            "find": "attendance_logs",
            "filter": query,
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422, "employee_monthly requires emp_code and month"
            )

        first, last = month_bounds(month)

        if not db.employees.find_one({"emp_code": emp_code}):
            raise HTTPException(404, "Employee not found")

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
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
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {
                                            "$not": [{
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7],
                                                ]
                                            }]
                                        },
                                        {"$ne": ["$half_day", True]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "half_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {"$eq": ["$half_day", True]},
                                        {
                                            "$not": [{
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7],
                                                ]
                                            }]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]

        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                422, "department_summary requires month"
            )

        first, last = month_bounds(month)
        first_str = first.isoformat()
        last_str = last.isoformat()

        employee_match = {"joined_on": {"$lte": last_str}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$emp_code", "$$code"]},
                                "date": {
                                    "$gte": first_str,
                                    "$lte": last_str,
                                },
                            }
                        },
                        {
                            "$addFields": {
                                "_is_weekday": {
                                    "$not": [{
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [1, 7],
                                        ]
                                    }]
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
                                                    "$_is_weekday",
                                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                    {"$ne": ["$half_day", True]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "half_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    "$_is_weekday",
                                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                    {"$eq": ["$half_day", True]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "work_hours_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            "$work_hours",
                                            0,
                                        ]
                                    }
                                },
                                "work_hours_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": ["$late_minutes", 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "total_late_minutes": {"$sum": "$late_minutes"},
                                "leave_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "LEAVE"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "on_duty_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "ON_DUTY"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "attendance_stats",
                }
            },
            {
                "$addFields": {
                    "stats": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$attendance_stats", 0]},
                            {},
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {
                        "$sum": {
                            "$add": [
                                {"$ifNull": ["$stats.present_days", 0]},
                                {
                                    "$multiply": [
                                        0.5,
                                        {"$ifNull": ["$stats.half_days", 0]},
                                    ]
                                },
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}
                    },
                    "work_hours_count": {
                        "$sum": {"$ifNull": ["$stats.work_hours_count", 0]}
                    },
                    "late_count": {
                        "$sum": {"$ifNull": ["$stats.late_count", 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {"$ifNull": ["$stats.leave_count", 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$ifNull": ["$stats.on_duty_count", 0]}
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]

        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                422, "late_leaderboard requires month"
            )

        first, last = month_bounds(month)
        employee_match = {}

        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$emp_code", "$$code"]},
                                "date": {
                                    "$gte": first.isoformat(),
                                    "$lte": last.isoformat(),
                                },
                                "late_minutes": {"$gt": 0},
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "total_late_minutes": {"$sum": "$late_minutes"},
                            }
                        },
                    ],
                    "as": "late_stats",
                }
            },
            {
                "$addFields": {
                    "total_late_minutes": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$late_stats.total_late_minutes", 0]},
                            0,
                        ]
                    }
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]

        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }

    else:  # department_trend
        if not department or not date_from or not date_to:
            raise HTTPException(
                422,
                "department_trend requires department, from and to",
            )

        start = parse_date(date_from, "from")
        end = parse_date(date_to, "to")

        if end < start:
            raise HTTPException(422, "'to' must be on or after 'from'")

        if (end - start).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "Department not found")

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": end.isoformat(),
                    }
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
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", list(PRESENT_STATUSES)]},
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
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "day": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "day",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(start, time.min),
                            datetime.combine(end, time.min),
                        ],
                    },
                }
            },
            {
                "$set": {
                    "present_count": {"$ifNull": ["$present_count", 0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "date_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$day",
                            "timezone": "UTC",
                        }
                    },
                    "is_working_day": {
                        "$not": [{
                            "$in": [{"$dayOfWeek": "$day"}, [1, 7]]
                        }]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"trend_day": "$date_string"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$trend_day"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$headcount_result.count", 0]},
                            0,
                        ]
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$multiply": [
                                    {"$divide": ["$present_count", "$headcount"]},
                                    100,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"day": 1},
                    "output": {
                        "moving_avg_raw": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$date_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_raw": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    try:
        explanation = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )
    except PyMongoError as exc:
        raise HTTPException(
            503,
            f"MongoDB explain failed: {type(exc).__name__}",
        )

    return json.loads(json_util.dumps(explanation))

