"""Flask entry point + routes."""
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

from db import (  # noqa: E402
    init_db, get_conn, get_categories, add_category, upsert_rule, delete_rule,
    get_rules, OTHER, PROTECTED_CATEGORIES, add_review_trust, get_review_trust,
    remove_review_trust,
)
from classifier import (  # noqa: E402
    match_rule, classify_batch, key_of, draft_category_meaning, ensure_guide,
    _GUIDE_PATH,
)
from denoise import finalize, reapply_denoise  # noqa: E402

PERSONAL_SECTION = "## Personal rules"


def _write_label(conn, row, label, classified_by, manual=False):
    """Apply a classification label to a stored row: set the category plus the
    deterministic de-noise fields (is_spending / signed_amount / exclude_reason /
    needs_review) that the label implies. `row` needs id, amount, account_type.
    Manual corrections clear needs_review and are pinned so re-classify skips
    them. claude_category is only (re)written on the Claude path, preserving the
    original pick used for revert detection."""
    f = finalize(label, row["amount"], row["account_type"])
    sets = ("category=?, classified_by=?, is_spending=?, signed_amount=?, "
            "exclude_reason=?, needs_review=?")
    params = [label, classified_by, f["is_spending"], f["signed_amount"],
              f["exclude_reason"], 0 if manual else f["needs_review"]]
    if classified_by == "claude":
        sets += ", claude_category=?"
        params.append(label)
    params.append(row["id"])
    conn.execute(f"UPDATE transactions SET {sets} WHERE id=?", params)


