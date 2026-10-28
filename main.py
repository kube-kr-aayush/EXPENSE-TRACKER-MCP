from fastmcp import FastMCP
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.server_api import ServerApi
from datetime import date as date_type
import os
import json
import re

CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is not set. Configure it in the MCP server environment."
    )

client = MongoClient(
    MONGODB_URI,
    server_api=ServerApi("1"),
    serverSelectionTimeoutMS=10000,
)
client.admin.command("ping")
db = client["expense_tracker"]
expenses = db["expenses"]
budgets = db["budgets"]
counters = db["counters"]

# Create indexes without changing existing documents.
expenses.create_index([("id", ASCENDING)], unique=True)
expenses.create_index([("date", DESCENDING), ("id", DESCENDING)])
budgets.create_index(
    [("category", ASCENDING), ("month", ASCENDING)], unique=True
)

mcp = FastMCP("ExpenseTracker")


def next_id(counter_name, collection):
    """Atomically allocate an integer ID, seeded from existing migrated records."""
    highest = collection.find_one({}, sort=[("id", DESCENDING)], projection={"id": 1})
    seed = highest["id"] if highest and isinstance(highest.get("id"), int) else 0
    counters.update_one(
        {"_id": counter_name}, {"$max": {"seq": seed}}, upsert=True
    )
    result = counters.find_one_and_update(
        {"_id": counter_name},
        {"$inc": {"seq": 1}},
        return_document=ReturnDocument.AFTER,
    )
    return result["seq"]


def public_expense(doc):
    if not doc:
        return None
    return {
        "id": doc.get("id"),
        "date": doc.get("date"),
        "amount": doc.get("amount"),
        "category": doc.get("category"),
        "subcategory": doc.get("subcategory", ""),
        "note": doc.get("note", ""),
    }


