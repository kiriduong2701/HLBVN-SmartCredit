"""SQLite persistence (stdlib sqlite3). Repository functions only - no
business rules live here.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..engine import qr as QR

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY, display_name TEXT, initials TEXT, account_masked TEXT,
  balance_vnd INTEGER NOT NULL, derived_from TEXT, fixture_adjustment TEXT, persona_note TEXT,
  applicant_json TEXT NOT NULL, sources_json TEXT NOT NULL, alternative_data_json TEXT NOT NULL DEFAULT '{}',
  existing_products_json TEXT NOT NULL DEFAULT '[]',
  internal_delinquency_flag INTEGER NOT NULL DEFAULT 0, on_time_streak INTEGER NOT NULL DEFAULT 0,
  identity_json TEXT NOT NULL DEFAULT '{}', fraud_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id TEXT PRIMARY KEY, name TEXT, category TEXT, city TEXT, mcc TEXT, acquirer_bin TEXT,
  merchant_account TEXT, partner_merchant INTEGER NOT NULL DEFAULT 0, partner_program TEXT, initials TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_merchant_account ON merchants(acquirer_bin, merchant_account);
CREATE TABLE IF NOT EXISTS qr_codes (
  qr_ref TEXT PRIMARY KEY, merchant_id TEXT REFERENCES merchants, kind TEXT NOT NULL, amount_vnd INTEGER,
  description TEXT, bill_number TEXT, payload TEXT NOT NULL UNIQUE, simulated_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY, qr_ref TEXT NOT NULL REFERENCES qr_codes, qr_kind TEXT NOT NULL,
  customer_id TEXT REFERENCES customers, merchant_id TEXT, amount_vnd INTEGER NOT NULL, amount_source TEXT,
  description TEXT, method TEXT, tenure_months INTEGER, state TEXT NOT NULL, closure_reason TEXT, payment_ref TEXT,
  pl_decision_json TEXT, terms_json TEXT, agreement_accepted_at TEXT, activation_id TEXT, converted_loan_ref TEXT,
  pay_later_amount_vnd INTEGER, pay_now_amount_vnd INTEGER, timer_paused_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, expires_at TEXT, business_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- At most one non-terminal checkout per dynamic QR order (prevents parallel duplicates).
CREATE UNIQUE INDEX IF NOT EXISTS ux_tx_active_qr ON transactions(qr_ref)
  WHERE state NOT IN ('CANCELLED', 'EXPIRED') AND qr_kind = 'DYNAMIC';
CREATE TABLE IF NOT EXISTS applications (
  application_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers, kind TEXT NOT NULL,
  origin TEXT, origin_transaction_id TEXT, requested_limit_vnd INTEGER, credit_state TEXT NOT NULL, band TEXT,
  reason_code TEXT, readiness_status TEXT, readiness_json TEXT, routing_json TEXT, capacity_json TEXT,
  input_snapshot_json TEXT, info_request_json TEXT, info_submitted_json TEXT, declared_json TEXT,
  acquisition_json TEXT, approved_limit_vnd INTEGER, decided_by TEXT, decision_reason_code TEXT, decision_note TEXT,
  appeal_of TEXT, appeal_count INTEGER NOT NULL DEFAULT 0, reason TEXT,
  assessment_count INTEGER NOT NULL DEFAULT 0, review_started_at TEXT, sla_due_at TEXT, qa_status TEXT, qa_by TEXT, qa_note TEXT,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_lines (
  customer_id TEXT PRIMARY KEY REFERENCES customers, status TEXT NOT NULL, limit_vnd INTEGER NOT NULL,
  activation_id TEXT, approved_at TEXT, assessed_on TEXT, basis_json TEXT, frozen_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exposure_holds (
  hold_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, transaction_id TEXT NOT NULL, amount_vnd INTEGER NOT NULL,
  monthly_instalment_vnd INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hold_active_tx ON exposure_holds(transaction_id) WHERE status = 'HELD';
CREATE TABLE IF NOT EXISTS consents (
  consent_id TEXT PRIMARY KEY, application_id TEXT, customer_id TEXT,
  granted INTEGER NOT NULL, categories_json TEXT NOT NULL, policy_version TEXT, recorded_at TEXT NOT NULL,
  withdrawn_at TEXT, kind TEXT NOT NULL, purpose TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT REFERENCES applications, ts TEXT NOT NULL,
  actor_type TEXT, actor_id TEXT, action TEXT, from_state TEXT, to_state TEXT, rule_ids_json TEXT, note TEXT,
  reason_code TEXT
);
CREATE TABLE IF NOT EXISTS payments (
  payment_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES transactions, order_key TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, attempt_no INTEGER NOT NULL, method TEXT, amount_vnd INTEGER,
  status TEXT NOT NULL, failure_code TEXT, failure_message TEXT, auth_challenge_id TEXT, created_at TEXT NOT NULL,
  completed_at TEXT
);
-- Database-level guarantee: one successful payment per merchant order.
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_success_order ON payments(order_key) WHERE status = 'SUCCEEDED';
CREATE TABLE IF NOT EXISTS auth_challenges (
  challenge_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, transaction_id TEXT NOT NULL, purpose TEXT NOT NULL,
  amount_vnd INTEGER NOT NULL, method TEXT NOT NULL, demo_code TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT, used_at TEXT
);
CREATE TABLE IF NOT EXISTS loans (
  loan_ref TEXT PRIMARY KEY, transaction_id TEXT UNIQUE, customer_id TEXT, principal_vnd INTEGER, terms_json TEXT,
  created_at TEXT, merchant_name TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', closed_at TEXT, origin TEXT,
  purpose TEXT, default_cured_on TEXT
);
CREATE TABLE IF NOT EXISTS loan_instalments (
  loan_ref TEXT NOT NULL REFERENCES loans, number INTEGER NOT NULL, due_date TEXT NOT NULL,
  amount_vnd INTEGER NOT NULL, principal_vnd INTEGER NOT NULL, interest_fee_vnd INTEGER NOT NULL,
  overdue_interest_vnd INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'SCHEDULED',
  paid_at TEXT, paid_on TEXT, repayment_id TEXT, PRIMARY KEY (loan_ref, number)
);
CREATE TABLE IF NOT EXISTS loan_repayments (
  repayment_id TEXT PRIMARY KEY, loan_ref TEXT NOT NULL REFERENCES loans, customer_id TEXT,
  instalments_json TEXT NOT NULL, amount_vnd INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, failure_message TEXT, business_date TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merchant_settlements (
  settlement_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL UNIQUE, merchant_id TEXT, method TEXT,
  gross_vnd INTEGER NOT NULL, discount_vnd INTEGER, net_vnd INTEGER, settle_on TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  notification_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, ts TEXT NOT NULL,
  business_date TEXT, kind TEXT NOT NULL, title_key TEXT NOT NULL, params_json TEXT, link TEXT, dedupe_key TEXT UNIQUE,
  read_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, role TEXT NOT NULL, subject_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT,
  event_type TEXT NOT NULL, transaction_id TEXT, application_id TEXT, from_state TEXT, to_state TEXT,
  rule_ids_json TEXT, details_json TEXT, demo_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TABLE IF NOT EXISTS demo_controls (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

# Tables a demo reset keeps: the append-only audit trail and the id counters
# (so ids are never reused and old audit events stay unambiguous).
PRESERVED_ON_RESET = {"audit_events", "counters", "sessions"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    """Thin wrapper; one connection guarded by a re-entrant lock."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- transactions -------------------------------------------------
    def begin(self) -> None:
        self.lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.lock.release()
            raise

    def commit(self) -> None:
        try:
            self.conn.execute("COMMIT")
        finally:
            self.lock.release()

    def rollback(self) -> None:
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        finally:
            self.lock.release()

    def close(self) -> None:
        self.conn.close()

    # -- generic helpers ----------------------------------------------
    def one(self, sql: str, args: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute(sql, tuple(args)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def run(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(args))

    def wipe(self) -> None:
        """Demo reset: recreate every business table in place. The append-only
        audit log and id counters are preserved (audit B7)."""
        with self.lock:
            names = [r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                 "AND name NOT LIKE 'sqlite_%'")]
            self.conn.execute("PRAGMA foreign_keys = OFF")
            for n in names:
                if n not in PRESERVED_ON_RESET:
                    self.conn.execute('DROP TABLE IF EXISTS "%s"' % n)
            self.conn.executescript(SCHEMA)

    def next_id(self, name: str) -> int:
        self.run("INSERT INTO counters(name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
        return self.one("SELECT value FROM counters WHERE name = ?", (name,))["value"]

    def update(self, table: str, key: str, key_value: Any, fields: Dict[str, Any],
               expected_version: Optional[int] = None) -> int:
        """Optimistic-concurrency update. Returns rows changed."""
        sets = ", ".join("%s = ?" % k for k in fields) + ", version = version + 1, updated_at = ?"
        args = list(fields.values()) + [now_iso(), key_value]
        sql = "UPDATE %s SET %s WHERE %s = ?" % (table, sets, key)
        if expected_version is not None:
            sql += " AND version = ?"
            args.append(expected_version)
        return self.run(sql, args).rowcount

    def audit(self, actor_type: str, actor_id: Optional[str], event_type: str, transaction_id: Optional[str] = None,
              application_id: Optional[str] = None, from_state: Optional[str] = None, to_state: Optional[str] = None,
              rule_ids: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> None:
        epoch = int(self.control("demo_epoch", "0") or 0)
        self.run("INSERT INTO audit_events(ts, actor_type, actor_id, event_type, transaction_id, application_id, "
                 "from_state, to_state, rule_ids_json, details_json, demo_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (now_iso(), actor_type, actor_id, event_type, transaction_id, application_id, from_state, to_state,
                  json.dumps(rule_ids or []), json.dumps(details or {}, ensure_ascii=False, default=str), epoch))

    def control(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.one("SELECT value FROM demo_controls WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_control(self, key: str, value: str) -> None:
        self.run("INSERT INTO demo_controls(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def seed(db: Database, fixtures: Dict[str, Any], epoch: int = 0) -> None:
    with db.lock:
        db.begin()
        try:
            db.set_control("demo_epoch", str(epoch))
            for c in fixtures["customers"]:
                db.run("INSERT INTO customers(customer_id, display_name, initials, account_masked, balance_vnd, derived_from, "
                       "fixture_adjustment, persona_note, applicant_json, sources_json, alternative_data_json, "
                       "existing_products_json, identity_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (c["customer_id"], c["display_name"], c["initials"], c["account_masked"], c["balance_vnd"],
                        c["derived_from"], c.get("fixture_adjustment"), c["persona_note"], json.dumps(c["applicant"]),
                        json.dumps(c["sources"]), json.dumps(c.get("alternative_data") or {}),
                        json.dumps(c.get("existing_hlb_products") or []), json.dumps(c.get("identity") or {})))
            for m in fixtures["merchants"]:
                db.run("INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (m["merchant_id"], m["name"], m["category"], m["city"], m["mcc"], m["acquirer_bin"],
                        m["merchant_account"], 1 if m.get("partner_merchant") else 0, m.get("partner_program"),
                        m["initials"]))
            merchants = {m["merchant_id"]: m for m in fixtures["merchants"]}
            for q in fixtures["qr_codes"]:
                m = merchants[q["merchant_id"]]
                payload = QR.build(m["acquirer_bin"], m["merchant_account"], m["mcc"], m["qr_name"], m["qr_city"],
                                   q.get("amount_vnd"), q.get("bill_number"))
                db.run("INSERT INTO qr_codes VALUES (?,?,?,?,?,?,?,?)",
                       (q["qr_ref"], q["merchant_id"], "DYNAMIC" if q.get("amount_vnd") else "STATIC", q.get("amount_vnd"),
                        q["description"], q.get("bill_number"), payload, q.get("simulated_failures", 0)))
            db.audit("SYSTEM", "seed", "DEMO_DATA_SEEDED", details={"customers": len(fixtures["customers"]),
                                                                     "qr_codes": len(fixtures["qr_codes"]), "epoch": epoch})
            db.commit()
        except Exception:
            db.rollback()
            raise


SCHEMA_VERSION = "5"


def _compatible(path: str) -> bool:
    """True if the file is new or already uses this schema version."""
    try:
        con = sqlite3.connect(path)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not tables:
                return True
            if "demo_controls" not in tables:
                return False
            row = con.execute("SELECT value FROM demo_controls WHERE key = 'schema_version'").fetchone()
            return bool(row and row[0] == SCHEMA_VERSION)
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def open_database(path: str, fixtures: Dict[str, Any]) -> Database:
    import os
    if path != ":memory:" and os.path.exists(path) and not _compatible(path):
        backup = path + ".pre-v%s-backup" % SCHEMA_VERSION
        os.replace(path, backup)  # never silently destroy an older demo database
        print("Existing database uses an older schema; moved to %s and starting fresh." % backup)
    db = Database(path)
    db.set_control("schema_version", SCHEMA_VERSION)
    if db.one("SELECT COUNT(*) AS n FROM customers")["n"] == 0:
        seed(db, fixtures)
    return db
