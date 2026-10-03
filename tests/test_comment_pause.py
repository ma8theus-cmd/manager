import sqlite3
import unittest
from unittest.mock import patch

from app.comment_automation import service


class CommentPauseTests(unittest.TestCase):
    def test_due_queue_skips_disabled_professional(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                target_url TEXT,
                enabled INTEGER NOT NULL,
                feedback_kind TEXT NOT NULL
            );
            CREATE TABLE members (
                id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                birth_date TEXT,
                profile_status TEXT,
                comment_eligibility TEXT
            );
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY,
                professional_id INTEGER,
                author_member_id INTEGER,
                status TEXT,
                scheduled_for TEXT
            );
            INSERT INTO comment_professionals VALUES
                (1, 'Brenda', 'https://example.invalid/brenda', 0, 'POSITIVE'),
                (2, 'Other', 'https://example.invalid/other', 1, 'POSITIVE');
            INSERT INTO members VALUES
                (1, 'Member', 'One', 'member.one', '1990-01-01', 'COMPLETE', 'ELIGIBLE'),
                (2, 'Member', 'Two', 'member.two', '1990-01-01', 'COMPLETE', 'ELIGIBLE');
            INSERT INTO comment_bank VALUES
                (1, 1, 1, 'AVAILABLE', '2000-01-01T00:00:00+00:00'),
                (2, 2, 2, 'AVAILABLE', '2000-01-01T00:00:00+00:00');
            """
        )
        try:
            with patch.object(service, "get_connection", return_value=conn):
                due = service._due(limit=3)
            self.assertEqual([row["professional_name"] for row in due], ["Other"])
        finally:
            conn.close()
