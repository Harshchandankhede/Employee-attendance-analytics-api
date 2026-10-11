# DECISIONS.md

**1. Indexes.** A unique index on `employees.emp_code` enforces uniqueness; a
duplicate lookup would not survive concurrency. On `attendance_logs`, a unique
index on `(emp_code, date)` gives one record per day and settles punch-in races,
`(date, emp_code)` serves the sorted list, and `(emp_code, punch_in)` serves
punch-out. I rejected an index on `name` — nothing searches it.

**2. Punch-in race.** Both requests read the employee, compute the date, then call
`insert_one`. One succeeds; the unique index makes the other raise, which I turn
into 409. So exactly one gets 201. I never check-then-insert, since both can pass
a check before either writes.

**3. Ties.** Ranking uses `$rank`, which numbers rows by how many precede them, so
ties share a rank and the next is skipped (1, 2, 2, 4). `limit` is applied
*afterwards*, matching `rank <= limit`. Employees tied at the cutoff both appear, so
the response can exceed `limit` — cutting a tie would be arbitrary.

**4. Headcount with zero logs.** The pipeline starts from `employees` and `$lookup`s
the logs onto each person, not the reverse, and `$unwind` keeps empty arrays. Join
direction is the point: starting from the logs would drop them. `$sum: 1` per row
is the headcount.

**5. At 100× the data.** Denormalise department and shift onto the attendance record
so analytics need no `$lookup`, then precompute daily aggregates.