def _guide_upsert(section_header: str, line: str, key_prefix: str | None = None) -> bool:
    """Insert `line` at the end of `section_header` in categorization_guide.md
    (section created if missing). If `key_prefix` is given, an existing line in
    that section starting with it is REPLACED instead of duplicated (used to
    update a category's meaning). Idempotent on an exact line. Returns True if
    the file changed."""
    try:
        with open(_GUIDE_PATH, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    if section_header in content:
        start = content.index(section_header) + len(section_header)
        nxt = content.find("\n## ", start)
        end = nxt if nxt != -1 else len(content)
        body = content[start:end]
        if line in body:
            return False
        rows = body.rstrip("\n").split("\n")
        replaced = False
        if key_prefix:
            for i, ln in enumerate(rows):
                if ln.strip().startswith(key_prefix):
                    rows[i] = line
                    replaced = True
                    break
        if not replaced:
            rows.append(line)
        content = content[:start] + "\n".join(rows) + "\n" + content[end:]
    else:
        content = content.rstrip("\n")
        content += ("\n\n" if content else "") + section_header + "\n\n" + line + "\n"
    with open(_GUIDE_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def _append_guide_line(note: str) -> bool:
    """Append a user-authored generalization under '## Personal rules'."""
    return _guide_upsert(PERSONAL_SECTION, "- " + " ".join(note.split()))


def _append_category_meaning(name: str, description: str) -> bool:
    """Record a new category's meaning under '## Category meanings' so Claude
    knows when to pick it. Replaces an existing entry for the same name."""
    desc = " ".join((description or "").split())
    if not desc:
        return False
    return _guide_upsert("## Category meanings", f"- {name}: {desc}",
                         key_prefix=f"- {name}:")


def _edit_guide_meaning_lines(transform) -> None:
    """Apply `transform(line) -> line | None` to every line in the guide; a
    None return drops the line. Used to rename/remove a category's meaning."""
    try:
        with open(_GUIDE_PATH, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return
    out = []
    for ln in content.split("\n"):
        r = transform(ln)
        if r is not None:
            out.append(r)
    with open(_GUIDE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _category_meanings() -> dict:
    """Parse the guide's '## Category meanings' section into {name: meaning}."""
    try:
        with open(_GUIDE_PATH, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return {}
    sec = "## Category meanings"
    if sec not in content:
        return {}
    start = content.index(sec) + len(sec)
    nxt = content.find("\n## ", start)
    body = content[start: nxt if nxt != -1 else len(content)]
    import re
    out = {}
    for ln in body.split("\n"):
        m = re.match(r"\s*-\s+([^:]+):\s*(.*)", ln)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _rename_guide_meaning(old: str, new: str) -> None:
    _edit_guide_meaning_lines(
        lambda ln: ln.replace(f"- {old}:", f"- {new}:", 1)
        if ln.strip().startswith(f"- {old}:") else ln)


def _remove_guide_meaning(name: str) -> None:
    _edit_guide_meaning_lines(
        lambda ln: None if ln.strip().startswith(f"- {name}:") else ln)


def _reclassify_merchant(conn, merchant: str) -> int:
    """Re-run Claude (guide-primed, context-aware) for one merchant's rows that
    aren't manually pinned, and re-finalize the de-noise fields. Covers ALL of
    the merchant's rows (spending and money-movement) -- guide mode deletes the
    merchant's hard rule first, so they re-derive from Claude. Returns the number
    of rows updated."""
    if not merchant:
        return 0
    rows = [dict(r) for r in conn.execute(
        "SELECT id, merchant_normalized, amount, account_type, pfc FROM transactions "
        "WHERE merchant_normalized=? AND (classified_by IS NULL OR classified_by != 'manual')",
        (merchant,)).fetchall()]
    if not rows:
        return 0
    labels = classify_batch(rows)
    for r in rows:
        label, by = labels[key_of(r)]
        _write_label(conn, r, label, by)
    conn.commit()
    return len(rows)


def _reapply_rules():
    """Re-apply the current rule set to every stored (non-manual) transaction so
    a rule change takes effect retroactively. A row a rule matches becomes that
    label; a row no rule matches falls back to Claude's original pick. Either way
    the de-noise fields are re-finalized from the resulting label."""
    rules = get_rules()
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, merchant_normalized, amount, account_type, claude_category "
        "FROM transactions WHERE source='plaid' "
        "AND (classified_by IS NULL OR classified_by != 'manual')").fetchall()]
    for r in rows:
        cat = match_rule(r["merchant_normalized"], rules)
        if cat:
            _write_label(conn, r, cat, "rule")
        elif r["claude_category"] is not None:
            _write_label(conn, r, r["claude_category"], "claude")
        # else: no rule and no Claude baseline -> leave the row untouched.
    conn.commit()
    conn.close()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev")
# Don't cache static files (CSS/JS) — a normal browser refresh always gets the
# latest during local development.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

init_db()
ensure_guide()


@app.route("/")
def index():
    return render_template("index.html")


# ---------- First-run setup check ----------

_PLACEHOLDERS = {"your_plaid_client_id", "your_plaid_secret", "your_plaid_sandbox_secret",
                 "sk-ant-your-key", "change_me", "change-me"}


def _configured(value: str | None) -> bool:
    v = (value or "").strip()
    return bool(v) and v not in _PLACEHOLDERS and not v.startswith("your_")


@app.route("/api/setup_status")
def api_setup_status():
    """Report whether the required API keys are configured. Re-reads .env on
    every call so the user can fill it in and click Recheck without
    restarting the app."""
    load_dotenv(override=True)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return jsonify({
        "plaid": _configured(os.environ.get("PLAID_CLIENT_ID"))
                 and _configured(os.environ.get("PLAID_SECRET")),
        "anthropic": _configured(anthropic_key) and anthropic_key.startswith("sk-ant-"),
        "plaid_env": os.environ.get("PLAID_ENV", "sandbox"),
    })


# ---------- Plaid Link (initial auth, run on computer) ----------

@app.route("/api/create_link_token", methods=["POST"])
def api_create_link_token():
    from plaid_client import create_link_token
    return jsonify({"link_token": create_link_token()})


@app.route("/api/exchange_public_token", methods=["POST"])
def api_exchange_public_token():
    from plaid_client import exchange_public_token
    data = request.get_json(force=True)
    exchange_public_token(data["public_token"], data.get("institution"))
    return jsonify({"ok": True})


# ---------- Sandbox quick connect (no Link UI) ----------

@app.route("/api/sandbox_connect", methods=["POST"])
def api_sandbox_connect():
    if os.environ.get("PLAID_ENV", "sandbox") != "sandbox":
        return jsonify({"ok": False, "error": "only available in sandbox"}), 400
    from plaid_client import sandbox_create_item
    data = request.get_json(silent=True) or {}
    inst_id = data.get("institution_id", "ins_109508")
    inst_name = data.get("institution_name", "First Platypus Bank")
    sandbox_create_item(inst_id, inst_name)
    return jsonify({"ok": True})


@app.route("/api/sandbox_seed", methods=["POST"])
def api_sandbox_seed():
    """Sandbox-only: connect a custom user seeded with all de-noise test cases."""
    if os.environ.get("PLAID_ENV", "sandbox") != "sandbox":
        return jsonify({"ok": False, "error": "only available in sandbox"}), 400
    from plaid_client import sandbox_create_custom_item
    sandbox_create_custom_item()
    return jsonify({"ok": True})


# ---------- Sync ----------

@app.route("/api/sync", methods=["POST"])
def api_sync():
    from sync import run_sync
    return jsonify(run_sync())


@app.route("/api/resync_full", methods=["POST"])
def api_resync_full():
    """Reset every item's Plaid cursor and re-sync from scratch. Plaid replays
    all current transactions; posted rows carry pending_transaction_id, so this
    cleans up old pending/posted DUPLICATES (e.g. a restaurant charge stored
    once without tip and once with). Existing rows keep their category
    (INSERT OR IGNORE); only genuinely new rows hit Claude."""
    from sync import run_sync
    conn = get_conn()
    conn.execute("UPDATE items SET cursor=NULL")
    conn.commit()
    conn.close()
    return jsonify(run_sync(reconcile=True))


@app.route("/api/reannotate", methods=["POST"])
def api_reannotate():
    """Re-finalize the de-noise fields (is_spending / signed_amount /
    exclude_reason / needs_review) from each row's CURRENT label, and re-run the
    cc-payment pairing pass. Use after changing finalize()/pairing in denoise.py.
    Does NOT re-call Claude -- use Re-classify for that. Manual rows untouched.
    NOTE: this recomputes needs_review, so dismissed flags may come back."""
    from denoise import annotate

    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date, merchant_normalized, amount, account, account_type, "
        "category, classified_by, is_spending, signed_amount, exclude_reason, "
        "needs_review FROM transactions WHERE source='plaid' "
        "AND (classified_by IS NULL OR classified_by != 'manual')").fetchall()]
    for r in rows:
        r["label"] = r["category"] or OTHER
    before = {r["id"]: (r["is_spending"], r["signed_amount"], r["exclude_reason"],
                        r["needs_review"], r["category"]) for r in rows}
    annotate(rows)  # finalize from label + cc-payment pairing (may set CC Payment)

    changed = 0
    for r in rows:
        after = (r["is_spending"], r["signed_amount"], r["exclude_reason"],
                 r["needs_review"], r["label"])
        if before[r["id"]] != after:
            changed += 1
        conn.execute(
            "UPDATE transactions SET category=?, classified_by=?, is_spending=?, "
            "signed_amount=?, exclude_reason=?, needs_review=? WHERE id=?",
            (r["label"], r["classified_by"], r["is_spending"], r["signed_amount"],
             r["exclude_reason"], r["needs_review"], r["id"]),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "rows": len(rows), "changed": changed})


