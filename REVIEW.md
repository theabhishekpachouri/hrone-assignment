# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|

| 1 | Global setup (`MongoClient`) | Using sync PyMongo inside FastAPI | Server will block or lag under heavy multi-user requests | Switched to Motor (`AsyncIOMotorClient`) for async support |
| 2 | `list_employees` | Wrong skip formula (`page * page_size`) | Page 1 skips the first 20 records (`skip=20` instead of `0`) | Fixed formula to `(page - 1) * page_size` |
| 3 | `list_employees` | Total count ignores department filter | Total count shows all employees even when I filter by department | Passed query filter into count: `db.employees.count_documents(q)` |
| 4 | `punch_in` | Missing check for employee existence | Passing a fake `emp_code` throws a 500 KeyError instead of a proper 404 | Added check to raise 404 if employee is not found |
| 5 | `punch_in` | Race condition in duplicate check | Two fast parallel requests can bypass check and create duplicate attendance for the same day | Added unique compound index on `(emp_code, date)` and handled duplicate key exception |
| 6 | `list_attendance` | Sorting and pagination done in Python memory | Memory spikes and app slows down on large datasets (100k+ records) | Moved sort, skip, and limit directly to MongoDB query level |
| 7 | App startup | No indexes created on startup | Queries run slow because of full collection scans | Added index creation code when the app starts |

Also note anything you looked at and decided was **not** a defect, and why:
* **`/health` endpoint:** I checked this and it looks fine—just returns `{"status": "ok"}` which works as a basic liveness check.
* **`create_employee` endpoint:** I reviewed the logic; it already handles duplicate employee codes properly and validates fields using Pydantic models, so no changes needed.