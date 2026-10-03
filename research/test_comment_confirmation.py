import sqlite3
import unittest
from datetime import datetime, timezone

import app.comment_automation.service as comment_service
from app.comment_automation.service import confirm_comment


class CommentConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY,
                author_member_id INTEGER,
                status TEXT NOT NULL,
                scheduled_for TEXT,
                confirmation_requested_at TEXT,
                confirmed_at TEXT,
                confirmed_by_member_id INTEGER,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            INSERT INTO comment_bank
                (id, author_member_id, status, updated_at)
                VALUES (1, 42, 'AWAITING_CONFIRMATION', '2026-09-15T00:00:00+00:00')
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_confirmation_by_author_queues_comment(self):
        result = confirm_comment(self.conn, 1, 42)
        row = self.conn.execute("SELECT * FROM comment_bank WHERE id=1").fetchone()
        self.assertTrue(result)
        self.assertEqual(row["status"], "AVAILABLE")
        self.assertIsNotNone(row["scheduled_for"])
        self.assertEqual(row["confirmed_by_member_id"], 42)
        self.assertIsNotNone(row["confirmed_at"])

    def test_confirmation_by_different_member_is_rejected(self):
        with self.assertRaises(ValueError):
            confirm_comment(self.conn, 1, 99)
        row = self.conn.execute("SELECT * FROM comment_bank WHERE id=1").fetchone()
        self.assertEqual(row["status"], "AWAITING_CONFIRMATION")
        self.assertIsNone(row["scheduled_for"])

    def test_delegated_assignment_queues_without_confirmation(self):
        assignment_fields = getattr(comment_service, "comment_assignment_fields", None)
        self.assertIsNotNone(assignment_fields)
        fields = assignment_fields(
            member_id=42,
            now="2026-09-15T16:00:00+00:00",
            require_confirmation=False,
        )
        self.assertEqual(fields["status"], "AVAILABLE")
        self.assertEqual(fields["scheduled_for"], "2026-09-15T16:00:00+00:00")
        self.assertEqual(fields["confirmed_at"], "2026-09-15T16:00:00+00:00")
        self.assertEqual(fields["confirmed_by_member_id"], 42)

    def test_professional_interval_blocks_second_comment_within_eight_hours(self):
        block = getattr(comment_service, "comment_publish_block", None)
        self.assertIsNotNone(block)
        result = block(
            professional_id=7,
            now_utc=datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc),
            previous_comments=[
                {"professional_id": 7, "submitted_at": "2026-09-15T10:00:00+00:00"}
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "professional_interval")
        self.assertEqual(result[1], "2026-09-15T18:00:00+00:00")

    def test_professional_interval_allows_comment_after_eight_hours(self):
        block = getattr(comment_service, "comment_publish_block", None)
        self.assertIsNotNone(block)
        result = block(
            professional_id=7,
            now_utc=datetime(2026, 9, 15, 18, 1, tzinfo=timezone.utc),
            previous_comments=[
                {"professional_id": 7, "submitted_at": "2026-09-15T10:00:00+00:00"}
            ],
        )
        self.assertIsNone(result)

    def test_minimum_interval_blocks_comments_published_too_close(self):
        block = getattr(comment_service, "comment_publish_block", None)
        self.assertIsNotNone(block)
        result = block(
            professional_id=8,
            now_utc=datetime(2026, 9, 15, 16, 2, tzinfo=timezone.utc),
            previous_comments=[
                {"professional_id": 9, "submitted_at": "2026-09-15T16:00:00+00:00"}
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "minimum_interval")
        self.assertEqual(result[1], "2026-09-15T16:04:00+00:00")


if __name__ == "__main__":
    unittest.main()
