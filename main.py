from fastmcp import FastMCP
import os
import sqlite3
import json

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("ExpenseTracker")

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                month TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                UNIQUE(category, month)
            )
        """)


init_db()

@mcp.tool()
def add_expense(date, amount, category, subcategory="", note=""):
    '''Add a new expense entry to the database.'''
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
            (date, amount, category, subcategory, note)
        )
        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
def delete_expense(expense_id: int):
    """Delete one expense using its unique database ID."""
    with sqlite3.connect(DB_PATH) as c:
        # First check whether the expense exists
        cur = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE id = ?
            """,
            (expense_id,)
        )

        row = cur.fetchone()

        if row is None:
            return {
                "status": "not_found",
                "message": f"No expense found with ID {expense_id}."
            }

        # Delete only the matching expense
        c.execute(
            "DELETE FROM expenses WHERE id = ?",
            (expense_id,)
        )

        return {
            "status": "deleted",
            "expense": {
                "id": row[0],
                "date": row[1],
                "amount": row[2],
                "category": row[3],
                "subcategory": row[4],
                "note": row[5]
            }
        }

@mcp.tool()
def get_expense(expense_id: int):
    """Fetch a single expense by its unique ID."""
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE id = ?
            """,
            (expense_id,)
        ).fetchone()

        if row is None:
            return {
                "status": "not_found",
                "message": f"No expense found with ID {expense_id}."
            }

        return {"status": "ok", "expense": dict(row)}


@mcp.tool()
def update_expense(
    expense_id: int,
    date: str = None,
    amount: float = None,
    category: str = None,
    subcategory: str = None,
    note: str = None
):
    """Update the provided fields of an existing expense by ID."""
    allowed_fields = {
        "date": date,
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "note": note
    }

    updates = {
        key: value
        for key, value in allowed_fields.items()
        if value is not None
    }

    if not updates:
        return {
            "status": "no_changes",
            "message": "Provide at least one field to update."
        }

    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "SELECT id FROM expenses WHERE id = ?",
            (expense_id,)
        )

        if cur.fetchone() is None:
            return {
                "status": "not_found",
                "message": f"No expense found with ID {expense_id}."
            }

        set_clause = ", ".join(
            f"{field} = ?" for field in updates
        )
        values = list(updates.values()) + [expense_id]

        c.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ?",
            values
        )

        return {
            "status": "updated",
            "expense_id": expense_id,
            "updated_fields": updates
        }


@mcp.tool()
def list_categories():
    """Return the available expense categories and subcategories."""
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {
            "status": "error",
            "message": f"Could not read categories.json: {e}"
        }

    
@mcp.tool()
def list_expenses(start_date, end_date):
    '''List expense entries within an inclusive date range.'''
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY id ASC
            """,
            (start_date, end_date)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

@mcp.tool()
def search_expenses(
    category: str = None,
    min_amount: float = None,
    max_amount: float = None,
    keyword: str = None,
    start_date: str = None,
    end_date: str = None
):
    """Search expenses by category, amount range, note keyword, or date range."""
    query = """
        SELECT id, date, amount, category, subcategory, note
        FROM expenses
        WHERE 1=1
    """
    params = []

    if category:
        query += " AND category = ?"
        params.append(category)

    if min_amount is not None:
        query += " AND amount >= ?"
        params.append(min_amount)

    if max_amount is not None:
        query += " AND amount <= ?"
        params.append(max_amount)

    if keyword:
        query += " AND (note LIKE ? OR subcategory LIKE ?)"
        pattern = f"%{keyword}%"
        params.extend([pattern, pattern])

    if start_date:
        query += " AND date >= ?"
        params.append(start_date)

    if end_date:
        query += " AND date <= ?"
        params.append(end_date)

    query += " ORDER BY date DESC, id DESC"

    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(query, params).fetchall()
        return [dict(row) for row in rows]

@mcp.tool()
def summarize_by_period(
    start_date: str,
    end_date: str,
    period: str = "month"
):
    """Summarize expenses by week or month within a date range."""
    if period not in ("week", "month"):
        return {
            "status": "error",
            "message": "period must be 'week' or 'month'."
        }

    if period == "month":
        period_expr = "strftime('%Y-%m', date)"
    else:
        # ISO week: Monday-based week, grouped by ISO year and week.
        period_expr = (
            "strftime('%Y', date, '-3 days', 'weekday 4') "
            "|| '-W' || "
            "strftime('%W', date, '-3 days', 'weekday 4')"
        )

    query = f"""
        SELECT {period_expr} AS period,
               SUM(amount) AS total_amount,
               COUNT(*) AS expense_count
        FROM expenses
        WHERE date BETWEEN ? AND ?
        GROUP BY period
        ORDER BY period
    """

    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(query, (start_date, end_date)).fetchall()
        return [dict(row) for row in rows]


@mcp.tool()
def set_budget(category: str, month: str, amount: float):
    """Set or update a category budget for a month. Month format: YYYY-MM."""
    import re

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {
            "status": "error",
            "message": "Month must be in YYYY-MM format, e.g. 2026-10."
        }

    if amount < 0:
        return {
            "status": "error",
            "message": "Budget amount cannot be negative."
        }

    category = category.strip()
    if not category:
        return {
            "status": "error",
            "message": "Category cannot be empty."
        }

    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """
            INSERT INTO budgets (category, month, amount)
            VALUES (?, ?, ?)
            ON CONFLICT(category, month)
            DO UPDATE SET amount = excluded.amount
            """,
            (category, month, amount)
        )

    return {
        "status": "ok",
        "category": category,
        "month": month,
        "budget": amount
    }


@mcp.tool()
def get_budget_status(month: str, category: str = None):
    """Show monthly budgets, spending, remaining amount and over-budget status."""
    import re

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {
            "status": "error",
            "message": "Month must be in YYYY-MM format, e.g. 2026-10."
        }

    query = """
        SELECT
            b.category,
            b.amount AS budget,
            COALESCE(SUM(e.amount), 0) AS spent
        FROM budgets b
        LEFT JOIN expenses e
          ON e.category = b.category
         AND substr(e.date, 1, 7) = b.month
        WHERE b.month = ?
    """
    params = [month]

    if category:
        query += " AND b.category = ?"
        params.append(category.strip())

    query += " GROUP BY b.id, b.category, b.amount ORDER BY b.category"

    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(query, params).fetchall()

    results = []
    for row in rows:
        budget = row["budget"]
        spent = row["spent"]
        results.append({
            "category": row["category"],
            "month": month,
            "budget": budget,
            "spent": spent,
            "remaining": round(budget - spent, 2),
            "over_budget": spent > budget
        })

    return results


@mcp.tool()
def summarize(start_date, end_date, category=None):
    '''Summarize expenses by category within an inclusive date range.'''
    with sqlite3.connect(DB_PATH) as c:
        query = (
            """
            SELECT category, SUM(amount) AS total_amount
            FROM expenses
            WHERE date BETWEEN ? AND ?
            """
        )
        params = [start_date, end_date]

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY category ASC"

        cur = c.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    # Read fresh each time so you can edit the file without restarting
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    mcp.run()
