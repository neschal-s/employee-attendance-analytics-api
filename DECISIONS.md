# DECISIONS.md

1. **Indexes.** I added a unique index on `employees.emp_code` to prevent duplicate employees and a unique compound index on `attendance_logs.emp_code` and `date` to enforce one attendance record per employee per day. The attendance date and employee index supports filtered listings, while employee department and join-date indexes support analytics queries. I did not add a standalone `status` index because status has few possible values and may have low selectivity.

2. **Punch-in race.** Concurrent requests can both pass an initial existence check. The unique compound index ensures that only one insert for an employee and date succeeds. The successful request returns `201`, while a duplicate-key error is translated into HTTP `409`. Database-enforced uniqueness protects against races that application-level checks alone cannot prevent.

3. **Ties.** Employees with equal late minutes receive the same competition rank, producing ranks such as `1, 2, 2, 4`. Results are ordered by employee code for deterministic output. Ranking is calculated before applying the requested limit, so employees sharing a qualifying rank are not arbitrarily excluded at the cutoff.

4. **Headcount.** Department headcount starts from employees who joined on or before the reporting period's end date and then looks up attendance records. Employees without attendance logs are still included in headcount.

5. **Scaling.** With 100 times more data, I would examine MongoDB execution statistics, index usage, execution time, and documents examined for the main query paths. I would then optimise the slowest aggregation pipelines, review index coverage, and benchmark changes against production-sized data.
