"""Plaid Sandbox custom-user seed data for the de-noise layer.

Builds the JSON configuration object passed as `override_password` (with
`override_username="user_custom"`) to /sandbox/public_token/create, so the
Sandbox item contains a known transaction set covering every de-noise case:

  1. credit-card payment dedup  (checking outflow + credit inflow pair)
  2. P2P in/out                 (Venmo: outflow = spending, inflow = offset)
  3. transfer between own accounts (checking -> savings, both legs)
  4. investment contribution    (Vanguard)
  5. income                     (payroll deposits)
  6. ATM cash withdrawal
  7. merchant refund            (Amazon refund offset)
plus normal spending across categories (Rent, Utility, Grocery, Dining,
Shopping, Subscription, Transport).

Amount sign convention matches the Plaid Transactions API output: positive =
money leaving the account (debit), negative = money in (credit).

Dates are generated relative to today (spread over the last ~5 weeks) so the
data always lands inside Plaid's accepted window and shows up in the current
and previous month views.
"""
import json
from datetime import date, timedelta


def _d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _txn(days_ago: int, description: str, amount: float) -> dict:
    return {
        "date_transacted": _d(days_ago),
        "date_posted": _d(days_ago),
        "amount": amount,
        "description": description,
        "currency": "USD",
    }


# Expected outcome per row (kept next to the data so the verifier and a human
# can read them together): (description, amount, expected exclude_reason or
# "spend"/"offset").
CHECKING_TXNS = [
    (36, "ACME CORP PAYROLL DIRECT DEP", -2500.00),   # income (excluded)
    (30, "OAKWOOD APARTMENTS RENT", 1500.00),         # spend: Rent
    (25, "PG&E UTILITY BILL", 90.00),                 # spend: Utility
    (20, "ONLINE TRANSFER TO SAVINGS", 500.00),       # transfer (excluded)
    (18, "VANGUARD INVESTMENT BUY", 400.00),          # investment (excluded)
    (15, "ATM WITHDRAWAL", 100.00),                   # cash (excluded)
    (12, "VENMO PAYMENT TO JOHN", 60.00),             # p2p out -> spending, review
    (10, "VENMO FROM ALICE", -45.00),                 # p2p in -> offset, review
    (7, "CHASE CREDIT CARD PAYMENT", 800.00),         # cc_payment (excluded)
    (5, "ACME CORP PAYROLL DIRECT DEP", -2500.00),    # income (excluded)
    # --- recurring history (for recurring detection) ---
    # NOTE: Plaid sandbox custom users hard-cap history at 90 days
    # (options.transactions.start_date is ignored for user_custom), so ALL
    # seed dates must stay under ~88 days. 3 monthly charges fit in ~60 days.
    # rent: fixed amount, monthly -> recurring (fixed)
    (60, "OAKWOOD APARTMENTS RENT", 1500.00),
    (88, "OAKWOOD APARTMENTS RENT", 1500.00),
    # utility: varying amount, monthly -> recurring "bill"
    (55, "PG&E UTILITY BILL", 84.12),
    (85, "PG&E UTILITY BILL", 102.30),
]

SAVINGS_TXNS = [
    (20, "TRANSFER FROM CHECKING", -500.00),          # transfer (excluded)
]

CREDIT_TXNS = [
    (28, "WHOLE FOODS #123", 110.00),                 # spend: Grocery
    (22, "CHIPOTLE 1234", 25.50),                     # spend: Dining
    (16, "AMAZON.COM*ORDER", 80.00),                  # spend: Shopping
    (11, "AMAZON.COM REFUND", -30.00),                # refund -> offset
    (9, "NETFLIX.COM", 15.49),                        # spend: Subscription
    (8, "UBER TRIP", 18.75),                          # spend: Transport
    (6, "PAYMENT THANK YOU - WEB", -800.00),          # cc_payment (excluded)
    # --- recurring history (for recurring detection) ---
    # Netflix: fixed monthly subscription
    (39, "NETFLIX.COM", 15.49),
    (69, "NETFLIX.COM", 15.49),
    # Spotify: fixed monthly subscription
    (4, "SPOTIFY USA", 9.99),
    (34, "SPOTIFY USA", 9.99),
    (64, "SPOTIFY USA", 9.99),
    # Gym: fixed monthly membership
    (13, "24 HOUR FITNESS", 45.00),
    (43, "24 HOUR FITNESS", 45.00),
    (74, "24 HOUR FITNESS", 45.00),
    # Blue Apron: CANCELED weekly subscription (last charge ~6 weeks ago) ->
    # must be detected but flagged inactive and excluded from the monthly
    # total. Weekly cadence is used because a canceled MONTHLY subscription
    # cannot fit 3 charges + a >37-day gap inside the 90-day sandbox cap.
    (45, "BLUE APRON", 59.99),
    (52, "BLUE APRON", 59.99),
    (59, "BLUE APRON", 59.99),
    (66, "BLUE APRON", 59.99),
]


def expected_spending_total() -> float:
    """Sum of all rows that should count (spends + offsets), for verification."""
    total = 0.0
    for days_ago, desc, amount in CHECKING_TXNS + SAVINGS_TXNS + CREDIT_TXNS:
        if desc in ("OAKWOOD APARTMENTS RENT", "PG&E UTILITY BILL",
                    "VENMO PAYMENT TO JOHN", "VENMO FROM ALICE",
                    "WHOLE FOODS #123", "CHIPOTLE 1234", "AMAZON.COM*ORDER",
                    "AMAZON.COM REFUND", "NETFLIX.COM", "UBER TRIP",
                    "SPOTIFY USA", "24 HOUR FITNESS", "BLUE APRON"):
            total += amount
    return round(total, 2)


def build_config() -> str:
    """Return the override_password JSON string for the custom Sandbox user."""
    config = {
        "override_accounts": [
            {
                "type": "depository",
                "subtype": "checking",
                "starting_balance": 5000,
                "meta": {"name": "Test Checking"},
                "transactions": [_txn(d, desc, amt) for d, desc, amt in CHECKING_TXNS],
            },
            {
                "type": "depository",
                "subtype": "savings",
                "starting_balance": 8000,
                "meta": {"name": "Test Savings"},
                "transactions": [_txn(d, desc, amt) for d, desc, amt in SAVINGS_TXNS],
            },
            {
                "type": "credit",
                "subtype": "credit card",
                "starting_balance": 250,
                "meta": {"name": "Test Credit Card"},
                "transactions": [_txn(d, desc, amt) for d, desc, amt in CREDIT_TXNS],
            },
        ]
    }
    return json.dumps(config)


if __name__ == "__main__":
    print(build_config())
    print("Expected spending total:", expected_spending_total())
