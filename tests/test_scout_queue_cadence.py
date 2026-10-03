import sqlite3
import unittest
from datetime import datetime, timezone

from app.comment_automation.service import (
    comment_publish_block,
    reconcile_scheduled_comments,
    schedule_next_comment_after_scout_found,
)
from app.scout.service import retry_after_comment_submission


class ScoutQueueCadenceTests(unittest.TestCase):
    def test_found_scout_blocks_next_comment_for_one_hour(self):
        block = comment_publish_block(
            professional_id=7,
            now_utc=datetime(2026, 9, 15, 16, 30, tzinfo=timezone.utc),
            previous_comments=[
                {
                    "professional_id": 7,
                    "submitted_at": "2026-09-15T10:00:00+00:00",
                    "scout_found_at": "2026-09-15T16:00:00+00:00",
                    "scout_found_attempt": 1,
                }
            ],
        )
        self.assertEqual(
            block,
            ("scout_found_interval", "2026-09-15T17:00:00+00:00"),
        )

    def test_next_comment_is_allowed_after_one_hour(self):
        block = comment_publish_block(
            professional_id=7,
            now_utc=datetime(2026, 9, 15, 17, 1, tzinfo=timezone.utc),
            previous_comments=[
                {
                    "professional_id": 7,
                    "submitted_at": "2026-09-15T10:00:00+00:00",
                    "scout_found_at": "2026-09-15T16:00:00+00:00",
                    "scout_found_attempt": 1,
                }
            ],
        )
        self.assertIsNone(block)

    def test_second_check_found_keeps_eight_hour_interval(self):
        block = comment_publish_block(
            professional_id=7,
            now_utc=datetime(2026, 9, 15, 17, 1, tzinfo=timezone.utc),
            previous_comments=[
                {
                    "professional_id": 7,
                    "submitted_at": "2026-09-15T10:00:00+00:00",
                    "scout_found_at": "2026-09-15T16:00:00+00:00",
                    "scout_found_attempt": 2,
                }
            ],
        )
        self.assertEqual(
            block,
            ("professional_interval", "2026-09-15T18:00:00+00:00"),
        )
    def test_found_scout_schedules_next_available_comment(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY,
                professional_id INTEGER NOT NULL,
                source_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                author_member_id INTEGER,
                confirmed_at TEXT,
                scheduled_for TEXT,
                submitted_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, source_no, status, author_member_id,
                 confirmed_at, updated_at)
            VALUES (1, 7, 10, 'USED', 41, '2026-09-15T10:00:00+00:00', 'x')
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, source_no, status, author_member_id,
                 confirmed_at, updated_at)
            VALUES (2, 7, 11, 'AVAILABLE', 42, '2026-09-15T09:00:00+00:00', 'x')
            """
        )
        conn.commit()

        scheduled = schedule_next_comment_after_scout_found(
            conn, professional_id=7, found_at="2026-09-15T16:00:00+00:00"
        )
        row = conn.execute(
            "SELECT scheduled_for FROM comment_bank WHERE id=2"
        ).fetchone()
        self.assertEqual(scheduled, "2026-09-15T17:00:00+00:00")
        self.assertEqual(row["scheduled_for"], scheduled)
        conn.close()

    def test_reconciles_legacy_future_schedule_to_next_eligible_time(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            INSERT INTO comment_professionals(id, name) VALUES (7, 'Agnes');
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY,
                professional_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                author_member_id INTEGER,
                confirmed_at TEXT,
                scheduled_for TEXT,
                submitted_at TEXT,
                scout_id INTEGER,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE comment_scouts (
                id INTEGER PRIMARY KEY,
                found_at TEXT,
                checks_done INTEGER NOT NULL,
                status TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, status, author_member_id, confirmed_at,
                 submitted_at, scout_id, updated_at)
            VALUES (1, 7, 'USED', 41, '2026-09-18T05:00:00+00:00',
                    '2026-09-18T05:00:00+00:00', 10, '2026-09-18T05:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO comment_scouts(id, found_at, checks_done)
            VALUES (10, '2026-09-18T05:10:00+00:00', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, status, author_member_id, confirmed_at,
                 scheduled_for, updated_at)
            VALUES (2, 7, 'AVAILABLE', 42, '2026-09-17T23:00:00+00:00',
                    '2026-09-19T08:00:00+00:00', '2026-09-17T23:30:00+00:00')
            """
        )
        conn.commit()

        repaired = reconcile_scheduled_comments(
            conn, now_utc=datetime(2026, 9, 18, 12, 32, tzinfo=timezone.utc)
        )
        row = conn.execute(
            "SELECT scheduled_for FROM comment_bank WHERE id=2"
        ).fetchone()
        self.assertEqual(repaired, 1)
        self.assertEqual(row["scheduled_for"], "2026-09-18T12:32:00+00:00")
        conn.close()

    def test_expired_scout_requeues_comment_after_24_hours_from_submission(self):
        self.assertEqual(
            retry_after_comment_submission("2026-09-25T17:16:53+00:00"),
            "2026-09-26T17:16:53+00:00",
        )

    def test_expired_scout_keeps_member_assignment_for_automatic_retry(self):
        from pathlib import Path
        source = (Path(__file__).parents[1] / "app" / "scout" / "service.py").read_text()
        self.assertIn("scheduled_for=?, submitted_at=NULL, published_at=NULL, scout_id=NULL", source)
        self.assertNotIn("status='AVAILABLE', author_member_id=NULL", source)

    def test_special_professionals_use_seven_and_eight_hour_scout_offsets(self):
        from app.scout.service import scout_check_offsets_for_professional
        self.assertEqual(scout_check_offsets_for_professional("Brenda"), (420, 480))
        self.assertEqual(scout_check_offsets_for_professional("Mariela"), (420, 480))
        self.assertEqual(scout_check_offsets_for_professional("Brenda 2"), (420, 480))
        self.assertEqual(scout_check_offsets_for_professional("Agnes"), (360, 480))

    def test_special_professional_waits_24_hours_after_found(self):
        block = comment_publish_block(
            professional_id=7,
            professional_name="Brenda",
            now_utc=datetime(2026, 9, 16, 15, 59, tzinfo=timezone.utc),
            previous_comments=[
                {
                    "professional_id": 7,
                    "professional_name": "Brenda",
                    "submitted_at": "2026-09-15T10:00:00+00:00",
                    "scout_found_at": "2026-09-15T16:00:00+00:00",
                    "scout_found_attempt": 1,
                    "scout_status": "FOUND",
                }
            ],
        )
        self.assertEqual(
            block,
            ("scout_found_interval", "2026-09-16T16:00:00+00:00"),
        )

    def test_special_professional_advances_after_second_check_not_found(self):
        block = comment_publish_block(
            professional_id=7,
            professional_name="Brenda",
            now_utc=datetime(2026, 9, 15, 10, 5, tzinfo=timezone.utc),
            previous_comments=[
                {
                    "professional_id": 7,
                    "professional_name": "Brenda",
                    "submitted_at": "2026-09-15T10:00:00+00:00",
                    "scout_found_at": None,
                    "scout_found_attempt": 2,
                    "scout_status": "EXPIRED",
                }
            ],
        )
        self.assertIsNone(block)

    def test_expired_scout_schedules_next_comment_immediately(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY,
                professional_id INTEGER NOT NULL,
                source_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                author_member_id INTEGER,
                confirmed_at TEXT,
                scheduled_for TEXT,
                submitted_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, source_no, status, author_member_id,
                 confirmed_at, updated_at)
            VALUES (1, 7, 10, 'USED', 41, '2026-09-15T10:00:00+00:00', 'x')
            """
        )
        conn.execute(
            """
            INSERT INTO comment_bank
                (id, professional_id, source_no, status, author_member_id,
                 confirmed_at, updated_at)
            VALUES (2, 7, 11, 'AVAILABLE', 42, '2026-09-15T09:00:00+00:00', 'x')
            """
        )
        conn.commit()

        from app.comment_automation.service import schedule_next_comment_after_scout_expired
        scheduled = schedule_next_comment_after_scout_expired(
            conn,
            professional_id=7,
            now_utc="2026-09-15T10:00:00+00:00",
        )
        row = conn.execute(
            "SELECT scheduled_for FROM comment_bank WHERE id=2"
        ).fetchone()
        self.assertEqual(scheduled, "2026-09-15T10:00:00+00:00")
        self.assertEqual(row["scheduled_for"], scheduled)
        conn.close()


if __name__ == "__main__":
    unittest.main()
