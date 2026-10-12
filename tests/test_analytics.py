"""
Tests for the four analytics endpoints and the explain endpoint.

Run:  .venv\\Scripts\\python.exe tests\\test_analytics.py

Each section reloads the nine sample documents, checks the endpoint against
values worked out by hand from DATA_MODEL.md, and restores the samples on the
way out. The leaderboard and explain tests add extra records, because the sample
data cannot exercise ties and cannot show an IXSCAN against a COLLSCAN.
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
PORT = int(__import__("os").environ.get("ANALYTICS_TEST_PORT", "8016"))
BASE = f"http://127.0.0.1:{PORT}"
IST = timezone(timedelta(hours=5, minutes=30))
OPTS = json_util.JSONOptions(tz_aware=True)
db = MongoClient("mongodb://localhost:27017", tz_aware=True)[
    __import__("os").environ.get("MONGO_DB", "attendance_db")]

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail and not cond else ""))


def section(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def restore_samples():
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads(
            (ROOT / "sample_data" / f"{name}.json").read_text(encoding="utf-8"),
            json_options=OPTS)
        db[name].delete_many({})
        db[name].insert_many(docs)


def ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def add_emp(code, dept, joined="2026-01-01"):
    db.employees.insert_one({"emp_code": code, "name": f"Person {code[-2:]}",
                             "email": f"{code}@e.com", "department": dept,
                             "shift_start": "09:30", "shift_end": "18:30",
                             "joined_on": joined, "created_at": ist(2026, 1, 1, 9, 0)})


def add_log(code, day, late):
    db.attendance_logs.insert_one({
        "emp_code": code, "date": day, "status": "PRESENT",
        "punch_in": ist(2026, 7, int(day[-2:]), 10, 30),
        "punch_out": ist(2026, 7, int(day[-2:]), 19, 0),
        "work_hours": 8.5, "late_minutes": late, "overtime_minutes": 0,
        "half_day": False, "history": []})


def main():
    restore_samples()

    # Output goes to a file, not a pipe: a Windows pipe buffer is a few KB, so a
    # server whose stdout is piped and never drained blocks mid-run.
    log_path = ROOT / "tests" / "analytics_server.log"
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.4)

        section("employee monthly (expected values read from the sample records)")
        # July 2026 has 23 weekdays. EMP0002 joined 07-13, so only weekdays from
        # the 13th count for them.
        EXPECTED = {
            "EMP0001": dict(working_days=23, present_days=2, leave_days=0, late_count=0,
                            total_late_minutes=0, total_overtime_minutes=0, attendance_pct=8.7),
            "EMP0002": dict(working_days=15, present_days=1, leave_days=0, late_count=0,
                            total_late_minutes=0, total_overtime_minutes=0, attendance_pct=6.67),
            "EMP0003": dict(working_days=23, present_days=1, leave_days=1, late_count=1,
                            total_late_minutes=35, total_overtime_minutes=40, attendance_pct=4.35),
            "EMP0004": dict(working_days=23, present_days=0.5, leave_days=0, late_count=0,
                            total_late_minutes=0, total_overtime_minutes=0, attendance_pct=2.17),
            "EMP0005": dict(working_days=23, present_days=1, leave_days=0, late_count=0,
                            total_late_minutes=0, total_overtime_minutes=40, attendance_pct=4.35),
            "EMP0006": dict(working_days=23, present_days=1, leave_days=0, late_count=0,
                            total_late_minutes=0, total_overtime_minutes=0, attendance_pct=4.35),
        }
        for emp, want in EXPECTED.items():
            r = requests.get(f"{BASE}/analytics/employees/{emp}/monthly",
                             params={"month": "2026-07"}, timeout=20)
            check(f"{emp} returns 200", r.status_code == 200, r.text[:120])
            got = r.json()
            for key, expected in want.items():
                actual = got.get(key)
                ok = (abs(actual - expected) < 1e-9 if isinstance(expected, float)
                      else actual == expected)
                check(f"{emp}.{key} = {expected}", ok, f"got {actual}")
        r = requests.get(f"{BASE}/analytics/employees/EMP9999/monthly",
                         params={"month": "2026-07"}, timeout=20)
        check("unknown employee -> 404", r.status_code == 404)
        for bad in ("2026-13", "202607", "abc"):
            r = requests.get(f"{BASE}/analytics/employees/EMP0001/monthly",
                             params={"month": bad}, timeout=20)
            check(f"month={bad!r} -> 422", r.status_code == 422, str(r.status_code))

        section("department summary (R9 headcount and mean over records)")
        r = requests.get(f"{BASE}/analytics/departments/summary",
                         params={"month": "2026-07"}, timeout=20).json()
        got = {i["department"]: i for i in r["items"]}
        order = [i["department"] for i in r["items"]]
        check("departments sorted ascending", order == sorted(order), str(order))
        DEPT = {
            # mean of 9.12, 9.03, 8.98 = 9.04
            "Engineering": dict(headcount=2, present_days=3, avg_work_hours=9.04, late_count=0,
                                total_late_minutes=0, leave_count=0, on_duty_count=0),
            # mean of 9.08 and 3.5 = 6.29; the WFH half day counts 0.5
            "Sales": dict(headcount=2, present_days=1.5, avg_work_hours=6.29, late_count=1,
                          total_late_minutes=35, leave_count=1, on_duty_count=0),
            # only EMP0005 has work_hours; EMP0006 is an open record (null)
            "Support": dict(headcount=2, present_days=2, avg_work_hours=8.75, late_count=0,
                            total_late_minutes=0, leave_count=0, on_duty_count=0),
        }
        for dept, want in DEPT.items():
            for key, expected in want.items():
                actual = got.get(dept, {}).get(key)
                ok = (abs(actual - expected) < 1e-9 if isinstance(expected, float)
                      else actual == expected)
                check(f"{dept}.{key} = {expected}", ok, f"got {actual}")

        add_emp("EMP0090", "Marketing")
        r = requests.get(f"{BASE}/analytics/departments/summary",
                         params={"month": "2026-07"}, timeout=20).json()
        mkt = next((i for i in r["items"] if i["department"] == "Marketing"), None)
        check("a department with zero logs still appears",
              mkt is not None and mkt["headcount"] == 1 and mkt["present_days"] == 0
              and mkt["avg_work_hours"] is None, json.dumps(mkt))
        db.employees.delete_one({"emp_code": "EMP0090"})

        add_emp("EMP0091", "Legal", joined="2026-09-01")
        r = requests.get(f"{BASE}/analytics/departments/summary",
                         params={"month": "2026-07"}, timeout=20).json()
        check("someone who joins after the month is not counted",
              "Legal" not in [i["department"] for i in r["items"]])
        db.employees.delete_one({"emp_code": "EMP0091"})

        # January: only EMP0001 (joined 2026-01-05) had joined by 31 Jan, so
        # Engineering has headcount 1 and the other departments are omitted.
        r = requests.get(f"{BASE}/analytics/departments/summary",
                         params={"month": "2026-01"}, timeout=20).json()
        jan = [i["department"] for i in r["items"]]
        check("only Engineering had joined by 31 January",
              jan == ["Engineering"] and r["items"][0]["headcount"] == 1, json.dumps(r["items"]))
        # December 2025 predates every join date, so nobody counts anywhere.
        r = requests.get(f"{BASE}/analytics/departments/summary",
                         params={"month": "2025-12"}, timeout=20).json()
        check("a month before anyone joined omits every department",
              r["items"] == [], json.dumps(r["items"]))

        section("late leaderboard (competition ranking and limit after ranking)")
        for code, total in [("EMP0100", 60), ("EMP0101", 40), ("EMP0102", 40),
                            ("EMP0103", 40), ("EMP0104", 20), ("EMP0105", 20)]:
            add_emp(code, "TieDept")
            add_log(code, "2026-07-06", total)

        def lb(**params):
            params.setdefault("month", "2026-07")
            r = requests.get(f"{BASE}/analytics/leaderboard/late", params=params, timeout=20)
            return r.status_code, (r.json() if r.status_code == 200 else {})

        _, body = lb(department="TieDept")
        got = [(i["emp_code"], i["rank"], i["total_late_minutes"]) for i in body["items"]]
        expected = [("EMP0100", 1, 60), ("EMP0101", 2, 40), ("EMP0102", 2, 40),
                    ("EMP0103", 2, 40), ("EMP0104", 5, 20), ("EMP0105", 5, 20)]
        check("ranks are 1,2,2,2,5,5 (competition ranking)", got == expected, str(got))
        _, body = lb(department="TieDept", limit=2)
        check("limit=2 returns all four rows at rank <= 2 (tie not cut)",
              [(i["emp_code"], i["rank"]) for i in body["items"]]
              == [("EMP0100", 1), ("EMP0101", 2), ("EMP0102", 2), ("EMP0103", 2)],
              str([(i["emp_code"], i["rank"]) for i in body["items"]]))
        _, body = lb(department="TieDept", limit=5)
        check("limit=5 returns all six rows", len(body["items"]) == 6, str(len(body["items"])))
        tie_rows = [i for i in lb(department="TieDept")[1]["items"]
                    if i["total_late_minutes"] == 40]
        check("rows within a tie are ordered by emp_code ascending",
              [i["emp_code"] for i in tie_rows] == ["EMP0101", "EMP0102", "EMP0103"])
        _, body = lb(department="Sales")
        check("ranking inside one department",
              len(body["items"]) == 1 and body["items"][0]["emp_code"] == "EMP0003"
              and body["items"][0]["rank"] == 1, json.dumps(body["items"]))

        db.attendance_logs.insert_one({
            "emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": ist(2026, 7, 6, 10, 30), "punch_out": ist(2026, 7, 6, 19, 0),
            "work_hours": 8.5, "late_minutes": 999, "overtime_minutes": 0,
            "half_day": False, "history": []})
        _, body = lb()
        check("a log whose employee does not exist is ignored",
              all(i["emp_code"] != "EMP9999" for i in body["items"]))
        check("every row has a positive total",
              all(i["total_late_minutes"] > 0 for i in body["items"]))

        _, body = lb(month="2026-01")
        check("a month with no lateness returns an empty list", body["items"] == [])
        for bad in ({"limit": 0}, {"limit": 51}, {"month": "2026-13"}):
            code, _ = lb(**bad)
            check(f"{bad} -> 422", code == 422, str(code))

        for code in ("EMP0100", "EMP0101", "EMP0102", "EMP0103", "EMP0104", "EMP0105"):
            db.employees.delete_one({"emp_code": code})
            db.attendance_logs.delete_many({"emp_code": code})

        section("department trend (one row per day, gap filled)")
        r = requests.get(f"{BASE}/analytics/departments/Sales/trend",
                         params={"from": "2026-07-06", "to": "2026-07-15"}, timeout=30)
        check("returns 200", r.status_code == 200, r.text[:150])
        items = r.json()["items"]
        for i in items:
            print(f"      {i['date']} ({date.fromisoformat(i['date']).strftime('%a')}) "
                  f"working={str(i['is_working_day']):5} hc={i['headcount']} "
                  f"present={i['present_count']:<4} late={i['late_count']} "
                  f"rate={i['attendance_rate']} mov={i['moving_avg_7d']}")
        want_days = [(date(2026, 7, 6) + timedelta(days=n)).isoformat() for n in range(10)]
        check("exactly one row per calendar day",
              [i["date"] for i in items] == want_days, str([i["date"] for i in items]))
        by_day = {i["date"]: i for i in items}
        check("Saturday is not a working day",
              by_day["2026-07-11"]["is_working_day"] is False)
        check("Sunday is not a working day",
              by_day["2026-07-12"]["is_working_day"] is False)
        check("weekend rates are null",
              by_day["2026-07-11"]["attendance_rate"] is None
              and by_day["2026-07-12"]["attendance_rate"] is None)
        check("a working day with no records has rate 0.0",
              by_day["2026-07-09"]["attendance_rate"] == 0.0,
              str(by_day["2026-07-09"]["attendance_rate"]))
        check("the half day contributes 0.5 present on 07-07",
              by_day["2026-07-07"]["present_count"] == 0.5,
              str(by_day["2026-07-07"]["present_count"]))
        check("the first row averages only itself",
              abs(by_day["2026-07-06"]["moving_avg_7d"] - 0.5) < 1e-9,
              str(by_day["2026-07-06"]["moving_avg_7d"]))
        check("weekend nulls are excluded from the average, not counted as 0",
              all(i["moving_avg_7d"] is not None for i in items),
              str([i["moving_avg_7d"] for i in items]))

        r = requests.get(f"{BASE}/analytics/departments/Sales/trend",
                         params={"from": "2026-07-11", "to": "2026-07-12"}, timeout=30).json()
        check("a weekend-only range gives null moving averages",
              all(i["moving_avg_7d"] is None for i in r["items"]),
              str([i["moving_avg_7d"] for i in r["items"]]))

        add_emp("EMP0200", "Sales", joined="2026-07-09")
        r = requests.get(f"{BASE}/analytics/departments/Sales/trend",
                         params={"from": "2026-07-06", "to": "2026-07-15"}, timeout=30).json()
        by_day = {i["date"]: i for i in r["items"]}
        check("R9: headcount 2 before a join date", by_day["2026-07-08"]["headcount"] == 2,
              str(by_day["2026-07-08"]["headcount"]))
        check("R9: headcount 3 from the join date", by_day["2026-07-09"]["headcount"] == 3,
              str(by_day["2026-07-09"]["headcount"]))
        db.employees.delete_one({"emp_code": "EMP0200"})

        r = requests.get(f"{BASE}/analytics/departments/Support/trend",
                         params={"from": "2026-07-06", "to": "2026-07-06"}, timeout=30).json()
        check("an overnight record lands on the day the shift started",
              r["items"][0]["present_count"] == 1, json.dumps(r["items"][0]))

        for params, want in [({"from": "2026-07-15", "to": "2026-07-06"}, 422),
                             ({"from": "2026-01-01", "to": "2026-12-31"}, 422),
                             ({"to": "2026-07-15"}, 422),
                             ({"from": "notadate", "to": "2026-07-15"}, 422)]:
            r = requests.get(f"{BASE}/analytics/departments/Sales/trend",
                             params=params, timeout=30)
            check(f"trend {json.dumps(params)} -> {want}", r.status_code == want,
                  str(r.status_code))
        r = requests.get(f"{BASE}/analytics/departments/Sales/trend",
                         params={"from": "2026-03-02", "to": "2026-06-01"}, timeout=30)
        check("a range of exactly 92 days is accepted", r.status_code == 200,
              str(r.status_code))
        r = requests.get(f"{BASE}/analytics/departments/Nope/trend",
                         params={"from": "2026-07-06", "to": "2026-07-15"}, timeout=30)
        check("unknown department -> 404", r.status_code == 404, str(r.status_code))

        section("explain endpoint (on the sample data)")
        r = requests.get(f"{BASE}/admin/explain/attendance_list",
                         params={"emp_code": "EMP0001"}, timeout=30)
        check("attendance_list -> 200", r.status_code == 200, r.text[:150])
        body = r.json()
        check("response names the endpoint", body.get("endpoint") == "attendance_list")
        check("response names the collection",
              body.get("collection") == "attendance_logs", str(body.get("collection")))
        check("response carries a raw explain object", isinstance(body.get("explain"), dict))
        for endpoint, params in [("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                                 ("department_summary", {"month": "2026-07"}),
                                 ("late_leaderboard", {"month": "2026-07"}),
                                 ("department_trend", {"department": "Sales",
                                                       "from": "2026-07-06", "to": "2026-07-08"})]:
            r = requests.get(f"{BASE}/admin/explain/{endpoint}", params=params, timeout=30)
            check(f"{endpoint} -> 200", r.status_code == 200, r.text[:150])
        for endpoint, params in [("nope", {}),
                                 ("employee_monthly", {"month": "2026-07"}),
                                 ("employee_monthly", {"emp_code": "EMP0001"}),
                                 ("department_summary", {}),
                                 ("department_summary", {"month": "2026-13"}),
                                 ("late_leaderboard", {}),
                                 ("department_trend", {"from": "2026-07-06", "to": "2026-07-08"}),
                                 ("attendance_list", {"status": "BOGUS"})]:
            r = requests.get(f"{BASE}/admin/explain/{endpoint}", params=params, timeout=30)
            check(f"explain {endpoint} {json.dumps(params)[:30]} -> 422",
                  r.status_code == 422, str(r.status_code))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            # wait() releases the child's handles; the parent's own file handle
            # must be closed before the file can be removed on Windows.
            proc.wait(timeout=15)
        handle.close()
        restore_samples()
        log_path.unlink(missing_ok=True)

    print(f"\n{'=' * 66}")
    print(f"passed {len(PASS)}, failed {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    print(f"sample records restored: {db.attendance_logs.count_documents({})} (expect 9)")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())