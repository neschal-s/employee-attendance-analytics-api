# DECISIONS.md

1. **Indexes.** I added a unique index on `employees.emp_code` to prevent duplicate employees, and a unique compound index on `attendance_logs.emp_code` and `date` to prevent duplicate daily records. The attendance date and employee index supports filtered listing, while the employee department and join-date indexes help with analytics queries. I considered adding an index on `status`, but status has few possible values, so I did not add a separate one.

2. **Punch-in race.** Both requests try to insert a record for the same employee and date. The unique compound index allows only one insert to succeed. The successful request returns `201`; the other gets a duplicate-key error, which the API converts to `409`. This keeps the check safe even when requests arrive together.

3. **Ties.** Employees with equal late minutes get the same rank. If a tie crosses the limit, all employees with a rank within the limit are returned. They are ordered by employee code for consistent output.

4. **Headcount.** The department summary starts from employees who joined by the end of the month, then looks up attendance records. Employees with no logs are still counted.

5. **Scaling.** For 100 times the data, I would test query plans and timings on a production-sized dataset, then optimise the slowest aggregation pipelines.