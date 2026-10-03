\
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(immediate: bool = False):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                birth_date TEXT NOT NULL,
                city_france TEXT NOT NULL,

                username TEXT UNIQUE,
                email TEXT UNIQUE,

                registration_status TEXT NOT NULL DEFAULT 'PENDING',
                email_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                profile_status TEXT NOT NULL DEFAULT 'NOT_STARTED',

                comment_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE',
                top50_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE',
                dissatisfied_at TEXT,
                dissatisfied_reason TEXT,

                selected_date TEXT,
                selected_at TEXT,
                form_prepared_at TEXT,
                account_created_at TEXT,
                email_confirmed_at TEXT,
                profile_completed_at TEXT,

                registration_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS member_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_members_registration_status
            ON members(registration_status);

            CREATE INDEX IF NOT EXISTS idx_members_selected_date
            ON members(selected_date);

            CREATE INDEX IF NOT EXISTS idx_member_events_member_id
            ON member_events(member_id);

            CREATE TABLE IF NOT EXISTS comment_scouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                advertisement_title TEXT NOT NULL,
                target_url TEXT NOT NULL,
                expected_comment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                published_at TEXT NOT NULL,
                checks_done INTEGER NOT NULL DEFAULT 0,
                max_checks INTEGER NOT NULL DEFAULT 2,
                next_check_at TEXT,
                last_checked_at TEXT,
                found_at TEXT,
                expired_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS comment_scout_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scout_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                result TEXT NOT NULL,
                details TEXT,
                FOREIGN KEY(scout_id) REFERENCES comment_scouts(id) ON DELETE CASCADE,
                UNIQUE(scout_id, attempt_no)
            );

            CREATE INDEX IF NOT EXISTS idx_comment_scouts_due
            ON comment_scouts(status, next_check_at);

            CREATE INDEX IF NOT EXISTS idx_comment_scouts_member
            ON comment_scouts(member_id);

            CREATE INDEX IF NOT EXISTS idx_comment_scout_checks_scout
            ON comment_scout_checks(scout_id, attempt_no);

            CREATE TABLE IF NOT EXISTS comment_professionals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                target_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comment_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_no INTEGER NOT NULL UNIQUE,
                professional_id INTEGER NOT NULL,
                age_band TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE',
                author_member_id INTEGER,
                status TEXT NOT NULL DEFAULT 'AVAILABLE',
                schedule_date TEXT,
                scheduled_for TEXT,
                submitted_at TEXT,
                published_at TEXT,
                scout_id INTEGER,
                confirmation_requested_at TEXT,
                confirmed_at TEXT,
                confirmed_by_member_id INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(professional_id) REFERENCES comment_professionals(id) ON DELETE CASCADE,
                FOREIGN KEY(author_member_id) REFERENCES members(id) ON DELETE SET NULL,
                FOREIGN KEY(scout_id) REFERENCES comment_scouts(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_comment_bank_due ON comment_bank(status, scheduled_for);
            CREATE INDEX IF NOT EXISTS idx_comment_bank_professional ON comment_bank(professional_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_comment_bank_unique_author
            ON comment_bank(author_member_id) WHERE author_member_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS comment_member_usage (
                member_id INTEGER PRIMARY KEY,
                comment_id INTEGER NOT NULL,
                used_at TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'SUBMITTED',
                FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE,
                FOREIGN KEY(comment_id) REFERENCES comment_bank(id) ON DELETE CASCADE
            );
            """
        )

        professional_columns = {row["name"] for row in conn.execute("PRAGMA table_info(comment_professionals)")}
        if "feedback_kind" not in professional_columns:
            conn.execute("ALTER TABLE comment_professionals ADD COLUMN feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE'")

        comment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(comment_bank)")}
        if "feedback_kind" not in comment_columns:
            conn.execute("ALTER TABLE comment_bank ADD COLUMN feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE'")
        for column, definition in (("confirmation_requested_at","TEXT"),("confirmed_at","TEXT"),("confirmed_by_member_id","INTEGER")):
            if column not in comment_columns:
                conn.execute(f"ALTER TABLE comment_bank ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_professionals_feedback_kind "
            "ON comment_professionals(feedback_kind, enabled)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_bank_feedback_kind "
            "ON comment_bank(feedback_kind, professional_id, status)"
        )
        if settings.comment_require_author_confirmation:
            conn.execute(
                "UPDATE comment_bank SET status='AWAITING_CONFIRMATION', scheduled_for=NULL, confirmation_requested_at=COALESCE(confirmation_requested_at, updated_at) WHERE status='AVAILABLE' AND author_member_id IS NOT NULL AND confirmed_at IS NULL"
            )
        else:
            now = utc_now()
            conn.execute(
                """UPDATE comment_bank
                   SET status='AVAILABLE',
                       scheduled_for=COALESCE(scheduled_for, ?),
                       confirmation_requested_at=NULL,
                       confirmed_at=COALESCE(confirmed_at, ?),
                       confirmed_by_member_id=COALESCE(confirmed_by_member_id, author_member_id),
                       updated_at=?
                   WHERE author_member_id IS NOT NULL
                     AND submitted_at IS NULL
                     AND confirmed_at IS NULL
                     AND status IN ('AVAILABLE', 'AWAITING_CONFIRMATION')""",
                (now, now, now),
            )

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_unconfirmed_comment_schedule
            AFTER UPDATE OF scheduled_for ON comment_bank
            WHEN NEW.scheduled_for IS NOT NULL
                 AND NEW.confirmed_at IS NULL
                 AND NEW.status='AVAILABLE'
            BEGIN
                UPDATE comment_bank
                SET status='AWAITING_CONFIRMATION',
                    scheduled_for=NULL,
                    confirmation_requested_at=COALESCE(confirmation_requested_at, updated_at)
                WHERE id=NEW.id;
            END
        """)

        # Comment eligibility is independent from account/profile status.
        # Existing members remain eligible unless explicitly marked dissatisfied.
        member_columns = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
        if "comment_eligibility" not in member_columns:
            conn.execute(
                "ALTER TABLE members ADD COLUMN comment_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE'"
            )
        if "dissatisfied_at" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN dissatisfied_at TEXT")
        if "top50_eligibility" not in member_columns:
            conn.execute(
                "ALTER TABLE members ADD COLUMN top50_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE'"
            )
        if "dissatisfied_reason" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN dissatisfied_reason TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_comment_eligibility "
            "ON members(comment_eligibility)"
        )

        # Registration routing metadata. Existing historical selections are
        # treated as DIRECT because the VPN worker did not exist yet.
        member_columns = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
        if "registration_route" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN registration_route TEXT")
        if "registration_target" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN registration_target TEXT")
        if "registration_worker_job_id" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN registration_worker_job_id TEXT")
        if "registration_worker_action" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN registration_worker_action TEXT")
        if "registration_egress_ip" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN registration_egress_ip TEXT")
        conn.execute(
            "UPDATE members SET registration_route='DIRECT' "
            "WHERE selected_at IS NOT NULL AND registration_route IS NULL"
        )
        conn.execute(
            "UPDATE members SET registration_target='PRIMARY' "
            "WHERE selected_at IS NOT NULL AND registration_target IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_registration_route "
            "ON members(registration_target, registration_route, selected_date)"
        )

        # Keep active Scouts aligned with the configured global rule.
        conn.execute(
            "UPDATE comment_scouts SET max_checks=? WHERE status='ACTIVE' AND max_checks<>?",
            (settings.scout_max_checks, settings.scout_max_checks),
        )


def add_event(conn: sqlite3.Connection, member_id: int, event_type: str, message: str = ""):
    conn.execute(
        """
        INSERT INTO member_events(member_id, event_type, message, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (member_id, event_type, message, utc_now()),
    )
