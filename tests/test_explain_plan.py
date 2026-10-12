"""
Does /admin/explain report a real index plan?

The nine sample records are far too small to tell an IXSCAN from a COLLSCAN:
at that size MongoDB may prefer a collection scan. This seeds a larger dataset,
checks every explainable endpoint, then restores the samples.

Run:  .venv\\Scripts\\python.exe tests\\test_explain_plan.py
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
PORT = 8017
BASE = f"http://127.0.0.1:{PORT}"
IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient("mongodb://localhost:27017", tz_aware=True)
db = client["attendance_db"]
opts = json_util.JSONOptions(tz_aware=True)

EMPLOYEES = 200
DAYS = 500          # 200 x 500 = 100,000 records
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Legal"]
STATUSES = ["PRESENT"] * 70 + ["WFH"] * 15 + ["ON_DUTY"] * 5 + ["LEAVE"] * 5 + ["ABSENT"] * 5

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def seed_large():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    for name in ("uniq_emp_code", "dept_emp_code", "joined_on", "uniq_emp_date",
                 "date_emp_code", "emp_punch_in", "date_emp_late"):
        for coll in ("employees", "attendance_logs"):
            try:
                db[coll].drop_index(name)
            except Exception:
                pass
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    db.employees.create_index([("joined_on", 1)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True,
                                    name="uniq_emp_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1), ("late_minutes", 1)],
                                    name="date_emp_late")

    emps = []
    for i in range(EMPLOYEES):
        joined = (datetime(2025, 1, 1) + timedelta(days=i * 2)).date()
        emps.append({
            "emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@e.com",
            "department": DEPARTMENTS[i % len(DEPARTMENTS)],
            "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": joined.isoformat(),
            "created_at": datetime(2025, 1, 1, tzinfo=IST),
        })
    db.employees.insert_many(emps)

    start = datetime(2026, 1, 5, tzinfo=IST)   # a Monday
    logs = []
    for i in range(EMPLOYEES):
        emp = emps[i]
        if emp["joined_on"] > (start + timedelta(days=DAYS)).date().isoformat():
            continue
        for d in range(DAYS):
            day = start + timedelta(days=d)
            if day.date() < datetime.fromisoformat(emp["joined_on"]).date():
                continue
            if day.weekday() >= 5 and (i + d) % 3:      # some weekend records
                continue
            status = STATUSES[(i * 7 + d * 3) % len(STATUSES)]
            late = 0 if status != "PRESENT" else ((i * 13 + d * 7) % 60)
            pin = day.replace(hour=9, minute=30) + timedelta(minutes=late)
            open_rec = (i + d) % 17 == 0
            doc = {
                "emp_code": emp["emp_code"], "date": day.date().isoformat(),
                "status": status, "punch_in": pin,
                "punch_out": None if open_rec else day.replace(hour=18, minute=30),
                "work_hours": None if open_rec else round(8 + (late % 40) / 100, 2),
                "late_minutes": late,
                "overtime_minutes": 0 if open_rec else (40 if (i + d) % 5 == 0 else 0),
                "half_day": False if open_rec else ((i + d) % 23 == 0),
                "history": [],
            }
            logs.append(doc)
    for chunk in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[chunk:chunk + 5000])
    return db.attendance_logs.count_documents({})


def restore_samples():
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((ROOT / "sample_data" / f"{name}.json").read_text(encoding="utf-8"),
                               json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)


def plan_markers(node, found=None):
    """Collect the stage names MongoDB reports anywhere in the plan tree."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "stage" and isinstance(value, str):
                found.add(value)
            plan_markers(value, found)
    elif isinstance(node, list):
        for item in node:
            plan_markers(item, found)
    return found


