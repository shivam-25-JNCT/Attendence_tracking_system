# DECISIONS.md

### 1. Indexes

I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` and `date` to enforce business-level uniqueness. Additional indexes support department and join-date filtering, attendance date sorting, and employee attendance lookups. I would avoid adding redundant indexes without checking their query benefit because each index increases storage and write costs.

### 2. Punch-in race

Two requests may attempt to create the same attendance record simultaneously. Both can reach the insert, but MongoDB's unique compound index allows only one record for that employee and date. The losing request receives a duplicate-key error, which the API translates into HTTP 409.

### 3. Ties

The leaderboard uses competition ranking, so equal late-minute totals share the same rank and the next rank is skipped. The result limit is applied after ranking, preserving employees tied at the cutoff. Employee code provides deterministic ordering among ties.

### 4. Headcount

The department summary starts from employees rather than attendance records. It can therefore include employees who have joined by the end of the requested month even if they have no attendance logs. Department and joining-date conditions determine the eligible headcount.

### 5. Scaling further

For 100 times the data, I would measure real query plans and execution statistics, review compound-index selectivity, and test aggregation memory and latency on representative data before changing the schema or adding infrastructure.
