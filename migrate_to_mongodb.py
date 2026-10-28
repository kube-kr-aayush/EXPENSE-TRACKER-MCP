
import os
import sqlite3
from pymongo import MongoClient
from pymongo.server_api import ServerApi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "expenses.db")

uri = os.getenv("MONGODB_URI")
if not uri:
    raise RuntimeError(
        "MONGODB_URI missing. Set it in this PowerShell terminal first."
    )

client = MongoClient(
    uri,
    server_api=ServerApi("1"),
    serverSelectionTimeoutMS=10000
)

try:
    client.admin.command("ping")
    db = client["expense_tracker"]

    # Read old SQLite data
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    expense_rows = [
        dict(row) for row in cursor.execute(
            "SELECT * FROM expenses"
        ).fetchall()
    ]

    table_names = {
        row["name"] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    budget_rows = []
    if "budgets" in table_names:
        budget_rows = [
            dict(row) for row in cursor.execute(
                "SELECT * FROM budgets"
            ).fetchall()
        ]

    conn.close()

    # Ensure IDs are unique in MongoDB
    expenses = db["expenses"]
    budgets = db["budgets"]
    counters = db["counters"]

    expenses.create_index("id", unique=True)
    budgets.create_index(
        [("category", 1), ("month", 1)],
        unique=True
    )

    # Copy only missing expense IDs; never overwrite existing docs
    inserted_expenses = 0
    for expense in expense_rows:
        existing = expenses.find_one({"id": expense["id"]})
        if existing is None:
            expenses.insert_one(expense)
            inserted_expenses += 1
        elif any(
            existing.get(key) != value
            for key, value in expense.items()
        ):
            raise RuntimeError(
                f"Expense ID {expense['id']} already exists "
                "with different data. No overwrite performed."
            )

    # Copy budgets by their original IDs; skip exact matches
    inserted_budgets = 0
    for budget in budget_rows:
        existing = budgets.find_one({"id": budget["id"]})
        if existing is None:
            budgets.insert_one(budget)
            inserted_budgets += 1
        elif any(
            existing.get(key) != value
            for key, value in budget.items()
        ):
            raise RuntimeError(
                f"Budget ID {budget['id']} already exists "
                "with different data. No overwrite performed."
            )

    # Seed the counter so new IDs continue after the largest existing ID
    max_id_doc = expenses.find_one(
        {}, sort=[("id", -1)], projection={"id": 1}
    )
    max_id = max_id_doc["id"] if max_id_doc else 0

    counters.update_one(
        {"_id": "expense_id"},
        {"$max": {"seq": max_id}},
        upsert=True
    )

    print("Migration completed successfully!")
    print("SQLite expenses:", len(expense_rows))
    print("Expenses newly copied:", inserted_expenses)
    print("SQLite budgets:", len(budget_rows))
    print("Budgets newly copied:", inserted_budgets)
    print("MongoDB expense count:", expenses.count_documents({}))
    print("MongoDB budget count:", budgets.count_documents({}))
    print("SQLite database was not modified.")

finally:
    client.close()