@app.route("/api/reclassify", methods=["POST"])
def api_reclassify():
    """Redo classification for non-manual rows (rules first, then Claude), then
    re-finalize + re-pair via reapply_denoise. Manual rows untouched. Use after
    editing categorization_guide.md or the classifier.

    Optional JSON body {"months": N} limits the (slow) Claude pass to rows dated
    within the last N months; pairing/finalize still re-run across the full set."""
    months = (request.get_json(silent=True) or {}).get("months")
    where = ("source='plaid' AND merchant_normalized != '' "
             "AND (classified_by IS NULL OR classified_by != 'manual')")
    params = []
    if months:
        where += " AND date >= date('now', ?)"
        params.append(f"-{int(months)} months")
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date, merchant_normalized, amount, account, account_type, pfc "
        "FROM transactions WHERE " + where, params).fetchall()]
    updated = 0
    if rows:
        labels = classify_batch(rows)
        for r in rows:
            label, by = labels[key_of(r)]
            conn.execute(
                "UPDATE transactions SET category=?, classified_by=?, claude_category=? WHERE id=?",
                (label, by, label if by == "claude" else None, r["id"]))
            updated += 1
        conn.commit()
        # finalize + cc-payment pairing as the authoritative final step (a per-row
        # finalize alone would flip paired cc-payments back to spending).
        reapply_denoise(conn)
    conn.close()
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/reset_learning", methods=["POST"])
def api_reset_learning():
    """Clear learned rules + trusted-merchant records so classification can be
    re-walked from the guide alone. KEEPS the guide, categories, and every
    transaction. Run Re-classify afterwards.

    JSON {"manual": true} ALSO un-pins manual corrections (classified_by=manual ->
    NULL) so Re-classify redoes them too -- EXCEPT rows manually set to Ignore,
    which stay hidden (those are deliberate junk/duplicate exclusions the guide
    can't reproduce)."""
    also_manual = bool((request.get_json(silent=True) or {}).get("manual"))
    conn = get_conn()
    n_rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    n_trust = conn.execute("SELECT COUNT(*) FROM review_trust").fetchone()[0]
    conn.execute("DELETE FROM rules")
    conn.execute("DELETE FROM review_trust")
    n_manual = 0
    if also_manual:
        cur = conn.execute(
            "UPDATE transactions SET classified_by=NULL WHERE source='plaid' "
            "AND classified_by='manual' AND (category IS NULL OR category != 'Ignore')")
        n_manual = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "rules": n_rules, "trust": n_trust, "manual": n_manual})


