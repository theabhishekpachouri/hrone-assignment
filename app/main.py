"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

"""
import os
from datetime import datetime, timedelta, timezone, date as dt_date
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# Indexes creation on startup

@app.on_event("startup")
def startup_db():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING)])
    db.attendance_logs.create_index([("department", ASCENDING)])


# --- Helper functions for rules ---

def to_ms(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


def from_ms(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=IST)


def round_half_up(val, decimals=2):
    return float(Decimal(str(val)).quantize(Decimal('1.' + '0' * decimals), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in, shift_start):
    h, m = map(int, shift_start.split(":"))
    start_dt = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    diff = int((punch_in - start_dt).total_seconds() / 60)
    return diff if diff > 10 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600.0, 2)


def compute_overtime(punch_out, shift_end, date_str):
    h, m = map(int, shift_end.split(":"))
    end_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    mins = int((punch_out - end_dt).total_seconds() / 60)
    return mins if mins >= 30 else 0


def serialize_log(doc):
    if not doc:
        return doc
    doc.pop("_id", None)
    doc["punch_in"] = to_ms(doc.get("punch_in"))
    doc["punch_out"] = to_ms(doc.get("punch_out"))
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = to_ms(doc["created_at"])
    
    # Defaults for legacy fields if missing
    doc.setdefault("half_day", False)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("late_minutes", 0)
    
    # Format history audit trail entries
    formatted_history = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        at_ms = to_ms(at_val) if isinstance(at_val, datetime) else at_val
        
        changes = {}
        for k, v in h.get("changes", {}).items():
            if isinstance(v, dict):
                f_val, t_val = v.get("from"), v.get("to")
                if k in ["punch_in", "punch_out"]:
                    f_val = to_ms(f_val) if isinstance(f_val, datetime) else f_val
                    t_val = to_ms(t_val) if isinstance(t_val, datetime) else t_val
                changes[k] = {"from": f_val, "to": t_val}
            else:
                changes[k] = v
        
        formatted_history.append({"at": at_ms, "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    doc["history"] = formatted_history
    return doc


# --- Pydantic Models ---

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --- Endpoints ---
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "DB offline")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    
    doc = body.model_dump()
    created_dt = datetime.now(IST)
    doc["created_at"] = created_dt
    db.employees.insert_one(doc)
    
    resp = body.model_dump()
    resp["created_at"] = to_ms(created_dt)
    return resp


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    
    items = []
    for emp in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size):
        if isinstance(emp.get("created_at"), datetime):
            emp["created_at"] = to_ms(emp["created_at"])
        items.append(emp)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = from_ms(body.punched_at) if body.punched_at else datetime.now(IST)
    d = ts.date().isoformat()
    
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
        
    late_mins = compute_late_minutes(ts, emp.get("shift_start", "09:30"))
    
    doc = {
        "emp_code": body.emp_code,
        "department": emp.get("department", "General"),
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ts = from_ms(body.punched_at) if body.punched_at else datetime.now(IST)
    
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)]
    )
    if not log:
        raise HTTPException(404, "No active punch-in found")
        
    punch_in_dt = log["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=IST)
        
    if ts <= punch_in_dt or (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "Invalid punch-out time window")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    shift_end = emp.get("shift_end", "18:30") if emp else "18:30"
    
    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime_mins = compute_overtime(ts, shift_end, log["date"])
    half_day = work_hours < 4.50

    db.attendance_logs.update_one(
        {"_id": log["_id"]},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime_mins, "half_day": half_day}}
    )

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return serialize_log(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "Attendance record not found")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    old_status = log.get("status")
    old_punch_in = log.get("punch_in")
    old_punch_out = log.get("punch_out")
    old_work_hours = log.get("work_hours")
    old_late = log.get("late_minutes", 0)
    old_overtime = log.get("overtime_minutes", 0)
    old_half_day = log.get("half_day", False)

    new_status = body.status if body.status is not None else old_status
    new_in_dt = from_ms(body.punch_in) if body.punch_in is not None else old_punch_in
    new_out_dt = from_ms(body.punch_out) if body.punch_out is not None else old_punch_out

    if new_status in ["ABSENT", "LEAVE"] and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "Cannot supply punch times for ABSENT or LEAVE")

    if new_status in ["PRESENT", "WFH", "ON_DUTY"] and not new_in_dt:
        raise HTTPException(422, "Presence status requires punch_in")

    update_data = {}
    if body.status is not None:
        update_data["status"] = body.status
    if body.punch_in is not None:
        update_data["punch_in"] = new_in_dt
    if body.punch_out is not None:
        update_data["punch_out"] = new_out_dt

    if new_status in ["ABSENT", "LEAVE"]:
        update_data["punch_in"] = None
        update_data["punch_out"] = None
        new_in_dt = None
        new_out_dt = None

    new_late = compute_late_minutes(new_in_dt, emp.get("shift_start", "09:30")) if new_in_dt else 0
    new_wh = None
    new_ot = 0
    new_hd = False

    if new_in_dt and new_out_dt:
        new_wh = compute_work_hours(new_in_dt, new_out_dt)
        new_ot = compute_overtime(new_out_dt, emp.get("shift_end", "18:30"), date)
        new_hd = new_wh < 4.50

    update_data.update({"late_minutes": new_late, "work_hours": new_wh, "overtime_minutes": new_ot, "half_day": new_hd})

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_in_dt != old_punch_in:
        changes["punch_in"] = {"from": to_ms(old_punch_in), "to": to_ms(new_in_dt)}
    if new_out_dt != old_punch_out:
        changes["punch_out"] = {"from": to_ms(old_punch_out), "to": to_ms(new_out_dt)}
    if new_wh != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_ot}
    if new_hd != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_hd}

    if not changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(IST),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    db.attendance_logs.update_one({"_id": log["_id"]}, {"$set": update_data, "$push": {"history": history_entry}})
    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return serialize_log(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot exceed date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    
    items = [serialize_log(doc) for doc in db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --- Analytics & Explain ---
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., description="YYYY-MM")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$switch": {
                            "branches": [
                                {"case": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "then": {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}}
                            ],
                            "default": 0
                        }
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    data = res[0] if res else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}

    joined_on = emp.get("joined_on")
    year, mnth = map(int, month.split("-"))
    working_days = 0
    start_day = 1
    
    if joined_on:
        j_date = dt_date.fromisoformat(joined_on)
        if j_date.year == year and j_date.month == mnth:
            start_day = j_date.day
        elif (j_date.year, j_date.month) > (year, mnth):
            working_days = 0

    if working_days == 0 and (j_date.year, j_date.month) <= (year, mnth):
        import calendar
        _, last_day = calendar.monthrange(year, mnth)
        for d_num in range(start_day, last_day + 1):
            if dt_date(year, mnth, d_num).weekday() < 5:
                working_days += 1

    attendance_pct = round_half_up((data["present_days"] / working_days) * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": data["present_days"], "leave_days": data["leave_days"],
        "late_count": data["late_count"], "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"], "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., description="YYYY-MM"), department: Optional[str] = None):
    q = {}
    if department:
        q["department"] = department
        
    depts = db.employees.distinct("department", q)
    items = []
    
    for dept in depts:
        headcount = db.employees.count_documents({"department": dept, "joined_on": {"$lte": f"{month}-31"}})
        if headcount == 0:
            continue
            
        pipeline = [
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": dept, "date": {"$regex": f"^{month}"}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$switch": {
                                "branches": [
                                    {"case": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "then": {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}}
                                ],
                                "default": 0
                            }
                        }
                    },
                    "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", None]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}
                }
            }
        ]
        res = list(db.attendance_logs.aggregate(pipeline))
        r = res[0] if res else {"present_days": 0, "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
        
        avg_wh = round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None
        items.append({
            "department": dept, "headcount": headcount, "present_days": r["present_days"],
            "avg_work_hours": avg_wh, "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]
        })
        
    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., description="YYYY-MM"), limit: int = 10, department: Optional[str] = None):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
        
    pipeline.extend([
        {"$project": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1, "_id": 0}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])
    
    rows = list(db.attendance_logs.aggregate(pipeline))
    ranked_items, current_rank, prev_val = [], 1, None
    
    for idx, row in enumerate(rows):
        val = row["total_late_minutes"]
        if idx > 0 and val < prev_val:
            current_rank = idx + 1
        row["rank"] = current_rank
        prev_val = val
        
        if row["rank"] <= limit:
            ranked_items.append(row)

    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    if to < from_:
        raise HTTPException(422, "to date cannot precede from date")
        
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    start_d, end_d = dt_date.fromisoformat(from_), dt_date.fromisoformat(to)
    if (end_d - start_d).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")

    items, curr, rates = [], start_d, []
    while curr <= end_d:
        d_str = curr.isoformat()
        is_wd = curr.weekday() < 5
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": d_str}})
        
        pipeline = [
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department, "date": d_str}},
            {
                "$group": {
                    "_id": None,
                    "present_count": {
                        "$sum": {
                            "$switch": {
                                "branches": [
                                    {"case": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "then": {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}}
                                ],
                                "default": 0
                            }
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
                }
            }
        ]
        res = list(db.attendance_logs.aggregate(pipeline))
        r = res[0] if res else {"present_count": 0, "late_count": 0}

        att_rate = round_half_up((r["present_count"] / headcount), 4) if (is_wd and headcount > 0) else (0.0 if is_wd else None)
        rates.append(att_rate)
        
        valid_rates = [rt for rt in rates[-7:] if rt is not None]
        mov_avg = round_half_up(sum(valid_rates) / len(valid_rates), 4) if valid_rates else None

        items.append({
            "date": d_str, "is_working_day": is_wd, "headcount": headcount,
            "present_count": r["present_count"], "late_count": r["late_count"],
            "attendance_rate": att_rate, "moving_avg_7d": mov_avg
        })
        curr += timedelta(days=1)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, **kwargs):
    if endpoint == "attendance_list":
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": db.attendance_logs.find({"emp_code": "EMP0001"}).explain()}
    elif endpoint == "employee_monthly":
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": db.attendance_logs.aggregate([{"$match": {"emp_code": "EMP0001"}}, {"$group": {"_id": "$emp_code"}}]).explain()}
    else:
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": db.attendance_logs.find().explain()}