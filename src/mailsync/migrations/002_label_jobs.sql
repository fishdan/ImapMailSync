CREATE TABLE label_jobs (
    account_id TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    source_uid INTEGER NOT NULL,
    marker TEXT NOT NULL UNIQUE,
    label_name TEXT NOT NULL,
    destination_uidvalidity INTEGER,
    destination_uid INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'missing', 'expired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    PRIMARY KEY (account_id, uidvalidity, source_uid),
    FOREIGN KEY (account_id, uidvalidity, source_uid)
        REFERENCES transfer_attempts(account_id, uidvalidity, source_uid),
    CHECK ((destination_uidvalidity IS NULL AND destination_uid IS NULL)
           OR (destination_uidvalidity > 0 AND destination_uid > 0))
);

CREATE INDEX label_jobs_status_account ON label_jobs(status, account_id);