def scan_info(explain):
    """Pull COLLSCAN / IXSCAN stage names and the documents examined count."""
    stages = plan_markers(explain)
    docs = 0
    for key in ("executionStats", "executionStages", "stages"):
        def walk(n):
            nonlocal docs
            if isinstance(n, dict):
                if "totalDocsExamined" in n and isinstance(n["totalDocsExamined"], (int, float)):
                    docs = max(docs, n["totalDocsExamined"])
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for i in n:
                    walk(i)
        walk(explain.get(key, {}))
    return stages, docs


CASES = [
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31", "page_size": 20}),
    ("employee_monthly", {"emp_code": "EMP0007", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "department": "Sales"}),
    ("department_trend", {"department": "Sales", "from": "2026-03-01", "to": "2026-03-31"}),
]

total = seed_large()
print(f"seeded {total} attendance records across {EMPLOYEES} employees\n")

log_path = ROOT / "tests" / "explain_server.log"
handle = log_path.open("w", encoding="utf-8")
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                        cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
try:
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.4)

    print(f"{'endpoint':22} {'params':46} {'has IXSCAN':10} {'COLLSCAN':9} docsExamined")
    print("-" * 110)
    for endpoint, params in CASES:
        r = requests.get(f"{BASE}/admin/explain/{endpoint}", params=params, timeout=60)
        if r.status_code != 200:
            check(f"{endpoint} {params}", False, f"HTTP {r.status_code} {r.text[:160]}")
            continue
        body = r.json()
        stages, docs = scan_info(body["explain"])
        has_ix = any("IXSCAN" in s for s in stages)
        has_coll = any("COLLSCAN" in s for s in stages)
        coll_marker = ""
        if has_coll:
            coll_marker = "  <-- COLLSCAN"
        print(f"{endpoint:22} {json.dumps(params)[:44]:46} {str(has_ix):10} "
              f"{str(has_coll):9} {docs:,}{coll_marker}")
        check(f"{endpoint} {json.dumps(params)[:60]}: IXSCAN present", has_ix,
              f"stages={sorted(stages)[:6]}")
        check(f"{endpoint} {json.dumps(params)[:60]}: no COLLSCAN", not has_coll,
              f"stages={sorted(stages)[:6]}")

    print("\n=== the contract shape of the response ===")
    r = requests.get(f"{BASE}/admin/explain/attendance_list",
                     params={"emp_code": "EMP0001"}, timeout=30).json()
    check("has endpoint", r.get("endpoint") == "attendance_list")
    check("has collection", r.get("collection") == "attendance_logs", str(r.get("collection")))
    check("has explain object", isinstance(r.get("explain"), dict))
    check("explain carries executionStats",
          "executionStats" in json.dumps(r["explain"]) or "executionStages" in json.dumps(r["explain"]))

    print("\n=== errors ===")
    for endpoint, params, want in [
        ("nope", {}, 422),
        ("employee_monthly", {"month": "2026-03"}, 422),
        ("employee_monthly", {"emp_code": "EMP0007"}, 422),
        ("late_leaderboard", {"emp_code": "EMP0001"}, 422),
        ("department_summary", {}, 422),
        ("department_trend", {"month": "2026-03"}, 422),
        ("department_trend", {"department": "Sales", "from": "2026-03-01"}, 422),
        ("department_summary", {"month": "2026-13"}, 422),
        ("attendance_list", {"status": "BOGUS"}, 422),
        ("attendance_list", {"date_from": "2026-03-10", "date_to": "2026-03-01"}, 422),
        ("department_trend", {"department": "Sales", "from": "2026-03-10", "to": "2026-03-01"}, 422),
    ]:
        r = requests.get(f"{BASE}/admin/explain/{endpoint}", params=params, timeout=30)
        check(f"{endpoint} {json.dumps(params)[:40]} -> {want}", r.status_code == want,
              f"got {r.status_code}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    restore_samples()
    handle.close()
    log_path.unlink(missing_ok=True)

print(f"\n{'=' * 60}\npassed {len(PASS)}, failed {len(FAIL)}")
for f in FAIL:
    print("  - " + f)
print(f"records after restore: {db.attendance_logs.count_documents({})} (expect 9)")
print("=" * 60)