@mcp.tool()
def add_expense(date, amount, category, subcategory="", note=""):
    """Add a new expense entry to MongoDB."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Amount must be a number."}
    if amount < 0:
        return {"status": "error", "message": "Amount cannot be negative."}
    expense_id = next_id("expense_id", expenses)
    doc = {
        "id": expense_id,
        "date": str(date),
        "amount": amount,
        "category": str(category),
        "subcategory": str(subcategory or ""),
        "note": str(note or ""),
    }
    expenses.insert_one(doc)
    return {"status": "ok", "id": expense_id}


@mcp.tool()
def delete_expense(expense_id: int):
    """Delete one expense using its unique database ID."""
    doc = expenses.find_one_and_delete({"id": int(expense_id)})
    if doc is None:
        return {"status": "not_found", "message": f"No expense found with ID {expense_id}."}
    return {"status": "deleted", "expense": public_expense(doc)}


@mcp.tool()
def get_expense(expense_id: int):
    """Fetch a single expense by its unique ID."""
    doc = expenses.find_one({"id": int(expense_id)}, {"_id": 0})
    if doc is None:
        return {"status": "not_found", "message": f"No expense found with ID {expense_id}."}
    return {"status": "ok", "expense": public_expense(doc)}


@mcp.tool()
def update_expense(
    expense_id: int,
    date: str = None,
    amount: float = None,
    category: str = None,
    subcategory: str = None,
    note: str = None,
):
    """Update the provided fields of an existing expense by ID."""
    updates = {
        key: value
        for key, value in {
            "date": date,
            "amount": amount,
            "category": category,
            "subcategory": subcategory,
            "note": note,
        }.items()
        if value is not None
    }
    if not updates:
        return {"status": "no_changes", "message": "Provide at least one field to update."}
    if "amount" in updates:
        try:
            updates["amount"] = float(updates["amount"])
        except (TypeError, ValueError):
            return {"status": "error", "message": "Amount must be a number."}
        if updates["amount"] < 0:
            return {"status": "error", "message": "Amount cannot be negative."}
    result = expenses.update_one({"id": int(expense_id)}, {"$set": updates})
    if result.matched_count == 0:
        return {"status": "not_found", "message": f"No expense found with ID {expense_id}."}
    return {"status": "updated", "expense_id": int(expense_id), "updated_fields": updates}


@mcp.tool()
def list_categories():
    """Return the available expense categories and subcategories."""
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "error", "message": f"Could not read categories.json: {e}"}


@mcp.tool()
def list_expenses(start_date, end_date):
    """List expense entries within an inclusive date range."""
    docs = expenses.find(
        {"date": {"$gte": str(start_date), "$lte": str(end_date)}},
        {"_id": 0},
    ).sort([("id", ASCENDING)])
    return [public_expense(doc) for doc in docs]


@mcp.tool()
def search_expenses(
    category: str = None,
    min_amount: float = None,
    max_amount: float = None,
    keyword: str = None,
    start_date: str = None,
    end_date: str = None,
):
    """Search expenses by category, amount range, note keyword, or date range."""
    query = {}
    if category:
        query["category"] = category
    if min_amount is not None or max_amount is not None:
        query["amount"] = {}
        if min_amount is not None:
            query["amount"]["$gte"] = float(min_amount)
        if max_amount is not None:
            query["amount"]["$lte"] = float(max_amount)
    if start_date or end_date:
        query["date"] = {}
        if start_date:
            query["date"]["$gte"] = str(start_date)
        if end_date:
            query["date"]["$lte"] = str(end_date)
    if keyword:
        query["$or"] = [
            {"note": {"$regex": re.escape(keyword), "$options": "i"}},
            {"subcategory": {"$regex": re.escape(keyword), "$options": "i"}},
        ]
    docs = expenses.find(query, {"_id": 0}).sort([("date", DESCENDING), ("id", DESCENDING)])
    return [public_expense(doc) for doc in docs]


@mcp.tool()
def summarize_by_period(start_date: str, end_date: str, period: str = "month"):
    """Summarize expenses by ISO week or month within a date range."""
    if period not in ("week", "month"):
        return {"status": "error", "message": "period must be 'week' or 'month'."}
    docs = expenses.find(
        {"date": {"$gte": str(start_date), "$lte": str(end_date)}},
        {"_id": 0, "date": 1, "amount": 1},
    )
    groups = {}
    for doc in docs:
        try:
            dt = date_type.fromisoformat(doc["date"])
        except (ValueError, TypeError, KeyError):
            continue
        key = dt.strftime("%Y-%m") if period == "month" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        if key not in groups:
            groups[key] = {"period": key, "total_amount": 0.0, "expense_count": 0}
        groups[key]["total_amount"] += float(doc.get("amount", 0))
        groups[key]["expense_count"] += 1
    return [
        {**groups[key], "total_amount": round(groups[key]["total_amount"], 2)}
        for key in sorted(groups)
    ]


@mcp.tool()
def set_budget(category: str, month: str, amount: float):
    """Set or update a category budget for a month. Month format: YYYY-MM."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {"status": "error", "message": "Month must be in YYYY-MM format, e.g. 2026-10."}
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Budget amount must be a number."}
    if amount < 0:
        return {"status": "error", "message": "Budget amount cannot be negative."}
    category = category.strip()
    if not category:
        return {"status": "error", "message": "Category cannot be empty."}

    existing = budgets.find_one({"category": category, "month": month})
    if existing:
        budgets.update_one({"_id": existing["_id"]}, {"$set": {"amount": amount}})
    else:
        budget_id = next_id("budget_id", budgets)
        budgets.insert_one({"id": budget_id, "category": category, "month": month, "amount": amount})
    return {"status": "ok", "category": category, "month": month, "budget": amount}


@mcp.tool()
def get_budget_status(month: str, category: str = None):
    """Show monthly budgets, spending, remaining amount and over-budget status."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {"status": "error", "message": "Month must be in YYYY-MM format, e.g. 2026-10."}
    budget_query = {"month": month}
    if category:
        budget_query["category"] = category.strip()
    results = []
    for budget_doc in budgets.find(budget_query, {"_id": 0}).sort("category", ASCENDING):
        spent_pipeline = [
            {"$match": {"category": budget_doc["category"], "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
            {"$group": {"_id": None, "spent": {"$sum": "$amount"}}},
        ]
        agg = list(expenses.aggregate(spent_pipeline))
        spent = round(float(agg[0]["spent"]), 2) if agg else 0.0
        budget_amount = float(budget_doc["amount"])
        results.append({
            "category": budget_doc["category"],
            "month": month,
            "budget": budget_amount,
            "spent": spent,
            "remaining": round(budget_amount - spent, 2),
            "over_budget": spent > budget_amount,
        })
    return results


@mcp.tool()
def summarize(start_date, end_date, category=None):
    """Summarize expenses by category within an inclusive date range."""
    match = {"date": {"$gte": str(start_date), "$lte": str(end_date)}}
    if category:
        match["category"] = category
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$category", "total_amount": {"$sum": "$amount"}}},
        {"$sort": {"_id": 1}},
    ]
    return [
        {"category": row["_id"], "total_amount": round(float(row["total_amount"]), 2)}
        for row in expenses.aggregate(pipeline)
    ]


@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    """Return the category JSON resource, reading fresh on each request."""
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    mcp.run()