# ---------- Manage learned data: rules / trusted merchants / ignored rows ----------

@app.route("/api/rules")
def api_rules_list():
    """All learned merchant->category rules (deterministic name matches)."""
    return jsonify([{"pattern": p, "category": c} for p, c in get_rules()])


@app.route("/api/rules/delete", methods=["POST"])
def api_rules_delete():
    pattern = (request.get_json(silent=True) or {}).get("pattern")
    if pattern:
        delete_rule(pattern)
    return jsonify({"ok": True})


@app.route("/api/trust")
def api_trust_list():
    """All trusted (merchant, category) pairs that skip the review queue."""
    return jsonify([{"merchant": m, "category": c} for m, c in sorted(get_review_trust())])


@app.route("/api/trust/delete", methods=["POST"])
def api_trust_delete():
    d = request.get_json(silent=True) or {}
    if d.get("merchant") and d.get("category"):
        remove_review_trust(d["merchant"], d["category"])
    return jsonify({"ok": True})


@app.route("/api/ignored")
def api_ignored_list():
    """Transactions manually set to Ignore (hidden from every total/chart)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, date, merchant_raw, merchant_normalized, amount FROM transactions "
        "WHERE source='plaid' AND category='Ignore' ORDER BY date DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transactions/<int:txn_id>/reclassify_one", methods=["POST"])
@app.route("/api/transactions/<int:txn_id>/unignore", methods=["POST"])
def api_reclassify_one(txn_id):
    """Re-classify a single row (rules + Claude) from the guide, then re-derive
    de-noise + pairing. Used to un-stick a manually ignored/edited row that has
    no stored Claude pick to reset to. Clears the manual pin so it's no longer
    stuck. claude_category is preserved (COALESCE) when a rule matches."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, date, merchant_normalized, amount, account, account_type, pfc "
        "FROM transactions WHERE id=?", (txn_id,)).fetchone()
    label = None
    if row:
        r = dict(row)
        label, by = classify_batch([r])[key_of(r)]
        conn.execute(
            "UPDATE transactions SET category=?, classified_by=?, "
            "claude_category=COALESCE(?, claude_category) WHERE id=?",
            (label, by, label if by == "claude" else None, txn_id))
        conn.commit()
        reapply_denoise(conn)
    conn.close()
    return jsonify({"ok": True, "category": label})


# ---------- Data for charts / table ----------

# SQL expression that turns a row's date into a period key for a granularity.
_PERIOD_EXPR = {
    "monthly": "substr({c},1,7)",
    "quarterly": "substr({c},1,4) || '-Q' || ((CAST(substr({c},6,2) AS INTEGER)+2)/3)",
    "yearly": "substr({c},1,4)",
}


def _period_where(gran, period, col="date"):
    """Return (sql_fragment, params) restricting `col` to the given period
    (e.g. '2026-06', '2026-Q2', '2026')."""
    if not period:
        return ("1=1", [])
    if gran == "yearly":
        return (f"substr({col},1,4)=?", [period])
    if gran == "quarterly":
        year, q = period.split("-Q")
        return (f"substr({col},1,4)=? AND ((CAST(substr({col},6,2) AS INTEGER)+2)/3)=?",
                [year, int(q)])
    return (f"substr({col},1,7)=?", [period])  # monthly


