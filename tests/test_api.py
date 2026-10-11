"""
Local test suite for the CRUD half of the contract.

Not part of the submission (the brief asks for app/main.py plus the three
documents), but it is how the work was verified. Run it with a MongoDB
reachable at $MONGO_URI:

    .venv\\Scripts\\python.exe tests\\test_api.py

It resets the database to the nine seeded sample documents on the way in and
removes everything it creates on the way out.
"""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PORT = int(os.getenv("TEST_PORT", "8010"))
BASE = f"http://127.0.0.1:{PORT}"
IST = timezone(timedelta(hours=5, minutes=30))
SAMPLE_DATES = 9

PASS, FAIL = [], []
_local = threading.local()


def session():
    """One Session per thread. A Session is not thread safe, and sharing one
    across the concurrency tests leaves connections in a bad state."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


def ist_ms(y, mo, d, h, mi, s=0):
    """IST wall clock -> epoch milliseconds."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail and not condition else ""))
    return condition


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


class Server:
    """Runs the app under test as a real subprocess, like the grader does.

    Output goes to a file rather than a pipe: a Windows pipe buffer is only a
    few KB, so a server whose stdout is piped and never drained blocks on write
    partway through a long test run and stops answering requests.
    """

    def __init__(self):
        self.proc = None
        self.log_path = ROOT / "tests" / "server.log"
        self.log = ""

    def __enter__(self):
        self.handle = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
            cwd=ROOT, stdout=self.handle, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                    return self
            except requests.RequestException:
                time.sleep(0.4)
        raise RuntimeError("server did not become ready")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.handle.close()
        self.log = self.log_path.read_text(encoding="utf-8", errors="replace")


