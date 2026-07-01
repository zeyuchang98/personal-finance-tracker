"""Plaid connection, token management, transaction pulling.

Uses the transactions/sync endpoint with a persisted cursor per item.
Access tokens are persisted in the DB (items table) so multi-institution
connections survive restarts.
"""
import os

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.sandbox_public_token_create_request import (
    SandboxPublicTokenCreateRequest,
)
from plaid.model.sandbox_public_token_create_request_options import (
    SandboxPublicTokenCreateRequestOptions,
)
from plaid.model.sandbox_public_token_create_request_options_transactions import (
    SandboxPublicTokenCreateRequestOptionsTransactions,
)
from plaid.model.link_token_transactions import LinkTokenTransactions

from db import get_conn

_ENV_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def _client():
    env = os.environ.get("PLAID_ENV", "sandbox")
    config = plaid.Configuration(
        host=_ENV_HOSTS.get(env, plaid.Environment.Sandbox),
        api_key={
            "clientId": os.environ["PLAID_CLIENT_ID"],
            "secret": os.environ["PLAID_SECRET"],
        },
    )
    return plaid_api.PlaidApi(plaid.ApiClient(config))


def create_link_token(user_id: str = "local-user") -> str:
    products = [Products(p) for p in os.environ.get("PLAID_PRODUCTS", "transactions").split(",")]
    countries = [CountryCode(c) for c in os.environ.get("PLAID_COUNTRY_CODES", "US").split(",")]
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name="Personal Finance Tracker",
        products=products,
        country_codes=countries,
        language="en",
        # Default history window is only 90 days; recurring detection and
        # yearly comparison need much more.
        transactions=LinkTokenTransactions(days_requested=730),
    )
    return _client().link_token_create(req).link_token


def exchange_public_token(public_token: str, institution: str | None = None) -> str:
    """Exchange a Link public_token for an access_token and persist it."""
    resp = _client().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    access_token = resp.access_token
    item_id = resp.item_id
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
        (item_id, access_token, institution),
    )
    conn.commit()
    conn.close()
    return access_token


def sandbox_create_item(institution_id: str = "ins_109508",
                        institution_name: str = "First Platypus Bank") -> str:
    """Sandbox-only: create an item directly (no Link UI) with built-in test
    transactions, then persist the access token. Returns the access_token.

    ins_109508 = First Platypus Bank (non-OAuth, stable for custom/test data).
    """
    client = _client()
    products = [Products(p) for p in os.environ.get("PLAID_PRODUCTS", "transactions").split(",")]
    pt = client.sandbox_public_token_create(
        SandboxPublicTokenCreateRequest(
            institution_id=institution_id,
            initial_products=products,
        )
    ).public_token
    return exchange_public_token(pt, institution_name)


def sandbox_create_custom_item(institution_id: str = "ins_109508",
                               institution_name: str = "Sandbox Custom (de-noise)") -> str:
    """Sandbox-only: create an item for the *custom user* whose accounts and
    transactions are defined by sandbox_seed.build_config(). This seeds every
    de-noise test case (cc payment pair, P2P, transfer, investment, income,
    ATM cash, refund) with known amounts. Returns the access_token.
    """
    from datetime import date, timedelta

    from sandbox_seed import build_config

    client = _client()
    products = [Products(p) for p in os.environ.get("PLAID_PRODUCTS", "transactions").split(",")]
    pt = client.sandbox_public_token_create(
        SandboxPublicTokenCreateRequest(
            institution_id=institution_id,
            initial_products=products,
            options=SandboxPublicTokenCreateRequestOptions(
                override_username="user_custom",
                override_password=build_config(),
                # Default window is 90 days, which silently drops the older
                # seeded recurring history -- request a full 2 years.
                transactions=SandboxPublicTokenCreateRequestOptionsTransactions(
                    start_date=date.today() - timedelta(days=730),
                    end_date=date.today(),
                ),
            ),
        )
    ).public_token
    return exchange_public_token(pt, institution_name)


def _classify_account_type(acct) -> str:
    t = str(getattr(acct, "type", "")).lower()
    subtype = str(getattr(acct, "subtype", "")).lower()
    if t == "credit" or subtype in ("credit card", "credit"):
        return "credit"
    if subtype == "savings":
        return "savings"
    return "checking"


def _sync_accounts(client, access_token, item_id):
    resp = client.accounts_get(AccountsGetRequest(access_token=access_token))
    conn = get_conn()
    for a in resp.accounts:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (account_id, item_id, name, account_type, mask) "
            "VALUES (?, ?, ?, ?, ?)",
            (a.account_id, item_id, a.name, _classify_account_type(a),
             getattr(a, "mask", None)),
        )
    conn.commit()
    conn.close()


def fetch_new_transactions():
    """Pull added/modified/removed transactions for every stored item via sync
    cursors. Returns (upserts, removed_ids):

      upserts      list of txn dicts (added + modified) for the sync layer
      removed_ids  transaction_ids Plaid says are gone (e.g. a pending auth that
                   has since posted under a new id) -- the sync layer deletes them

    Advances and persists each item's cursor.
    """
    client = _client()
    conn = get_conn()
    items = conn.execute("SELECT item_id, access_token, cursor FROM items").fetchall()
    conn.close()

    # account_id -> account_type lookup, refreshed per run
    out = []
    removed_ids = []
    for item in items:
        access_token = item["access_token"]
        item_id = item["item_id"]
        _sync_accounts(client, access_token, item_id)

        conn = get_conn()
        acct_types = {
            r["account_id"]: r["account_type"]
            for r in conn.execute("SELECT account_id, account_type FROM accounts").fetchall()
        }
        conn.close()

        cursor = item["cursor"] or None
        has_more = True
        while has_more:
            kwargs = {"access_token": access_token}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))
            for t in resp.added:
                out.append(_to_dict(t, acct_types))
            for t in resp.modified:  # same id, updated fields (e.g. amount finalized)
                out.append(_to_dict(t, acct_types))
            for t in resp.removed:   # e.g. a pending txn replaced by its posted version
                removed_ids.append(t.transaction_id)
            cursor = resp.next_cursor
            has_more = resp.has_more

        conn = get_conn()
        conn.execute("UPDATE items SET cursor=? WHERE item_id=?", (cursor, item_id))
        conn.commit()
        conn.close()

    return out, removed_ids


def _to_dict(t, acct_types):
    merchant = getattr(t, "merchant_name", None) or t.name
    pfc = getattr(t, "personal_finance_category", None)
    # Store "PRIMARY/DETAILED" so the de-noise layer can use the detailed code
    # (e.g. LOAN_PAYMENTS/LOAN_PAYMENTS_CREDIT_CARD_PAYMENT).
    pfc_str = None
    if pfc:
        detailed = getattr(pfc, "detailed", None)
        pfc_str = f"{pfc.primary}/{detailed}" if detailed else pfc.primary
    return {
        "txn_id": t.transaction_id,
        "date": str(t.date),
        "merchant_raw": merchant,
        "amount": float(t.amount),  # Plaid: positive = money out
        "account": t.account_id,
        "account_type": acct_types.get(t.account_id, "checking"),
        "pfc": pfc_str,
        "pending": 1 if getattr(t, "pending", False) else 0,
        # set on a POSTED txn, points to the earlier PENDING txn it replaces
        "pending_transaction_id": getattr(t, "pending_transaction_id", None) or None,
    }
