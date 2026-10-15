# Design Decisions

1. **Indexes:** I created seven indexes. The unique employee-code index prevents duplicate employees, while the unique employee-code/date index guarantees one attendance record per employee per day. The other indexes support department-filtered employee queries, joining-date calculations, attendance date sorting, employee history lookups, and status/date filtering. I rejected a standalone name index because the API has no name-based lookup requirement, and extra indexes increase storage and write overhead.

2. **Punch-in race:** Two identical requests may both pass the initial existence check. The unique compound index is the final safeguard: one insert succeeds, while the other receives a duplicate-key error and returns `409 Conflict`.

3. **Ties:** The leaderboard uses MongoDB `$rank`. Employees tied at the cutoff share the same rank and are all returned, so the result can exceed `limit`.

4. **Headcount:** The department summary starts from employees and looks up attendance records, so employees without logs are still counted.

5. **100x scale:** I would examine query plans and workload first, then consider pre-aggregating frequently requested analytics to reduce repeated aggregation work.