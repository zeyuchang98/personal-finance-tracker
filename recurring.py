"""Recurring spending detection: subscriptions and periodic bills.

Detection is computed on demand from stored transactions (no schema change).
A merchant is recurring when it has >= MIN_OCCURRENCES spending rows whose
gaps consistently match a known cadence:

  weekly   ~7d    monthly  ~30d    quarterly ~91d    yearly  ~365d

Amount behavior splits the result into two kinds:
  subscription  amounts are (near-)identical  (Netflix, Spotify, gym)
  bill          amounts vary but timing is periodic (rent w/ changes, utility)

Each detected item reports its typical amount (median), cadence, last charge,
next expected charge, and an estimated monthly cost so the UI can show a
"total subscriptions per month" figure.
"""
from datetime import date, timedelta
from statistics import median

from db import get_conn

MIN_OCCURRENCES = 3

# cadence name -> (expected gap days, tolerance days)
CADENCES = {
    "weekly": (7, 2),
    "monthly": (30, 7),
    "quarterly": (91, 14),
    "yearly": (365, 30),
}

# amounts within this relative spread of the median = fixed (subscription)
SUBSCRIPTION_AMOUNT_TOLERANCE = 0.05

# categories that are bills by nature, even when the amount is fixed
# (rent is a bill, not a "subscription", despite the constant amount)
BILL_CATEGORIES = {"Rent", "Utility"}

# monthly cost factor per cadence
_MONTHLY_FACTOR = {"weekly": 30 / 7, "monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12}


def _match_cadence(gaps: list[int]) -> str | None:
    """Return the cadence name if >=80% of gaps fall inside its tolerance."""
    for name, (days, tol) in CADENCES.items():
        hits = sum(1 for g in gaps if abs(g - days) <= tol)
        if hits >= max(2, round(0.8 * len(gaps))):
            return name
    return None


def detect_recurring() -> list[dict]:
    """Scan all counted spending rows and return detected recurring items,
    sorted by estimated monthly cost (descending)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT merchant_normalized, merchant_raw, date, signed_amount, category "
        "FROM transactions "
        "WHERE is_spending=1 AND signed_amount > 0 AND merchant_normalized != '' "
        "ORDER BY merchant_normalized, date"
    ).fetchall()
    conn.close()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["merchant_normalized"], []).append(r)

    out = []
    for merchant, g in groups.items():
        if len(g) < MIN_OCCURRENCES:
            continue
        dates = sorted({date.fromisoformat(r["date"]) for r in g})
        if len(dates) < MIN_OCCURRENCES:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        cadence = _match_cadence(gaps)
        if not cadence:
            continue

        amounts = [r["signed_amount"] for r in g]
        med = median(amounts)
        fixed = med > 0 and all(
            abs(a - med) <= SUBSCRIPTION_AMOUNT_TOLERANCE * med for a in amounts
        )
        category = g[-1]["category"]
        kind = "bill" if (category in BILL_CATEGORIES or not fixed) else "subscription"
        last = dates[-1]
        days, tol = CADENCES[cadence]
        next_expected = last + timedelta(days=days)
        # Active = the next charge isn't overdue beyond the cadence tolerance.
        # E.g. a monthly subscription is inactive once >37 days pass with no
        # charge (canceled / ended) -- listed greyed out, not in the total.
        active = (date.today() - last).days <= days + tol
        out.append({
            "merchant": g[-1]["merchant_raw"] or merchant,
            "category": category,
            "kind": kind,
            "cadence": cadence,
            "typical_amount": round(med, 2),
            "occurrences": len(dates),
            "last_date": last.isoformat(),
            "next_expected": next_expected.isoformat(),
            "monthly_cost": round(med * _MONTHLY_FACTOR[cadence], 2),
            "active": active,
        })
    out.sort(key=lambda x: (not x["active"], -x["monthly_cost"]))
    return out