def main():
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[
        os.getenv("MONGO_DB", "attendance_db")
    ]
    reset(db)

    with Server() as server:
        s = session()

        section("health and readiness")
        r = s.get(f"{BASE}/health", timeout=10)
        check("GET /health returns 200", r.status_code == 200, r.text)
        check('body is {"status": "ok"}', r.json() == {"status": "ok"}, r.text)

        section("employees: R10 pagination and sorting")
        r = s.get(f"{BASE}/employees", params={"page": 1, "page_size": 3}, timeout=10)
        body = r.json()
        codes = [e["emp_code"] for e in body["items"]]
        check("page 1 starts at the first employee", codes[0] == "EMP0001", str(codes))
        check("sorted by emp_code ascending", codes == sorted(codes), str(codes))
        check("total counts the whole collection", body["total"] == 6, str(body["total"]))
        r2 = s.get(f"{BASE}/employees", params={"page": 2, "page_size": 3}, timeout=10).json()
        check("page 2 does not repeat page 1",
              not set(codes) & {e["emp_code"] for e in r2["items"]})
        r3 = s.get(f"{BASE}/employees", params={"department": "Support"}, timeout=10).json()
        check("total respects the department filter", r3["total"] == 2, str(r3["total"]))
        check("created_at is epoch millis, not an ISO string",
              isinstance(r3["items"][0]["created_at"], int))
        check("_id never appears in a response", "_id" not in json.dumps(r3["items"][0]))
        check("page_size above 100 -> 422",
              s.get(f"{BASE}/employees", params={"page_size": 101}, timeout=10).status_code == 422)
        check("page 0 -> 422",
              s.get(f"{BASE}/employees", params={"page": 0}, timeout=10).status_code == 422)

        section("employees: create and uniqueness")
        dup = {"emp_code": "EMP0001", "name": "Dup", "email": "d@e.com",
               "department": "Engineering", "joined_on": "2026-01-01"}
        check("duplicate emp_code -> 409",
              s.post(f"{BASE}/employees", json=dup, timeout=10).status_code == 409)
        bad_code = {**dup, "emp_code": "ABC"}
        check("emp_code not matching the pattern -> 422",
              s.post(f"{BASE}/employees", json=bad_code, timeout=10).status_code == 422)
        bad_email = {**dup, "emp_code": "EMP9001", "email": "not-an-email"}
        check("malformed email -> 422",
              s.post(f"{BASE}/employees", json=bad_email, timeout=10).status_code == 422)
        same = {"emp_code": "EMP9002", "name": "Same", "email": "s@e.com",
                "department": "Eng", "shift_start": "09:00", "shift_end": "09:00",
                "joined_on": "2026-01-01"}
        check("shift_start == shift_end -> 422",
              s.post(f"{BASE}/employees", json=same, timeout=10).status_code == 422)
        overnight = {"emp_code": "EMP9003", "name": "Night", "email": "n@e.com",
                     "department": "Support", "shift_start": "22:00", "shift_end": "06:00",
                     "joined_on": "2026-01-01"}
        r = s.post(f"{BASE}/employees", json=overnight, timeout=10)
        check("overnight shift is accepted", r.status_code == 201, r.text)
        check("created_at is set by the server", isinstance(r.json()["created_at"], int))

        section("punch-in: R1 attendance date and R2 lateness")
        cases = [
            # emp_code, date, IST punch-in, expected date, expected late_minutes
            ("EMP0001", (2026, 8, 3, 9, 28), "2026-08-03", 0),
            ("EMP0001", (2026, 8, 4, 9, 40, 0), "2026-08-04", 0),   # R2 boundary: not late
            ("EMP0001", (2026, 8, 5, 9, 40, 1), "2026-08-05", 10),  # R2 boundary: late by 10
            ("EMP0001", (2026, 8, 6, 10, 15, 59), "2026-08-06", 45),
            ]
        # Overnight shift (shift 22:00-06:00). Each case gets its own shift
        # start so the derived attendance dates cannot collide, and the expected
        # late_minutes is measured from the 22:00 the shift began, not from
        # midnight: a 05:59 punch-in on an overnight shift is very late.
        overnight_cases = [
            # IST punch-in,            attendance date, late_minutes from 22:00
            ((2026, 8, 10, 21, 55), "2026-08-10", 0),      # 5 min before shift start
            ((2026, 8, 12, 2, 0), "2026-08-11", 240),      # 4h after a 22:00 start
            ((2026, 8, 14, 5, 59, 59), "2026-08-13", 479), # just before 06:00, very late
            ((2026, 8, 16, 6, 0), "2026-08-16", 0),        # exactly at 06:00: new day, new shift
        ]
        for ts, want_date, want_late in overnight_cases:
            r = s.post(f"{BASE}/attendance/punch-in",
                       json={"emp_code": "EMP0005", "punched_at": ist_ms(*ts)}, timeout=10)
            body = r.json() if r.status_code == 201 else {}
            check(f"EMP0005 overnight punch-in {ts[3]:02d}:{ts[4]:02d} "
                  f"-> date {want_date}, late {want_late}",
                  r.status_code == 201 and body.get("date") == want_date
                  and body.get("late_minutes") == want_late,
                  f"got {r.status_code} {body.get('date')} late={body.get('late_minutes')}")
        for emp, ts, want_date, want_late in cases:
            r = s.post(f"{BASE}/attendance/punch-in",
                       json={"emp_code": emp, "punched_at": ist_ms(*ts)}, timeout=10)
            ok = r.status_code == 201
            body = r.json() if ok else {}
            check(f"{emp} punch-in {ts[3]:02d}:{ts[4]:02d}:{ts[5] if len(ts) > 5 else 0:02d} "
                  f"-> date {want_date}, late {want_late}",
                  ok and body.get("date") == want_date and body.get("late_minutes") == want_late,
                  f"got {r.status_code} {body.get('date')} late={body.get('late_minutes')}")

        r = s.post(f"{BASE}/attendance/punch-in",
                   json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 20, 9, 0)}, timeout=10)
        check("second punch-in for the same date -> 409",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 20, 9, 0)},
                     timeout=10).status_code == 409)
        check("unknown employee -> 404",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP9999"}, timeout=10).status_code == 404)
        check("status ABSENT on punch-in -> 422",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP0001", "status": "ABSENT"}, timeout=10).status_code == 422)
        check("punched_at in seconds -> 422",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP0001", "punched_at": 1783312500},
                     timeout=10).status_code == 422)
        check("punched_at as a float -> 422",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP0001", "punched_at": 1783312500000.5},
                     timeout=10).status_code == 422)
        check("punched_at as a string -> 422",
              s.post(f"{BASE}/attendance/punch-in",
                     json={"emp_code": "EMP0001", "punched_at": "1783312500000"},
                     timeout=10).status_code == 422)

        section("punch-out: R3, R4, R5")
        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 24, 10, 5)}, timeout=10)
        r = s.post(f"{BASE}/attendance/punch-out",
                   json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 24, 19, 10)}, timeout=10)
        body = r.json()
        check("late 35min punch-in is R2-correct", body.get("late_minutes") == 35, str(body))
        check("work_hours 9.08 (R4 half-up)", body.get("work_hours") == 9.08, str(body.get("work_hours")))
        check("overtime 40 (R3, at or above 30)", body.get("overtime_minutes") == 40)
        check("half_day false", body.get("half_day") is False)

        check("punch-out on a closed record -> 409",
              s.post(f"{BASE}/attendance/punch-out",
                     json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 24, 20, 0)},
                     timeout=10).status_code == 409)
        check("punched_at before punch_in -> 422",
              s.post(f"{BASE}/attendance/punch-out",
                     json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 24, 9, 0)},
                     timeout=10).status_code == 422)
        check("punched_out beyond 24 hours -> 422",
              s.post(f"{BASE}/attendance/punch-out",
                     json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 26, 9, 0)},
                     timeout=10).status_code == 422)
        check("punch-out for an unknown employee -> 404",
              s.post(f"{BASE}/attendance/punch-out",
                     json={"emp_code": "EMP9999"}, timeout=10).status_code == 404)
        check("punch-out with no record at all -> 404",
              s.post(f"{BASE}/attendance/punch-out",
                     json={"emp_code": "EMP9004"}, timeout=10).status_code == 404)

        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0004", "punched_at": ist_ms(2026, 8, 25, 9, 30)}, timeout=10)
        body = s.post(f"{BASE}/attendance/punch-out",
                      json={"emp_code": "EMP0004", "punched_at": ist_ms(2026, 8, 25, 13, 0)},
                      timeout=10).json()
        check("3.5 hour day -> half_day true (R5)", body.get("half_day") is True, str(body))
        check("3.5 hour day -> work_hours 3.5", body.get("work_hours") == 3.5)

        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 26, 9, 30)}, timeout=10)
        body = s.post(f"{BASE}/attendance/punch-out",
                      json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 26, 14, 0)},
                      timeout=10).json()
        check("exactly 4.50 hours -> half_day false (R5 boundary)",
              body.get("work_hours") == 4.5 and body.get("half_day") is False, str(body))
        body = s.post(f"{BASE}/attendance/punch-out",
                      json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 26, 15, 0)},
                      timeout=10)
        check("5.00 hours -> half_day false", body.status_code in (404, 409))

        section("punch-out: overnight shift across midnight")
        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0005", "punched_at": ist_ms(2026, 8, 27, 21, 55)}, timeout=10)
        r = s.post(f"{BASE}/attendance/punch-out",
                   json={"emp_code": "EMP0005", "punched_at": ist_ms(2026, 8, 28, 6, 40)}, timeout=10)
        body = r.json()
        check("record stays on the day the shift started (R1)",
              body.get("date") == "2026-08-27", str(body.get("date")))
        check("overnight work_hours 8.75", body.get("work_hours") == 8.75, str(body.get("work_hours")))
        check("overnight overtime 40", body.get("overtime_minutes") == 40)

        section("concurrency")
        db.attendance_logs.delete_many({"emp_code": "EMP0002", "date": "2026-08-28"})
        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0002", "punched_at": ist_ms(2026, 8, 28, 9, 30)}, timeout=10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(
                lambda _: session().post(f"{BASE}/attendance/punch-out",
                                        json={"emp_code": "EMP0002",
                                              "punched_at": ist_ms(2026, 8, 28, 18, 45)},
                                        timeout=30).status_code,
                range(8)))
        check("8 parallel punch-outs: exactly one 200", codes.count(200) == 1, str(sorted(codes)))
        check("8 parallel punch-outs: the rest are 409", codes.count(409) == 7, str(sorted(codes)))
        check("no 500 under concurrency", 500 not in codes, str(sorted(codes)))

        db.attendance_logs.delete_many({"emp_code": "EMP0002", "date": "2026-08-29"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(
                lambda _: session().post(f"{BASE}/attendance/punch-in",
                                        json={"emp_code": "EMP0002",
                                              "punched_at": ist_ms(2026, 8, 29, 9, 30)},
                                        timeout=30).status_code,
                range(8)))
        check("8 parallel punch-ins: exactly one 201", codes.count(201) == 1, str(sorted(codes)))
        check("8 parallel punch-ins: the rest are 409", codes.count(409) == 7, str(sorted(codes)))

        section("regularize (PATCH): recompute and audit trail")
        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 30, 10, 5)}, timeout=10)
        s.post(f"{BASE}/attendance/punch-out",
               json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, 30, 18, 20)}, timeout=10)
        before = s.get(f"{BASE}/attendance", params={"emp_code": "EMP0001",
                                                      "date_from": "2026-08-30"}, timeout=10).json()
        check("punch-out computed late_minutes 35 before correction",
              before["items"][0]["late_minutes"] == 35, str(before["items"][0]["late_minutes"]))

        r = s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                    json={"punch_in": ist_ms(2026, 8, 30, 9, 28),
                          "reason": "biometric glitch", "regularized_by": "hr.admin"}, timeout=10)
        body = r.json()
        check("correction recomputes late_minutes to 0 (R2)",
              body.get("late_minutes") == 0, str(body.get("late_minutes")))
        check("correction recomputes work_hours", body.get("work_hours") == 8.87,
              str(body.get("work_hours")))
        check("exactly one history entry appended", len(body.get("history", [])) == 1,
              str(len(body.get("history", []))))
        entry = body["history"][0]
        check("history records who and why",
              entry.get("by") == "hr.admin" and entry.get("reason") == "biometric glitch")
        check("history.at is epoch millis", isinstance(entry.get("at"), int))
        check("nested change values are epoch millis, not datetimes",
              isinstance(entry["changes"]["punch_in"]["to"], int), str(entry["changes"]))
        check("only fields that actually changed are recorded",
              set(entry["changes"]) == {"punch_in", "work_hours", "late_minutes"},
              str(sorted(entry["changes"])))
        check("punch-out was left alone", body.get("punch_out") == before["items"][0]["punch_out"])

        r = s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                    json={"reason": "this changes nothing", "regularized_by": "hr.admin"}, timeout=10)
        check("a correction that changes nothing -> 422", r.status_code == 422, r.text)

        # ABSENT and LEAVE may not carry punch times in the same request.
        for bad_status in ("LEAVE", "ABSENT"):
            check(f"{bad_status} plus punch_in -> 422",
                  s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                          json={"status": bad_status, "punch_in": ist_ms(2026, 8, 30, 9, 0),
                                "reason": "status cannot carry a punch time",
                                "regularized_by": "hr.admin"},
                          timeout=10).status_code == 422)
            check(f"{bad_status} plus punch_out -> 422",
                  s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                          json={"status": bad_status, "punch_out": ist_ms(2026, 8, 30, 20, 0),
                                "reason": "status cannot carry a punch time",
                                "regularized_by": "hr.admin"},
                          timeout=10).status_code == 422)

        r = s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                    json={"status": "LEAVE", "reason": "retroactive approved leave",
                          "regularized_by": "hr.admin"}, timeout=10)
        body = r.json()
        check("LEAVE clears both punch times",
              body.get("punch_in") is None and body.get("punch_out") is None, str(body))
        check("LEAVE clears work_hours", body.get("work_hours") is None)
        check("LEAVE clears the derived counters",
              body.get("late_minutes") == 0 and body.get("overtime_minutes") == 0
              and body.get("half_day") is False, str(body))
        check("history is appended, not replaced", len(body.get("history", [])) == 2,
              f"got {len(body.get('history', []))}")
        check("the second entry records the status change and cleared times",
              body["history"][1]["changes"].get("status") == {"from": "PRESENT", "to": "LEAVE"},
              str(body["history"][1]["changes"].get("status")))
        check("cleared times are recorded as null in the audit trail",
              body["history"][1]["changes"].get("punch_out") == {"from": before["items"][0]["punch_out"], "to": None},
              str(body["history"][1]["changes"].get("punch_out")))

        r = s.patch(f"{BASE}/attendance/EMP0001/2026-08-30",
                    json={"status": "PRESENT", "reason": "cannot be present without a punch",
                          "regularized_by": "hr.admin"}, timeout=10)
        check("a presence status with no punch_in -> 422", r.status_code == 422, r.text)

        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 31, 10, 0)}, timeout=10)
        s.post(f"{BASE}/attendance/punch-out",
               json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 8, 31, 18, 0)}, timeout=10)
        r = s.patch(f"{BASE}/attendance/EMP0003/2026-08-31",
                    json={"punch_in": ist_ms(2026, 9, 1, 9, 0),
                          "reason": "wrong attendance date", "regularized_by": "hr.admin"}, timeout=10)
        check("punch_in on a different attendance date -> 422 (R1)", r.status_code == 422, r.text)

        r = s.patch(f"{BASE}/attendance/EMP0003/2026-08-31",
                    json={"punch_out": ist_ms(2026, 8, 31, 9, 0),
                          "reason": "punch out before punch in", "regularized_by": "hr.admin"},
                    timeout=10)
        check("punch_out before punch_in -> 422", r.status_code == 422, r.text)

        r = s.patch(f"{BASE}/attendance/EMP0003/2026-08-31",
                    json={"punch_out": ist_ms(2026, 9, 2, 9, 0),
                          "reason": "more than a day later", "regularized_by": "hr.admin"}, timeout=10)
        check("punch_out beyond 24 hours -> 422", r.status_code == 422, r.text)

        check("correction for an unknown employee -> 404",
              s.patch(f"{BASE}/attendance/EMP9999/2026-08-31",
                      json={"reason": "nobody here", "regularized_by": "hr.admin"},
                      timeout=10).status_code == 404)
        check("correction for a date with no record -> 404",
              s.patch(f"{BASE}/attendance/EMP0003/2026-08-14",
                      json={"reason": "no record that day", "regularized_by": "hr.admin"},
                      timeout=10).status_code == 404)
        check("reason under 5 characters -> 422",
              s.patch(f"{BASE}/attendance/EMP0003/2026-08-31",
                      json={"reason": "x", "regularized_by": "hr.admin"},
                      timeout=10).status_code == 422)
        # Derived fields are read-only: sending one is ignored, and the API
        # still recomputes it from the punch times.
        r = s.patch(f"{BASE}/attendance/EMP0003/2026-08-31",
                    json={"punch_in": ist_ms(2026, 8, 31, 9, 25),
                          "late_minutes": 999, "work_hours": 99.9, "half_day": True,
                          "overtime_minutes": 999,
                          "reason": "derived fields are ignored not rejected",
                          "regularized_by": "hr.admin"}, timeout=10)
        body = r.json()
        check("derived fields in the body are ignored, not rejected",
              r.status_code == 200, r.text)
        check("late_minutes recomputed from the punch time, not taken from the body",
              body.get("late_minutes") == 0, str(body.get("late_minutes")))
        check("work_hours recomputed, not taken from the body",
              body.get("work_hours") == 8.58, str(body.get("work_hours")))
        check("half_day recomputed, not taken from the body", body.get("half_day") is False,
              str(body.get("half_day")))

        section("regularize: no history is lost under concurrent corrections")
        db.attendance_logs.delete_many({"emp_code": "EMP0004", "date": "2026-09-01"})
        s.post(f"{BASE}/attendance/punch-in",
               json={"emp_code": "EMP0004", "punched_at": ist_ms(2026, 9, 1, 10, 0)}, timeout=10)
        s.post(f"{BASE}/attendance/punch-out",
               json={"emp_code": "EMP0004", "punched_at": ist_ms(2026, 9, 1, 18, 0)}, timeout=10)
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(
                lambda h: session().patch(f"{BASE}/attendance/EMP0004/2026-09-01",
                                  json={"punch_in": ist_ms(2026, 9, 1, 9, 25 + h),
                                        "reason": f"concurrent correction {h}",
                                        "regularized_by": "hr.admin"},
                                  timeout=30).status_code,
                range(6)))
        stored = db.attendance_logs.find_one({"emp_code": "EMP0004", "date": "2026-09-01"})
        entries = len(stored.get("history") or [])
        check("every successful correction kept its history entry",
              entries == codes.count(200), f"history={entries} 200s={codes.count(200)}")
        check("no 500 under concurrent corrections", 500 not in codes, str(sorted(codes)))
        print(f"        (responses: {sorted(codes)}, history entries stored: {entries})")

        section("serialization of legacy and edge records")
        legacy = s.get(f"{BASE}/attendance",
                       params={"emp_code": "EMP0002", "date_from": "2026-07-14",
                               "date_to": "2026-07-14"},
                       timeout=10).json()["items"][0]
        check("a record stored without history still returns history: []",
              legacy["history"] == [], str(legacy.get("history")))
        check("a record stored without half_day still returns half_day: false",
              legacy["half_day"] is False, str(legacy.get("half_day")))

        absent = s.get(f"{BASE}/attendance",
                       params={"emp_code": "EMP0004", "status": "ABSENT"},
                       timeout=10).json()["items"][0]
        check("ABSENT record has null punch times",
              absent["punch_in"] is None and absent["punch_out"] is None, str(absent))
        check("ABSENT record has null work_hours", absent["work_hours"] is None)
        check("a seeded record with history keeps its entry",
              len(s.get(f"{BASE}/attendance",
                        params={"emp_code": "EMP0001", "date_from": "2026-07-07",
                                "date_to": "2026-07-07"},
                        timeout=10).json()["items"][0]["history"]) == 1)
        check("an open record reports null work_hours",
              s.get(f"{BASE}/attendance", params={"emp_code": "EMP0006"},
                   timeout=10).json()["items"][0]["work_hours"] is None)
        r = s.get(f"{BASE}/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"},
                  timeout=10)
        check("date_from after date_to -> 422", r.status_code == 422)
        r = s.get(f"{BASE}/attendance", params={"status": "NOPE"}, timeout=10)
        check("unknown status filter -> 422", r.status_code == 422)
        r = s.get(f"{BASE}/attendance", params={"page_size": 2, "page": 1}, timeout=10).json()
        check("attendance is sorted by date descending",
              [i["date"] for i in r["items"]] == sorted((i["date"] for i in r["items"]), reverse=True),
              str([i["date"] for i in r["items"]]))
        check("total reflects the filters", r["total"] >= len(r["items"]))

        log = server.log
    reset(db)

    print(f"\n{'=' * 68}")
    print(f"passed {len(PASS)}, failed {len(FAIL)}")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
    if "500 Internal Server Error" in log:
        print("\nWARNING: the server logged a 500")
    print("=" * 68)
    return 1 if FAIL else 0


def reset(db):
    """Reload the nine seeded sample documents, so a crashed earlier run
    cannot leak extra employees or records into the next one."""
    here = ROOT / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(encoding="utf-8"),
                               json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)


if __name__ == "__main__":
    sys.exit(main())