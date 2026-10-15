# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, manual attendance corrections, audit history, and attendance analytics.

## Features

- Create and list employees with validation and pagination.
- Record employee punch-in and punch-out times.
- Calculate late minutes, work hours, overtime, and half-day status.
- Correct attendance records while preserving an audit trail.
- Generate monthly employee analytics and department summaries.
- Provide a late-comers leaderboard and daily department attendance trends.
- Inspect MongoDB query execution plans through admin explain endpoints.

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer
- MongoDB Atlas or a reachable MongoDB instance

## Setup

Create and activate a virtual environment in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file containing your own MongoDB configuration:

```text
MONGO_URI=your_mongodb_connection_string
MONGO_DB=attendance_db
```

Keep `.env` private and never commit credentials to Git.

## Run the API

```powershell
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs` to explore the interactive Swagger UI.

Check database connectivity at `http://127.0.0.1:8000/health`.

## Tests

Run the validation tests with:

```powershell
python -m pytest .\tests\test_validation.py -v
```

The validation tests cover selected request-model edge cases. They do not replace comprehensive endpoint and business-rule testing.

## Sample Data

The `sample_data/` directory contains the provided MongoDB Extended JSON documents, and `sample_seed.py` loads the sample dataset. **Seeding clears existing employee and attendance records**, so use a dedicated test database before running it.

The evaluator uses a larger dataset with additional edge cases.

## Submission

The public repository includes `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, and `DECISIONS.md`, along with relevant project documentation and tests.

Do not commit `.env`, database credentials, other secrets, or a Dockerfile.