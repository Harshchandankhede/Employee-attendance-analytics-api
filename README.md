# Employee Attendance & Analytics API

FastAPI + MongoDB backend for HR attendance: punch-in and punch-out with the
business rules applied, manual corrections with an audit trail, and analytics
built as MongoDB aggregation pipelines.

**Stack:** Python 3.11+ · FastAPI · PyMongo · MongoDB 6.0+

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness; 503 when MongoDB does not answer |
| `POST` | `/employees` | Create an employee (`emp_code` unique at the database level) |
| `GET` | `/employees` | List by department, sorted by `emp_code`, paginated |
| `POST` | `/attendance/punch-in` | Open the day's record (R1, R2) |
| `GET` | `/attendance` | Filter by employee, date range, status; sorted and paginated |
| `POST` | `/attendance/punch-out` | Close the record (R3–R5), works across midnight |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manual correction with an audit entry |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET` | `/analytics/departments/summary` | Per-department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Lateness ranking, standard competition ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily series with a 7-day moving average |
| `GET` | `/admin/explain/{endpoint}` | MongoDB query plan for an endpoint's main query |

Business rules R1–R10 are implemented as described in the assignment contract.

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

Three suites, all starting the app as a real subprocess and exercising the
contract against MongoDB. They need `requests` in addition to `requirements.txt`.

```bash
python tests/test_api.py          # CRUD, rules R1-R5, pagination, concurrency
python tests/test_analytics.py    # the four analytics endpoints and explain
python tests/test_explain_plan.py  # index usage against ~80,000 seeded records
```

The sample data is reloaded before and after every run, so a crashed run cannot
leak state into the next. `test_analytics.py` builds the tie, zero-log and
mid-month-joiner cases that the nine sample records cannot show;
`test_explain_plan.py` seeds roughly 80,000 records because the plan for a
collection that small tells you nothing about index usage.

## Design notes

The decisions below were verified against the running service rather than assumed.

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

**`/admin/explain/{endpoint}` proves the queries use indexes.** Each of the five
explainable endpoints has its query built by the same function the real endpoint
calls, so the explained query is the query that actually runs — there is no second
copy that can drift. Against a seeded 80,000-record dataset every plan reports
`IXSCAN` with no `COLLSCAN`.

See `REVIEW.md` for the defects found in the starter code and `DECISIONS.md` for
the reasoning behind the indexes, the concurrency handling and the tie rules.

## Implementation notes

A few behaviours were verified empirically because the obvious implementation is
wrong in each case:

- MongoDB's `$round` rounds half to **even**, so `$round(2.5)` is `2` and
  `$round(1.005, 2)` is `1.0`. Both disagree with the half-up rounding the
  contract requires, so rounding is done in Python.
- `$rank` accepts exactly **one** field in `sortBy`; adding a tie-break there
  splits every tie group and destroys the ranking.
- `$densify`'s upper bound is **exclusive**, so the range end is passed as
  `to + 1 day` to include it.
- `$dayOfWeek` returns `1` for Sunday and `7` for Saturday, so a working day is
  anything outside `[1, 7]`.
- Sort clauses accepted by a `find()` cursor are tuples, but an explain command is
  encoded straight to BSON and needs a plain document.