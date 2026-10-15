# REVIEW.md

## Defects found in the starter

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | The punch-in is compared with the shift start on the punch-in's calendar date. This does not correctly handle overnight shifts when the punch-in belongs to the previous shift date. | Create an employee with a `22:00–06:00` shift and punch in at `05:00` IST. The result must be calculated against the previous day's shift start. | Resolve the correct shift date for overnight shifts before calculating lateness. |
| 2 | `compute_work_hours()` | Python's built-in `round()` uses ties-to-even rounding, whereas the contract requires half-up rounding to two decimal places. | Test durations whose hour values fall exactly on a two-decimal rounding tie. | Use `Decimal` with `ROUND_HALF_UP` after truncating timestamps to whole seconds. |
| 3 | `compute_overtime()` | The calculation does not correctly move the shift end to the following day for overnight shifts. It also does not enforce the minimum 30-minute overtime threshold. | Test a `22:00–06:00` shift with a punch-out after midnight, then test a punch-out 29 minutes and 30 minutes after shift end. | Resolve the correct shift-end date and return overtime only when the elapsed time is at least 30 minutes, using whole elapsed minutes. |
| 4 | `PunchInIn.status` | The request model allows `ABSENT` and `LEAVE` in its enum, even though punch-in only accepts `PRESENT`, `WFH`, and `ON_DUTY`. | Submit a punch-in request with `status: "LEAVE"` or `status: "ABSENT"`. | Restrict the model to the three presence statuses accepted by the contract. |
| 5 | `EmployeeIn` shift validation | The individual shift times are validated, but the model must also reject identical start and end times. The contract explicitly disallows equal shift times. | Create an employee with `shift_start: "09:30"` and `shift_end: "09:30"`. | Add a model-level validation that rejects equal shift times. |
| 6 | `create_employee()` response | `created_at` is stored as a datetime and returned directly, rather than being serialized as an epoch-millisecond integer as required by the API contract. | Create an employee and inspect the JSON type and value of `created_at`. | Convert the UTC datetime to epoch milliseconds in the response. |
| 7 | `list_attendance()` status validation | The status query parameter is an unrestricted string, so unsupported status values are not rejected by request validation. | Request `/attendance?status=INVALID`. The contract requires invalid enum values to return 422. | Restrict `status` to the contract's defined attendance status enum. |
| 8 | `list_attendance()` date validation | `date.fromisoformat()` alone does not guarantee that the input follows the exact `YYYY-MM-DD` representation required by the API. | Submit a date in an alternative accepted ISO representation and check whether it is rejected. | Validate the exact date format as well as whether the date exists. |

## Areas reviewed and considered correct

- The unique index on `employees.emp_code` is the correct mechanism for enforcing employee-code uniqueness under concurrent creates.
- The unique compound index on `attendance_logs.emp_code` and `attendance_logs.date` is appropriate for enforcing one attendance record per employee per attendance date and preventing duplicate punch-ins.
- `list_employees()` applies filtering, sorting, counting and pagination in MongoDB rather than loading the full collection into Python.
- `list_attendance()` applies its filters, sort and pagination in MongoDB, and checks that the date range is not reversed.
- `health()` pings MongoDB and returns HTTP 503 when a MongoDB error occurs.