def _period_list(conn, gran):
    """All available period keys for a granularity, newest first."""
    expr = _PERIOD_EXPR.get(gran, _PERIOD_EXPR["monthly"]).format(c="date")
    return [r["p"] for r in conn.execute(
        f"SELECT DISTINCT {expr} p FROM transactions ORDER BY p DESC").fetchall()]


@app.route("/api/summary")
def api_summary():
    """Spending by category + income for one period. Query params:
    gran = monthly|quarterly|yearly, period = e.g. 2026-06 / 2026-Q2 / 2026
    (default: latest period for the granularity)."""
    gran = request.args.get("gran", "monthly")
    period = request.args.get("period")
    conn = get_conn()
    periods = _period_list(conn, gran)
    if not period:
        period = periods[0] if periods else ""
    where, params = _period_where(gran, period)
    rows = conn.execute(
        f"SELECT category, ROUND(SUM(signed_amount),2) total FROM transactions "
        f"WHERE is_spending=1 AND {where} GROUP BY category ORDER BY total DESC",
        params,
    ).fetchall()
    income_rows = conn.execute(
        f"SELECT category, ROUND(SUM(-amount),2) total FROM transactions "
        f"WHERE exclude_reason='income' AND amount<0 AND {where} "
        f"GROUP BY category ORDER BY total DESC",
        params,
    ).fetchall()
    income = round(sum(r["total"] for r in income_rows), 2)
    conn.close()
    return jsonify({
        "gran": gran,
        "period": period,
        "periods": periods,
        "income": income,
        "by_category": [{"category": r["category"], "total": r["total"]} for r in rows],
        "income_by_category": [{"category": r["category"], "total": r["total"]} for r in income_rows],
    })


@app.route("/api/trend")
def api_trend():
    """Spending vs income over time at a chosen granularity.
    Query param: period = monthly | quarterly | yearly. Returns the most recent
    N periods (oldest -> newest) so the UI can draw a comparison bar chart."""
    period = request.args.get("period", "monthly")
    pkeys = {
        "monthly": "substr(date,1,7)",
        "quarterly": "substr(date,1,4) || '-Q' || ((CAST(substr(date,6,2) AS INTEGER)+2)/3)",
        "yearly": "substr(date,1,4)",
    }
    pk = pkeys.get(period, pkeys["monthly"])
    conn = get_conn()
    spend = {r["p"]: r["v"] for r in conn.execute(
        f"SELECT {pk} p, ROUND(SUM(signed_amount),2) v FROM transactions "
        f"WHERE is_spending=1 GROUP BY p").fetchall()}
    inc = {r["p"]: r["v"] for r in conn.execute(
        f"SELECT {pk} p, ROUND(SUM(-amount),2) v FROM transactions "
        f"WHERE exclude_reason='income' AND amount<0 GROUP BY p").fetchall()}
    conn.close()
    # all periods (oldest -> newest); the chart is horizontally scrollable
    keys = sorted(set(spend) | set(inc))
    points = [{"label": k, "spending": spend.get(k) or 0, "income": inc.get(k) or 0}
              for k in keys]
    return jsonify({"period": period, "points": points})


@app.route("/api/top_merchants")
def api_top_merchants():
    """Merchants ranked by how often you spent there in the period (with total).
    Query params: gran + period (same as summary)."""
    gran = request.args.get("gran", "monthly")
    period = request.args.get("period")
    where, params = _period_where(gran, period)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT MAX(merchant_raw) name, COUNT(*) cnt, ROUND(SUM(signed_amount),2) total "
        f"FROM transactions WHERE is_spending=1 AND signed_amount>0 "
        f"AND merchant_normalized != '' AND {where} "
        f"GROUP BY merchant_normalized ORDER BY cnt DESC, total DESC", params).fetchall()
    conn.close()
    return jsonify([{"merchant": r["name"], "count": r["cnt"], "total": r["total"]}
                    for r in rows])


@app.route("/api/income")
def api_income():
    """Income detail for a period (the inflows the de-noise layer marked income).
    Query params: gran + period (same as summary)."""
    gran = request.args.get("gran", "monthly")
    period = request.args.get("period")
    where, params = _period_where(gran, period, col="t.date")
    conn = get_conn()
    rows = conn.execute(
        f"SELECT t.date, t.merchant_raw, t.amount, a.name AS account_name, "
        f"a.mask AS account_mask, t.account_type FROM transactions t "
        f"LEFT JOIN accounts a ON t.account = a.account_id "
        f"WHERE t.exclude_reason='income' AND t.amount<0 AND {where} "
        f"ORDER BY t.date DESC", params).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    total = round(sum(-r["amount"] for r in items), 2)
    return jsonify({"total": total, "items": items})


