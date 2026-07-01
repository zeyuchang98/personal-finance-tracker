"""Sync pipeline: Plaid pull -> normalize -> classify (ALL new rows) -> finalize -> store.

Every NEW transaction goes through classification (classifier.classify_batch):
Claude (guide-primed) or a saved rule assigns each row one label from the full
list -- spending categories AND money-movement labels (Income, Transfer,
Investment, CC Payment, Cash, Refund). denoise.annotate then DETERMINISTICALLY
turns the label into is_spending / signed_amount / exclude_reason / needs_review
and runs the cc-payment pairing pass.

Pending vs posted: Plaid sends a pending authorization first, then when it posts
sends the posted txn (NEW id, carrying pending_transaction_id) plus the pending
txn in `removed`. We delete both the explicitly-removed ids AND any id named by a
pending_transaction_id, so a restaurant charge doesn't show up twice (once
without the tip, once with).
"""
from classifier import normalize_merchant, classify_batch, key_of
from db import get_conn, OTHER
from denoise import annotate, finalize, reapply_denoise
from plaid_client import fetch_new_transactions


def _empty(removed=0):
    return {"fetched": 0, "inserted": 0, "by_rule": 0, "by_claude": 0,
            "excluded": 0, "removed": removed, "updated": 0}


def run_sync(reconcile=False):
    """Pull and store. Returns {fetched, inserted, by_rule, by_claude, excluded,
    removed, updated}.

    reconcile=True (full re-sync after a cursor reset): in addition to the
    normal removed/pending_transaction_id dedup, drop any local Plaid row inside
    the window Plaid just replayed that Plaid no longer reports. That cleans up
    stale pending/posted DUPLICATES even when the bank didn't supply a
    pending_transaction_id and the original `removed` event was missed. Manual
    rows and history older than Plaid's window are left alone.
    """
    upserts, removed_ids = fetch_new_transactions()

    # Supersede deletions: rows Plaid explicitly removed, plus any pending txn
    # that a posted txn in this batch replaces (via pending_transaction_id).
    supersede = set(removed_ids)
    for t in upserts:
        if t.get("pending_transaction_id"):
            supersede.add(t["pending_transaction_id"])

    conn = get_conn()
    if supersede:
        conn.executemany(
            "DELETE FROM transactions WHERE txn_id=?", [(i,) for i in supersede])
        conn.commit()

    if reconcile and upserts:
        live = {t["txn_id"] for t in upserts}
        min_date = min(t["date"] for t in upserts)
        local = conn.execute(
            "SELECT txn_id FROM transactions WHERE source='plaid' AND date >= ? "
            "AND (classified_by IS NULL OR classified_by != 'manual')",
            (min_date,)).fetchall()
        stale = [(r["txn_id"],) for r in local if r["txn_id"] not in live]
        if stale:
            conn.executemany("DELETE FROM transactions WHERE txn_id=?", stale)
            conn.commit()

    removed_n = conn.total_changes  # only deletes have run so far

    if not upserts:
        reapply_denoise(conn)  # still re-apply de-noise logic to existing rows
        conn.close()
        return _empty(removed=removed_n)

    for t in upserts:
        t["merchant_normalized"] = normalize_merchant(t["merchant_raw"])

    # Split into genuinely new vs already-stored (modified) so we only spend
    # Claude calls on new merchants, and never re-classify existing rows.
    ids = [t["txn_id"] for t in upserts]
    placeholders = ",".join("?" * len(ids))
    existing = {r["txn_id"] for r in conn.execute(
        f"SELECT txn_id FROM transactions WHERE txn_id IN ({placeholders})", ids)}
    new = [t for t in upserts if t["txn_id"] not in existing]
    modified = [t for t in upserts if t["txn_id"] in existing]

    inserted = by_rule = by_claude = excluded = 0
    if new:
        labels = classify_batch(new)
        for t in new:
            t["label"], t["classified_by"] = labels[key_of(t)]
        annotate(new)  # finalize fields + cc-payment pairing (may override a leg)
        for t in new:
            category, classified_by = t["label"], t["classified_by"]
            if classified_by == "claude":
                claude_cat = category
            else:
                sib = conn.execute(
                    "SELECT claude_category FROM transactions "
                    "WHERE merchant_normalized=? AND claude_category IS NOT NULL LIMIT 1",
                    (t["merchant_normalized"],),
                ).fetchone()
                claude_cat = sib["claude_category"] if sib else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(txn_id, date, merchant_raw, merchant_normalized, amount, account, "
                " account_type, category, claude_category, source, classified_by, "
                " is_spending, signed_amount, exclude_reason, needs_review, pending, pfc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'plaid', ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["txn_id"], t["date"], t["merchant_raw"], t["merchant_normalized"],
                    t["amount"], t["account"], t["account_type"], category,
                    claude_cat, classified_by, t["is_spending"], t["signed_amount"],
                    t["exclude_reason"], t["needs_review"], t.get("pending", 0), t["pfc"],
                ),
            )
            if cur.rowcount:
                inserted += 1
                if classified_by == "rule":
                    by_rule += 1
                elif classified_by == "claude":
                    by_claude += 1
                if t["is_spending"] == 0:
                    excluded += 1

    # Modified rows: same id, refreshed facts (e.g. a pending amount finalized to
    # include the tip). Update the volatile fields and re-finalize is_spending /
    # signed_amount from the row's EXISTING label; keep its category/classified_by.
    updated = 0
    for t in modified:
        row = conn.execute(
            "SELECT category FROM transactions WHERE txn_id=?", (t["txn_id"],)).fetchone()
        label = (row["category"] if row else None) or OTHER
        f = finalize(label, t["amount"], t["account_type"])
        conn.execute(
            "UPDATE transactions SET date=?, merchant_raw=?, merchant_normalized=?, "
            "amount=?, pending=?, pfc=?, is_spending=?, signed_amount=?, exclude_reason=? "
            "WHERE txn_id=?",
            (t["date"], t["merchant_raw"], t["merchant_normalized"], t["amount"],
             t.get("pending", 0), t["pfc"], f["is_spending"], f["signed_amount"],
             f["exclude_reason"], t["txn_id"]),
        )
        updated += 1

    conn.commit()
    reapply_denoise(conn)
    conn.close()
    return {
        "fetched": len(upserts), "inserted": inserted, "by_rule": by_rule,
        "by_claude": by_claude, "excluded": excluded, "removed": removed_n,
        "updated": updated,
    }
