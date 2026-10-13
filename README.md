# Candidate kit

# Employee Attendance & Analytics API

FastAPI backend service built for the HROne software engineering assignment to manage employee attendance, shifts, regularizations, and analytics using MongoDB.

## Features
- **Punch-In / Punch-Out**: Real-time attendance tracking in IST (UTC+5:30).
- **Working Hours Calculation**: Automatic calculation and rounding of total hours worked per shift.
- **Analytics API**: Summary metrics for attendance records, present counts, and average working hours.

## Tech Stack
- **Python**: 3.11+
- **Framework**: FastAPI, Uvicorn
- **Database**: PyMongo with MongoDB 6.0+ / Atlas

## Local Setup & Running
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```