@app.route("/api/transactions")
def api_transactions():
    """Detail table. Optional filters: gran+period (e.g. monthly/2026-06),
    category, account (account_id), review=1 (needs_review only)."""
    gran = request.args.get("gran", "monthly")
    period = request.args.get("period")
    category = request.args.get("category")
    account = request.args.get("account")
    review = request.args.get("review")
    q = ("SELECT t.*, a.name AS account_name, a.mask AS account_mask "
         "FROM transactions t LEFT JOIN accounts a ON t.account = a.account_id "
         "WHERE 1=1")
    params = []
    if period:
        where, p = _period_where(gran, period, col="t.date")
        q += f" AND {where}"
        params += p
    if category:
        q += " AND t.category=?"
        params.append(category)
    if account:
        q += " AND t.account=?"
        params.append(account)
    if review:
        q += " AND t.needs_review=1"
    q += " ORDER BY t.date DESC, t.id DESC"
    conn = get_conn()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/yearly")
def api_yearly():
    """Yearly comparison: monthly totals by category for one year, plus
    per-category totals vs the previous year. Query param: year=YYYY."""
    year = request.args.get("year")
    conn = get_conn()
    years = [r["y"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,4) y FROM transactions "
        "WHERE is_spending=1 ORDER BY y DESC").fetchall()]
    if not year:
        year = years[0] if years else ""
    prev_year = str(int(year) - 1) if year else ""

    def cat_totals(y):
        return {
            r["category"] or "(uncategorized)": r["t"]
            for r in conn.execute(
                "SELECT category, ROUND(SUM(signed_amount),2) t FROM transactions "
                "WHERE is_spending=1 AND substr(date,1,4)=? GROUP BY category",
                (y,)).fetchall()
        }

    by_month_cat = {}
    for r in conn.execute(
        "SELECT substr(date,1,7) m, category, ROUND(SUM(signed_amount),2) t "
        "FROM transactions WHERE is_spending=1 AND substr(date,1,4)=? "
        "GROUP BY m, category", (year,)).fetchall():
        by_month_cat.setdefault(r["m"], {})[r["category"] or "(uncategorized)"] = r["t"]
    cur_totals = cat_totals(year)
    prev_totals = cat_totals(prev_year)
    conn.close()

    months = [f"{year}-{mm:02d}" for mm in range(1, 13)] if year else []
    categories = sorted(set(cur_totals) | set(prev_totals))
    comparison = []
    for c in categories:
        cur_t, prev_t = cur_totals.get(c, 0), prev_totals.get(c, 0)
        comparison.append({
            "category": c, "total": cur_t, "prev_total": prev_t,
            "delta_pct": round((cur_t - prev_t) / prev_t * 100, 1) if prev_t else None,
        })
    comparison.sort(key=lambda x: -x["total"])
    return jsonify({
        "year": year, "prev_year": prev_year, "years": years,
        "months": months,
        "by_month": {m: by_month_cat.get(m, {}) for m in months},
        "comparison": comparison,
        "year_total": round(sum(cur_totals.values()), 2),
        "prev_year_total": round(sum(prev_totals.values()), 2),
    })


@app.route("/api/recurring")
def api_recurring():
    """Detected recurring spending (subscriptions + periodic bills)."""
    from recurring import detect_recurring
    items = detect_recurring()
    # Only active items count toward the monthly total; ended ones are listed
    # greyed out for reference.
    monthly_total = round(sum(i["monthly_cost"] for i in items if i["active"]), 2)
    return jsonify({"items": items, "monthly_total": monthly_total})


