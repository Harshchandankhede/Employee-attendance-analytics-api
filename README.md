# Employee Attendance & Analytics API

FastAPI + MongoDB backend for HR attendance: punch-in and punch-out with the
business rules applied, manual corrections with an audit trail, and analytics
built as MongoDB aggregation pipelines.

## Running it

Requires Python 3.11+ and MongoDB 6.0 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db

uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a `.env` file is loaded
for convenience but real environment variables take priority. All indexes are
created on startup, so there is no migration step.

Interactive API docs are at `http://localhost:8000/docs`.

## Tests

`tests/test_api.py` runs the app as a subprocess and exercises the contract
against MongoDB. It needs `requests` in addition to `requirements.txt`.

```bash
python tests/test_api.py
```

The sample data from the candidate kit is reloaded before and after each run.

## Design notes

**All instants cross the API boundary as epoch milliseconds** and are stored as
BSON dates. The Mongo client is built with `tz_aware=True`, so every datetime read
from the database knows it is UTC and naive/aware comparisons cannot happen.
Attendance dates are IST calendar dates, including the overnight-shift rule: a
punch-in earlier in the day than `shift_end` belongs to the previous day's shift.

**Rounding happens in Python, never with `$round`.** MongoDB's `$round` rounds
half to even, so `$round(2.5)` is `2` and `$round(1.005, 2)` is `1.0`. Both
disagree with the half-up rule R8 requires, and the same float error that makes
`round(2.675, 2)` return `2.67` in Python affects `$avg` results too. Pipelines
therefore return unrounded numbers and the endpoint rounds once on the way out.

**Concurrency is enforced by the database, not by application checks.** A unique
index on `(emp_code, date)` guarantees one attendance record per day, so
simultaneous punch-ins cannot both succeed. Punch-out updates a record only while
`punch_out` is still null, so two simultaneous punch-outs produce exactly one 200.
A correction writes through an update filtered on the history length it read, so
two concurrent corrections cannot overwrite each other's audit entries.

**Analytics read the stored derived values** (`work_hours`, `late_minutes`,
`overtime_minutes`, `half_day`) rather than recomputing them, since the data model
states the stored values are authoritative. The department summary joins *from*
`employees` onto `attendance_logs` with `preserveNullAndEmptyArrays`, which is what
keeps employees with no records in the headcount. The leaderboard ranks with
`$rank` for standard competition ranking (1, 2, 2, 4) and applies `limit` after
ranking, so a tie group at the cutoff is returned in full. The trend uses
`$densify` to produce one row per calendar day, including weekends and days with
no records, without a Python loop.

See `REVIEW.md` for the defects found in the starter code and `DECISIONS.md` for
the reasoning behind the indexes, the concurrency handling and the tie rules.