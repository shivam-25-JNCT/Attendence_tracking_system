# REVIEW.md

## Starter Code Review

The original starter had several correctness, validation, performance, and concurrency issues. I reviewed the helper functions and existing endpoints against `openapi.yaml` and `DATA_MODEL.md`.

| # | Where | Defect | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Late calculation did not correctly handle all grace-period boundaries and timezone cases. | Test 09:40:00, 09:40:01, and 10:15:59 for a 09:30 shift. | Apply the strict 10-minute rule and floor elapsed minutes. |
| 2 | `compute_work_hours` | Python's default rounding can use half-even rounding rather than the required half-up rule. | Test a duration at a rounding midpoint. | Use explicit half-up rounding. |
| 3 | `compute_overtime` | Shift-end calculation did not reliably account for overnight shifts and timezone-aware instants. | Test a 22:00–06:00 shift. | Calculate the correct shift-end instant and apply the 30-minute threshold. |
| 4 | Employee creation | Checking for duplicates before inserting is vulnerable to concurrent requests. | Send simultaneous requests with the same `emp_code`. | Enforce uniqueness with a database unique index and handle duplicate-key errors. |
| 5 | Employee input validation | Basic fields did not enforce every documented format and constraint. | Send malformed employee codes, dates, emails, or shift times. | Add contract-aligned Pydantic validation. |
| 6 | Employee creation response | Server timestamps could be returned as datetimes rather than epoch milliseconds. | Inspect `created_at` in the response. | Convert instants to epoch milliseconds at the API boundary. |
| 7 | Punch-in | An unknown employee could be dereferenced without a proper 404 response. | Punch in with an unknown employee code. | Return 404 before accessing employee shift details. |
| 8 | Punch-in timestamp | Timestamp conversion and default-time handling could be incorrect for falsy or non-UTC values. | Test omitted, invalid, and boundary timestamps. | Validate strict epoch milliseconds and normalize instants consistently. |
| 9 | Punch-in concurrency | A separate duplicate check followed by insert cannot guarantee uniqueness under concurrent requests. | Send parallel punch-in requests for the same employee/date. | Use the unique compound index and return 409 on duplicate-key conflicts. |
| 10 | Attendance listing | Loading all matching records and sorting or paginating in Python is inefficient at scale. | Test with a large collection. | Filter, sort, count, skip, and limit in MongoDB. |
| 11 | Attendance listing | Pagination and date-range validation were incomplete. | Try invalid page values and `date_from > date_to`. | Validate query parameters and return 422 for invalid inputs. |
| 12 | Attendance responses | Raw MongoDB documents could expose `_id` or contain datetimes inconsistent with the API contract. | Inspect the JSON response. | Serialize only documented fields and convert timestamps to epoch milliseconds. |
| 13 | Punch-out | The starter did not implement the required complete punch-out behavior. | Call the documented punch-out endpoint. | Find the appropriate open record and atomically close it while computing derived fields. |
| 14 | Regularization | The required correction endpoint and append-only audit trail were missing. | Attempt a documented PATCH request. | Recalculate affected fields and append exactly one history entry per successful correction. |
| 15 | Analytics | The required monthly and department analytics endpoints were missing. | Call the documented analytics paths. | Implement aggregation pipelines following the API contract. |
| 16 | Leaderboard | Correct competition ranking and tie handling were not implemented. | Test tied late-minute totals at the result limit. | Use database-side ranking and apply the limit after ranking. |
| 17 | Department trend | The complete daily series and moving average were not implemented. | Test weekends and dates with no attendance records. | Fill date gaps in the aggregation pipeline and calculate the required moving average. |
| 18 | Explain endpoint | Query execution statistics were not exposed by the starter. | Call a supported explain endpoint. | Run a real explain with execution statistics for the documented query or pipeline. |
| 19 | MongoDB indexes | The starter did not establish all required indexes at startup. | Inspect indexes and run explain plans. | Create idempotent unique and query-supporting indexes. |
| 20 | Error handling | Several missing-resource and conflict cases could return the wrong status or an internal server error. | Test unknown resources, duplicate operations, and invalid inputs. | Return documented 404, 409, and 422 responses. |

## Reviewed Design Choices

- Employee identity is based on `emp_code`, not MongoDB `_id`.
- Attendance records are identified by `(emp_code, date)`.
- Stored derived fields are authoritative for analytics; analytics should not recompute them from timestamps.
- MongoDB aggregation is required for analytics instead of fetching every document into Python.
- Manual corrections belong in the audit history; normal punch-in and punch-out events do not create history entries.

## Verification Notes

Only tests actually executed should be reported as passed. Database integration tests that were skipped because MongoDB was unreachable remain unverified and must be rerun when the database is available.