@app.route("/api/accounts")
def api_accounts():
    """All connected accounts, with institution, for the account filter."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT a.account_id, a.name, a.mask, a.account_type, i.institution "
        "FROM accounts a LEFT JOIN items i ON a.item_id = i.item_id ORDER BY a.id"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/categories")
def api_categories():
    return jsonify(get_categories())


@app.route("/api/category_draft")
def api_category_draft():
    """Claude-drafted one-line meaning for a proposed new category, used to
    prefill the add-category dialog. Best-effort; empty draft on failure."""
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"draft": ""})
    return jsonify({"draft": draft_category_meaning(name, get_categories())})


@app.route("/api/categories", methods=["POST"])
def api_add_category():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "empty"}), 400
    add_category(name)
    # Record what the category means so Claude knows when to pick it.
    _append_category_meaning(name, data.get("description"))
    return jsonify({"ok": True, "categories": get_categories()})


@app.route("/api/categories_detail")
def api_categories_detail():
    """Categories with their is_base flag and how many transactions use each,
    for the management UI. Base categories are wired into the code and locked."""
    conn = get_conn()
    cats = conn.execute("SELECT name, is_base FROM categories ORDER BY is_base DESC, id").fetchall()
    counts = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM transactions GROUP BY category").fetchall()}
    conn.close()
    meanings = _category_meanings()
    return jsonify([{"name": r["name"], "is_base": r["is_base"],
                     "locked": r["name"] in PROTECTED_CATEGORIES,
                     "count": counts.get(r["name"], 0),
                     "meaning": meanings.get(r["name"], "")} for r in cats])


@app.route("/api/categories/meaning", methods=["POST"])
def api_set_category_meaning():
    """Set (or clear) a category's plain-English guide meaning. Allowed for any
    category — editing the meaning only steers Claude, it doesn't touch the name."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    meaning = (data.get("meaning") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "empty"}), 400
    if meaning:
        _append_category_meaning(name, meaning)  # replaces the existing line
    else:
        _remove_guide_meaning(name)
    return jsonify({"ok": True})


@app.route("/api/categories/rename", methods=["POST"])
def api_rename_category():
    """Rename a custom category everywhere (categories, transactions, rules, guide)."""
    data = request.get_json(force=True)
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    if not old or not new:
        return jsonify({"ok": False, "error": "empty"}), 400
    if old in PROTECTED_CATEGORIES:
        return jsonify({"ok": False, "error": "system label is locked"}), 400
    conn = get_conn()
    row = conn.execute("SELECT is_base FROM categories WHERE name=?", (old,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "not found"}), 404
    if new != old and conn.execute("SELECT 1 FROM categories WHERE name=?", (new,)).fetchone():
        conn.close(); return jsonify({"ok": False, "error": "a category with that name already exists"}), 400
    conn.execute("UPDATE categories SET name=? WHERE name=?", (new, old))
    conn.execute("UPDATE transactions SET category=? WHERE category=?", (new, old))
    conn.execute("UPDATE transactions SET claude_category=? WHERE claude_category=?", (new, old))
    conn.execute("UPDATE rules SET category=? WHERE category=?", (new, old))
    conn.commit(); conn.close()
    _rename_guide_meaning(old, new)
    return jsonify({"ok": True, "categories": get_categories()})


