-- Schema for the personal finance tracker (MVP main chain)

CREATE TABLE IF NOT EXISTS items (
    -- one row per Plaid access token (one institution connection)
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       TEXT UNIQUE,
    access_token  TEXT NOT NULL,
    institution   TEXT,
    cursor        TEXT,              -- transactions/sync cursor
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT UNIQUE,       -- Plaid account_id
    item_id       TEXT,
    name          TEXT,
    account_type  TEXT,              -- checking / savings / credit
    mask          TEXT,              -- last 4 digits, for display
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id              TEXT UNIQUE,        -- Plaid transaction_id
    date                TEXT,
    merchant_raw        TEXT,
    merchant_normalized TEXT,
    amount              REAL,              -- raw Plaid amount (positive = money out)
    account             TEXT,              -- account_id
    account_type        TEXT,              -- checking / savings / credit
    category            TEXT,
    claude_category     TEXT,              -- Claude's ORIGINAL pick; static, never overwritten (NULL if never Claude-classified)
    source              TEXT,              -- plaid / manual
    classified_by       TEXT,              -- rule / claude / manual
    is_spending         INTEGER DEFAULT 1, -- bool: counted in spending report
    signed_amount       REAL,              -- + = spend, - = offset
    exclude_reason      TEXT,              -- refund/investment/transfer/cash/income/cc_payment/p2p/NULL
    needs_review        INTEGER DEFAULT 0,
    pending             INTEGER DEFAULT 0, -- bool: Plaid pending auth (amount may change once it posts)
    linked_txn_id       TEXT,              -- optional: P2P inflow linked to a spend
    pfc                 TEXT               -- Plaid personal_finance_category (raw, for debugging)
);

CREATE TABLE IF NOT EXISTS rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_pattern TEXT UNIQUE,
    category         TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE,
    is_base INTEGER DEFAULT 0
);
