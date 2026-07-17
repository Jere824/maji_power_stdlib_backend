import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "utilities.db")


def get_connection():
    """Open a SQLite connection with dictionary-like rows."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def table_has_column(conn, table_name, column_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def initialize_database():
    """
    Create the core database tables if they do not already exist.
    This file uses only Python's built-in sqlite3 module.
    """
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                phone       TEXT    NOT NULL UNIQUE,
                role        TEXT    NOT NULL CHECK(role IN ('customer', 'meter_reader', 'billing_officer', 'admin')),
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS utility_accounts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                account_number TEXT    NOT NULL UNIQUE,
                meter_number   TEXT    NOT NULL UNIQUE,
                service_type   TEXT    NOT NULL CHECK(service_type IN ('water', 'electricity')),
                address        TEXT    NOT NULL,
                customer_id    INTEGER NOT NULL REFERENCES users(id),
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS meter_readings (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                utility_account_id  INTEGER NOT NULL REFERENCES utility_accounts(id),
                previous_reading    REAL    NOT NULL,
                current_reading     REAL    NOT NULL,
                consumption         REAL    GENERATED ALWAYS AS (current_reading - previous_reading) STORED,
                reading_date        TEXT    NOT NULL,
                read_by_user_id     INTEGER NOT NULL REFERENCES users(id),
                notes               TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bills (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                utility_account_id  INTEGER NOT NULL REFERENCES utility_accounts(id),
                meter_reading_id    INTEGER REFERENCES meter_readings(id),
                billing_period      TEXT    NOT NULL,
                previous_balance    REAL    NOT NULL DEFAULT 0,
                consumption_charge  REAL    NOT NULL DEFAULT 0,
                fixed_charge        REAL    NOT NULL DEFAULT 0,
                tax_amount          REAL    NOT NULL DEFAULT 0,
                penalty_amount      REAL    NOT NULL DEFAULT 0,
                total_due           REAL    NOT NULL DEFAULT 0,
                status              TEXT    NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','issued','paid','partially_paid','overdue')),
                due_date            TEXT    NOT NULL,
                created_by_user_id  INTEGER NOT NULL REFERENCES users(id),
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payments (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id             INTEGER NOT NULL REFERENCES bills(id),
                amount              REAL    NOT NULL,
                payment_method      TEXT    NOT NULL CHECK(payment_method IN ('mobile_money', 'card', 'bank')),
                status              TEXT    NOT NULL DEFAULT 'initiated'
                                    CHECK(status IN ('initiated','pending','confirmed','failed','cancelled')),
                provider_reference  TEXT,
                receipt_id          TEXT UNIQUE,
                paid_at             TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS receipts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_number      TEXT    NOT NULL UNIQUE,
                payment_id          INTEGER NOT NULL UNIQUE REFERENCES payments(id),
                bill_id             INTEGER NOT NULL REFERENCES bills(id),
                amount              REAL    NOT NULL,
                provider_reference  TEXT,
                issued_at           TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tariffs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                service_type        TEXT    NOT NULL CHECK(service_type IN ('water', 'electricity')),
                price_per_unit      REAL    NOT NULL,
                fixed_charge        REAL    NOT NULL DEFAULT 0,
                tax_rate            REAL    NOT NULL DEFAULT 0,
                overdue_penalty_flat REAL   NOT NULL DEFAULT 0,
                grace_period_days   INTEGER NOT NULL DEFAULT 14,
                active_from         TEXT    NOT NULL DEFAULT (date('now')),
                active_to           TEXT,
                created_by_user_id  INTEGER REFERENCES users(id),
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payment_status_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id  INTEGER NOT NULL REFERENCES payments(id),
                status      TEXT    NOT NULL,
                detail      TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS disputes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id             INTEGER NOT NULL REFERENCES bills(id),
                raised_by_user_id   INTEGER NOT NULL REFERENCES users(id),
                reason              TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'open'
                                    CHECK(status IN ('open','under_review','resolved','rejected')),
                resolution_note     TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS notification_queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER REFERENCES users(id),
                bill_id             INTEGER REFERENCES bills(id),
                channel             TEXT    NOT NULL DEFAULT 'in_app',
                message             TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'queued'
                                    CHECK(status IN ('queued','sent','failed')),
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                sent_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER REFERENCES users(id),
                action      TEXT    NOT NULL,
                target      TEXT    NOT NULL,
                detail      TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_bills_period ON bills(billing_period);
            CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(status);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_meter_account_date ON meter_readings(utility_account_id, reading_date);
            """
        )

        # Seed default users and demo data only when the users table is empty.
        user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
        if user_count == 0:
            seed_sample_data(conn)

        tariff_count = conn.execute("SELECT COUNT(*) AS count FROM tariffs").fetchone()["count"]
        if tariff_count == 0:
            conn.executemany(
                """
                INSERT INTO tariffs
                (service_type, price_per_unit, fixed_charge, tax_rate, overdue_penalty_flat, grace_period_days, created_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("water", 80.0, 150.0, 0.16, 100.0, 14, 5),
                    ("electricity", 25.0, 200.0, 0.16, 150.0, 14, 5),
                ],
            )

        conn.commit()


def seed_sample_data(conn):
    now = datetime.now()
    conn.executemany(
        "INSERT INTO users (name, phone, role) VALUES (?, ?, ?)",
        [
            ("Amina Customer", "+254700000001", "customer"),
            ("Brian Meter Reader", "+254700000002", "meter_reader"),
            ("Carol Billing Officer", "+254700000003", "billing_officer"),
            ("Daniel Customer", "+254700000004", "customer"),
            ("Eunice Admin", "+254700000005", "admin"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO utility_accounts
        (account_number, meter_number, service_type, address, customer_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("ACC-1001", "MTR-W-1001", "water", "Westlands Block A", 1),
            ("ACC-1002", "MTR-E-1002", "electricity", "Kilimani House 7", 4),
            ("ACC-1003", "MTR-W-1003", "water", "Rongai Plot 12", 1),
        ],
    )
    conn.executemany(
        """
        INSERT INTO meter_readings
        (utility_account_id, previous_reading, current_reading, reading_date, read_by_user_id, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 120, 138, (now - timedelta(days=5)).strftime("%Y-%m-%d"), 2, "Normal reading"),
            (2, 580, 642, (now - timedelta(days=4)).strftime("%Y-%m-%d"), 2, "Normal reading"),
            (3, 80, 145, (now - timedelta(days=3)).strftime("%Y-%m-%d"), 2, "Possible high usage"),
        ],
    )
