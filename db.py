"""SQLite init and helpers."""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "finance.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "models.sql")

BASE_CATEGORIES = [
    # spending categories (count toward spending totals)
    "Rent", "Utility", "Shopping", "Dining",
    "Grocery", "Subscription", "Medical", "Transport",
    "Travel", "Pets", "Tax", "Reimbursement", "Other",
    # money-movement labels (NOT spending) -- Claude picks these too now.
    # Income is split into Salary (employment) and Other Income (everything else).
    "Salary", "Other Income", "Transfer", "Investment", "CC Payment", "Cash", "Refund",
    # manual-only label: fully exclude a row from every total/chart
    "Ignore",
]
OTHER = "Other"
INCOME = "Income"  # legacy umbrella label, kept only as a de-noise alias
REFUND_LABEL = "Refund"
IGNORE = "Ignore"  # manual-only; never offered to Claude as a candidate

# Labels that are NOT spending: each maps to a de-noise exclude_reason. A row
# with one of these is excluded from spending totals (is_spending=0). "Refund"
# is handled separately (counted as a negative offset). Salary + Other Income
# both map to "income" (so the income widgets, which filter on exclude_reason=
# 'income', keep working); "Income" stays as a legacy alias for old/stray rows.
MOVEMENT_LABELS = {
    "Salary": "income",
    "Other Income": "income",
    "Income": "income",  # legacy alias -- not seeded as a category anymore
    "Transfer": "transfer",
    "Investment": "investment",
    "CC Payment": "cc_payment",
    "Cash": "cash",
    "Ignore": "ignore",
}

# System labels the de-noise / classifier logic references BY NAME -- renaming or
# deleting these would break the engine, so the UI locks them. Everything else
# (spending categories, base or custom) can be renamed/deleted freely.
PROTECTED_CATEGORIES = set(MOVEMENT_LABELS) | {OTHER, REFUND_LABEL}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    """Lightweight additive migrations for existing DBs (CREATE TABLE IF NOT
    EXISTS won't add new columns to an already-created table)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "pending" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN pending INTEGER DEFAULT 0")
    # Trusted (merchant, category) pairs: once the user approves a review row, we
    # remember it so future transactions from the same merchant with the same
    # label skip the review queue.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS review_trust ("
        "merchant TEXT NOT NULL, category TEXT NOT NULL, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (merchant, category))")
    # One-time: split the old single "Income" label into Salary / Other Income.
    # Existing income rows default to Other Income; a later Re-classify sorts out
    # which are Salary. Idempotent -- only runs while the Income category exists.
    names = {r[0] for r in conn.execute("SELECT name FROM categories")}
    if "Income" in names:
        conn.execute("INSERT OR IGNORE INTO categories (name, is_base) VALUES ('Salary', 1)")
        conn.execute("INSERT OR IGNORE INTO categories (name, is_base) VALUES ('Other Income', 1)")
        conn.execute("UPDATE transactions SET category='Other Income' WHERE category='Income'")
        conn.execute("UPDATE rules SET category='Other Income' WHERE category='Income'")
        conn.execute("DELETE FROM categories WHERE name='Income'")
    # One-time: the default "IRS" category was renamed to the more general "Tax".
    if "IRS" in names:
        conn.execute("INSERT OR IGNORE INTO categories (name, is_base) VALUES ('Tax', 1)")
        conn.execute("UPDATE transactions SET category='Tax' WHERE category='IRS'")
        conn.execute("UPDATE rules SET category='Tax' WHERE category='IRS'")
        conn.execute("DELETE FROM categories WHERE name='IRS'")


def init_db():
    """Create tables and seed categories. Protected system labels are always
    ensured to exist; the spending categories are seeded only on first run (empty
    table) so the user's later renames/deletions of them persist across restarts."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = f.read()
    conn = get_conn()
    conn.executescript(schema)
    _migrate(conn)
    first_run = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    for name in BASE_CATEGORIES:
        if first_run or name in PROTECTED_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, is_base) VALUES (?, 1)", (name,)
            )
    conn.commit()
    conn.close()


def get_categories():
    conn = get_conn()
    rows = conn.execute("SELECT name FROM categories ORDER BY is_base DESC, id").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def add_category(name):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO categories (name, is_base) VALUES (?, 0)", (name,))
    conn.commit()
    conn.close()


def get_rules():
    """Return list of (merchant_pattern, category)."""
    conn = get_conn()
    rows = conn.execute("SELECT merchant_pattern, category FROM rules").fetchall()
    conn.close()
    return [(r["merchant_pattern"], r["category"]) for r in rows]


def upsert_rule(merchant_pattern, category):
    conn = get_conn()
    conn.execute(
        "INSERT INTO rules (merchant_pattern, category) VALUES (?, ?) "
        "ON CONFLICT(merchant_pattern) DO UPDATE SET category=excluded.category",
        (merchant_pattern, category),
    )
    conn.commit()
    conn.close()


def delete_rule(merchant_pattern):
    conn = get_conn()
    conn.execute("DELETE FROM rules WHERE merchant_pattern=?", (merchant_pattern,))
    conn.commit()
    conn.close()


def add_review_trust(merchant, category):
    """Remember that (merchant, category) is user-approved -> skip future review."""
    if not merchant or not category:
        return
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO review_trust (merchant, category) VALUES (?, ?)",
        (merchant, category))
    conn.commit()
    conn.close()


def get_review_trust():
    """Return the set of trusted (merchant, category) pairs."""
    conn = get_conn()
    rows = conn.execute("SELECT merchant, category FROM review_trust").fetchall()
    conn.close()
    return {(r["merchant"], r["category"]) for r in rows}


def remove_review_trust(merchant, category):
    conn = get_conn()
    conn.execute("DELETE FROM review_trust WHERE merchant=? AND category=?",
                 (merchant, category))
    conn.commit()
    conn.close()


def reset_db():
    """Drop all data and re-seed base categories. Used for re-testing."""
    conn = get_conn()
    for tbl in ("transactions", "rules", "accounts", "items", "categories"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.commit()
    conn.close()
    init_db()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        conn = get_conn()
        try:
            n_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            n_txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            n_rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        except Exception:
            n_items = n_txns = n_rules = 0
        conn.close()
        if n_items or n_txns or n_rules:
            print(f"WARNING: this wipes EVERYTHING, including {n_items} bank "
                  f"connection(s) (you would have to re-link them in Plaid), "
                  f"{n_txns} transactions and {n_rules} learned rules.")
            answer = input("Type 'yes' to continue: ").strip().lower()
            if answer != "yes":
                print("Aborted, nothing changed.")
                sys.exit(0)
        reset_db()
        print("DB reset (all data cleared) at", DB_PATH)
    else:
        init_db()
        print("DB initialized at", DB_PATH)
    print("Categories:", get_categories())
