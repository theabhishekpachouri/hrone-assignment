# DECISIONS.md

1. **Indexes**: Created unique indexes on `emp_code` for employees and `(emp_code, date)` for attendance logs to stop duplicate entries. Also added indexes on `date` and `department` because filtering and grouping without them on 100k rows would crawl. Didn't index fields like work_hours to save write performance.

2. **Punch-in race**: When two identical punch-ins come in together, both check the db, find nothing, and try to insert. MongoDB’s unique index catches the duplicate and throws an error, which we catch and send back as a 409 Conflict. 

3. **Ties**: Used standard ranking (1, 2, 2, 4). If a tie happens right at the limit cutoff, we include all tied employees instead of randomly dropping someone, and sort them by `emp_code` to keep it deterministic.

4. **Headcount**: The department summary pipeline queries the `employees` collection first (matching department and join date), not the attendance logs. This ensures people with zero logs still show up in the headcount.

5. **100x scale**: For 100x data, I'd throw in some Redis caching for heavy endpoints like department summaries and move analytics queries to read replicas so they don't block normal punch-in writes.