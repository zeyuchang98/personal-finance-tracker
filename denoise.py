"""De-noise layer: turn a Claude/rule LABEL into spending bookkeeping.

Classification now runs on EVERY transaction (see classifier.py): Claude (primed
by categorization_guide.md) or a saved rule assigns each row one label from the
full list -- spending categories AND money-movement labels (Income, Transfer,
Investment, CC Payment, Cash, Refund). This module is the thin DETERMINISTIC
layer that translates that label into:

  is_spending     1 = counted in spending reports, 0 = excluded
  signed_amount   + = spend, - = offset (refund / reimbursement), 0 if excluded
  exclude_reason  movement label: cc_payment / transfer / investment / income /
                  cash / refund / p2p / NULL (plain spend)
  needs_review    1 = surface to the user to confirm

Two things stay deterministic on purpose (a per-transaction LLM can't do them
well): the sign of signed_amount (from the amount's direction) and the
cc-payment PAIRING pass, which is relational -- it matches a depository outflow
against an equal credit-account inflow within a few days and overrides BOTH legs
to CC Payment, catching pairs Claude labelled as something else.
"""
from datetime import date

from db import MOVEMENT_LABELS, REFUND_LABEL, OTHER, get_review_trust

PAIR_WINDOW_DAYS = 5  # cc-payment pairing: legs must post within this window

# Movement labels confident enough to NOT flag for review.
_NO_REVIEW = {"cc_payment", "cash", "ignore"}


def _res(is_spending, signed_amount, exclude_reason, needs_review=0):
    return {
        "is_spending": is_spending,
        "signed_amount": round(signed_amount, 2),
        "exclude_reason": exclude_reason,
        "needs_review": needs_review,
    }


def finalize(label: str, amount, account_type=None) -> dict:
    """Map a classification label + the raw amount to spending bookkeeping.
    Plaid convention: positive amount = money out, negative = money in."""
    amt = float(amount)
    inflow = amt < 0

    # Credit-card payment: only "free" (excluded) when the card it pays is also
    # connected -- then the card's own purchases are the real spending. We can
    # only trust that when it's TWO-WAY VERIFIED:
    #   - the inflow leg (a payment landing ON a connected credit card) is
    #     excluded here, and
    #   - the depository OUTFLOW is left as spending; the pairing pass excludes
    #     it only if it matches that connected card's inflow.
    # A payment to a card that ISN'T connected never pairs, so it stays real
    # spending (otherwise that spending would vanish entirely).
    if label == "CC Payment":
        if inflow:
            return _res(0, 0.0, "cc_payment", 0)
        return _res(1, abs(amt), "cc_payment", 1)

    # Other money-movement labels: excluded from spending totals.
    if label in MOVEMENT_LABELS:
        reason = MOVEMENT_LABELS[label]
        review = 0 if reason in _NO_REVIEW else 1
        return _res(0, 0.0, reason, review)

    # Merchant refund: money back, counted as a negative offset.
    if label == REFUND_LABEL:
        return _res(1, -abs(amt), "refund", 1)

    # Plain spending category on an INFLOW = a reimbursement/payback (e.g. a
    # friend Venmo-ing you their share): counts as a negative offset, flagged.
    if inflow:
        return _res(1, -abs(amt), "p2p", 1)

    # Normal case: an outflow in a spending category.
    return _res(1, abs(amt), None, 0)


def _pair_cc_payments(txns: list[dict]) -> None:
    """Safety net: catch cc-payment pairs by matching a still-counted depository
    outflow against an equal-amount credit-account inflow posted within
    PAIR_WINDOW_DAYS. Overrides BOTH legs to CC Payment (label + fields)."""
    def _date(t):
        return date.fromisoformat(t["date"])

    # Candidates: depository outflows still counted as spending -- both plain
    # spends and tentatively-counted cc-payment outflows (which become "free"
    # only once verified by a matching credit-account inflow below).
    outs = [t for t in txns
            if t["amount"] > 0 and t["account_type"] in ("checking", "savings")
            and t["is_spending"] == 1 and t["exclude_reason"] in (None, "cc_payment")]
    ins = [t for t in txns
           if t["amount"] < 0 and t["account_type"] == "credit"]
    used = set()
    for o in outs:
        for i in ins:
            if id(i) in used:
                continue
            if (abs(o["amount"] + i["amount"]) < 0.01
                    and abs((_date(o) - _date(i)).days) <= PAIR_WINDOW_DAYS):
                for leg in (o, i):
                    leg.update(_res(0, 0.0, "cc_payment"))
                    leg["label"] = "CC Payment"        # reflect in stored category
                    leg["classified_by"] = "rule"      # deterministic, not Claude
                used.add(id(i))
                break


def annotate(txns: list[dict]) -> list[dict]:
    """Finalize a batch in place: each txn must already carry t['label'] (from
    classify). Sets is_spending / signed_amount / exclude_reason / needs_review
    from the label, then runs the cc-payment pairing override."""
    for t in txns:
        t.update(finalize(t["label"], t["amount"], t.get("account_type")))
    _pair_cc_payments(txns)
    return txns


def reapply_denoise(conn) -> None:
    """Re-derive de-noise bookkeeping (is_spending / signed_amount /
    exclude_reason / category) for every non-manual row from its stored label,
    and re-run the relational cc-payment pairing across the WHOLE set.

    This MUST be the final step after anything that re-labels rows. Per-row
    writes (which re-run finalize() in isolation) can't know about pairing, so a
    paired cc-payment would otherwise flip back to spending. Running this last
    makes pairing authoritative. Deterministic, Claude-free.

    needs_review is only ever LOWERED here, never raised: new = min(old, computed).
    That clears the flag when the row is now resolved (e.g. a cc-payment that got
    paired -> excluded, computed review 0) while keeping any flag the user already
    dismissed dismissed, and never re-raising a flag the user cleared."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date, merchant_normalized, amount, account, account_type, "
        "category, needs_review FROM transactions WHERE source='plaid' "
        "AND (classified_by IS NULL OR classified_by != 'manual')").fetchall()]
    old_review = {r["id"]: r["needs_review"] for r in rows}
    trusted = get_review_trust()  # (merchant, category) pairs the user approved
    for r in rows:
        r["label"] = r["category"] or OTHER
    annotate(rows)  # finalize + cc-payment pairing over the full set (sets needs_review)
    for r in rows:
        review = min(old_review[r["id"]] or 0, r["needs_review"] or 0)
        if (r["merchant_normalized"], r["label"]) in trusted:
            review = 0  # user has approved this merchant+label before
        conn.execute(
            "UPDATE transactions SET category=?, is_spending=?, signed_amount=?, "
            "exclude_reason=?, needs_review=? WHERE id=?",
            (r["label"], r["is_spending"], r["signed_amount"], r["exclude_reason"],
             review, r["id"]))
    conn.commit()