@app.route("/api/categories/delete", methods=["POST"])
def api_delete_category():
    """Delete a custom category; its transactions/rules move to `reassign_to`
    (default Other), which also lets you MERGE one category into another."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    reassign = (data.get("reassign_to") or OTHER).strip()
    if name in PROTECTED_CATEGORIES:
        return jsonify({"ok": False, "error": "system label is locked"}), 400
    conn = get_conn()
    row = conn.execute("SELECT is_base FROM categories WHERE name=?", (name,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "not found"}), 404
    conn.execute("DELETE FROM categories WHERE name=?", (name,))
    conn.execute("UPDATE transactions SET category=? WHERE category=?", (reassign, name))
    conn.execute("UPDATE transactions SET claude_category=? WHERE claude_category=?", (reassign, name))
    conn.execute("UPDATE rules SET category=? WHERE category=?", (reassign, name))
    conn.commit()
    # reassigning to a money-movement label changes spending bookkeeping
    reapply_denoise(conn)
    conn.close()
    _remove_guide_meaning(name)
    return jsonify({"ok": True, "categories": get_categories()})


@app.route("/api/review_count")
def api_review_count():
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE needs_review=1").fetchone()["n"]
    conn.close()
    return jsonify({"count": n})


@app.route("/api/transactions/<int:txn_id>/review_done", methods=["POST"])
def api_review_done(txn_id):
    """Dismiss the needs_review flag after the user has checked the row.

    Also remembers (merchant, category) as trusted so future transactions from
    the same merchant with the same label skip review, and clears the flag on any
    other rows from that same merchant+category that are pending review now."""
    conn = get_conn()
    row = conn.execute(
        "SELECT merchant_normalized, category FROM transactions WHERE id=?",
        (txn_id,)).fetchone()
    trusted = 0
    if row and row["merchant_normalized"] and row["category"]:
        add_review_trust(row["merchant_normalized"], row["category"])
        # clear this row + every sibling from the same merchant+category at once
        cur = conn.execute(
            "UPDATE transactions SET needs_review=0 "
            "WHERE needs_review=1 AND merchant_normalized=? AND category=?",
            (row["merchant_normalized"], row["category"]))
        trusted = cur.rowcount
    else:
        conn.execute("UPDATE transactions SET needs_review=0 WHERE id=?", (txn_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "cleared": trusted})


# ---------- Correction + learning ----------

@app.route("/api/transactions/<int:txn_id>/category", methods=["POST"])
def api_correct_category(txn_id):
    """Manually relabel a transaction. ANY label is allowed (spending category
    OR a money-movement label like Income/Transfer); the de-noise fields are
    re-derived from the chosen label via _write_label, so e.g. picking Income
    excludes the row and picking a spending category re-includes it. Two modes:

      rule  -> save a merchant->label rule (deterministic, applied retroactively)
      guide -> append a plain-English generalization to the guide, drop any hard
               rule for this merchant, and re-run Claude for that merchant only
    """
    data = request.get_json(force=True)
    category = data["category"]
    mode = data.get("mode", "rule")
    note = (data.get("note") or "").strip()
    conn = get_conn()
    row = conn.execute(
        "SELECT id, merchant_normalized, claude_category, amount, account_type "
        "FROM transactions WHERE id=?",
        (txn_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404

    merchant = row["merchant_normalized"]

    # Guide mode: don't write a hard rule. Append a natural-language
    # generalization to the guide, drop any existing hard rule for this
    # merchant (rules short-circuit Claude), then re-run Claude for just this
    # merchant so the user sees immediately whether the new guide line worked.
    if mode == "guide":
        if not note:
            conn.close()
            return jsonify({"ok": False, "error": "note required for guide mode"}), 400
        conn.close()
        if merchant:
            delete_rule(merchant)
        _append_guide_line(note)
        conn = get_conn()
        _reclassify_merchant(conn, merchant)  # re-labels + re-finalizes this merchant
        conn.execute("UPDATE transactions SET needs_review=0 WHERE id=?", (txn_id,))
        conn.commit()
        reapply_denoise(conn)  # restore cc-payment pairing (per-row writes undo it)
        applied = conn.execute(
            "SELECT category FROM transactions WHERE id=?", (txn_id,)).fetchone()
        applied_cat = applied["category"] if applied else None
        conn.close()
        return jsonify({"ok": True, "mode": "guide", "applied": applied_cat,
                        "matched": applied_cat == category})

    # One-off: change just THIS transaction, learn nothing (no rule, no guide).
    # Still pinned 'manual' so re-classify/sync won't overwrite the fix.
    if mode == "once":
        _write_label(conn, row, category, "manual", manual=True)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "mode": "once"})

    # Rule pattern: defaults to the whole merchant, but the user may pass a
    # shorter SUBSTRING (e.g. "MT LAW") so the rule generalizes to every merchant
    # whose name contains it (match_rule does substring containment).
    pattern = (data.get("pattern") or "").strip() or merchant

    # Reverted = changed back to Claude's original static pick -> drop the rule.
    reverted = (
        row["claude_category"] is not None and category == row["claude_category"]
    )
    conn.close()

    # Update the rule library, then re-apply rules to every month.
    if pattern:
        if reverted:
            delete_rule(pattern)
            if pattern != merchant:
                delete_rule(merchant)
        else:
            upsert_rule(pattern, category)
    _reapply_rules()

    # Pin the row the user touched as 'manual' (unless reverted), re-deriving its
    # de-noise fields from the chosen label. Correcting also counts as reviewing.
    conn = get_conn()
    if not reverted:
        _write_label(conn, row, category, "manual", manual=True)
    else:
        conn.execute("UPDATE transactions SET needs_review=0 WHERE id=?", (txn_id,))
    conn.commit()
    reapply_denoise(conn)  # restore cc-payment pairing (rule re-apply undoes it)
    conn.close()
    return jsonify({"ok": True, "mode": "rule", "reverted": reverted})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
