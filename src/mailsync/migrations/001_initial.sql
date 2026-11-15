CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE source_baselines (
    account_id TEXT PRIMARY KEY,
    account_identity TEXT NOT NULL,
    destination_identity TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL CHECK (uidvalidity > 0),
    first_eligible_uid INTEGER NOT NULL CHECK (first_eligible_uid > 0),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'blocked'))
);

CREATE TABLE transfer_attempts (
    account_id TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    source_uid INTEGER NOT NULL,
    marker TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('running', 'confirmed', 'uncertain', 'failed')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_code TEXT,
    PRIMARY KEY (account_id, uidvalidity, source_uid),
    FOREIGN KEY (account_id) REFERENCES source_baselines(account_id)
);

CREATE TABLE copy_records (
    account_id TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    source_uid INTEGER NOT NULL,
    marker TEXT NOT NULL UNIQUE,
    destination_uidvalidity INTEGER NOT NULL,
    destination_uid INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, uidvalidity, source_uid),
    FOREIGN KEY (account_id) REFERENCES source_baselines(account_id)
);

CREATE TABLE cycles (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed'))
);

CREATE TABLE cycle_accounts (
    cycle_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    copied INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    PRIMARY KEY (cycle_id, account_id),
    FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id)
);
