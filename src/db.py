"""
db.py — SQLite connection and schema initialization.

All ingestion modules call get_connection() to share one database file.
The schema uses a long (tidy) format: one row per company/indicator/year,
which makes it trivial to add new metrics without altering the table.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "esg.db"


def get_connection() -> sqlite3.Connection:
    """Return a connection to the shared SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # allows dict-style access to rows
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
    return conn


def init_db() -> None:
    """Create tables if they don't already exist."""
    conn = get_connection()
    with conn:
        conn.executescript("""
            -- World Bank environmental/social macro indicators (country-level)
            CREATE TABLE IF NOT EXISTS worldbank_indicators (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                country_code    TEXT    NOT NULL,
                country_name    TEXT    NOT NULL,
                indicator_code  TEXT    NOT NULL,
                indicator_name  TEXT    NOT NULL,
                year            INTEGER NOT NULL,
                value           REAL,
                source          TEXT    DEFAULT 'World Bank API',
                ingested_at     TEXT    DEFAULT (datetime('now')),
                UNIQUE (country_code, indicator_code, year)
            );

            -- SEC/EDGAR governance disclosures (company-level)
            CREATE TABLE IF NOT EXISTS sec_filings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                company_name    TEXT    NOT NULL,
                cik             TEXT    NOT NULL,
                form_type       TEXT    NOT NULL,   -- e.g. 10-K, DEF 14A
                filing_date     TEXT    NOT NULL,
                fiscal_year     INTEGER,
                metric_name     TEXT    NOT NULL,   -- e.g. 'board_size', 'audit_fees'
                metric_value    REAL,
                metric_unit     TEXT,               -- e.g. 'USD', 'count', 'ratio'
                source_url      TEXT,
                source          TEXT    DEFAULT 'SEC EDGAR',
                ingested_at     TEXT    DEFAULT (datetime('now')),
                UNIQUE (cik, form_type, fiscal_year, metric_name)
            );

            -- Ingestion run log — useful for debugging and incremental updates
            CREATE TABLE IF NOT EXISTS ingestion_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT    NOT NULL,
                status      TEXT    NOT NULL,   -- 'success' | 'partial' | 'error'
                rows_added  INTEGER DEFAULT 0,
                message     TEXT,
                ran_at      TEXT    DEFAULT (datetime('now'))
            );
        """)
    conn.close()
    print(f"[db] Database ready at {DB_PATH}")


if __name__ == "__main__":
    init_db()
