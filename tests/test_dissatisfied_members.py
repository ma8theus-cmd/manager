import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.comment_automation.service import assign_comment_author
from app.config import settings
from app.database import get_connection, init_db
from app.services import (
    comment_member_lists,
    mark_member_dissatisfied,
    unmark_member_dissatisfied,
)


class DissatisfiedMemberTests(unittest.TestCase):
    @contextmanager
    def temporary_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "members.db"
            original = settings.database_path
            object.__setattr__(settings, "database_path", db_path)
            try:
                init_db()
                yield
            finally:
                object.__setattr__(settings, "database_path", original)

    def add_member(self, member_id=1):
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO members
                   (id,first_name,last_name,birth_date,city_france,
                    registration_status,email_status,profile_status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (member_id, "Membro", str(member_id), "1980-01-01", "Paris",
                 "PROFILE_COMPLETE", "CONFIRMED", "COMPLETE", "now", "now"),
            )
    def add_comment(self, member_id=None, feedback_kind="POSITIVE"):
        with get_connection() as conn:
            professional_id = conn.execute(
                """INSERT INTO comment_professionals
                   (name,target_url,enabled,feedback_kind,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (f"Profissional {feedback_kind}", "https://example.test/ad",
                 1, feedback_kind, "now", "now"),
            ).lastrowid
            conn.execute(
                """INSERT INTO comment_bank
                   (source_no,professional_id,age_band,comment_text,feedback_kind,
                    author_member_id,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (900 + professional_id, professional_id, "test",
                 f"Feedback {feedback_kind}", feedback_kind, member_id,
                 "USED" if member_id else "AVAILABLE", "now", "now"),
            )

    def test_marking_member_separates_them_from_eligible_members(self):
        with self.temporary_database():
            self.add_member(1)
            mark_member_dissatisfied(1, "Relatou experiência ruim.")
            lists = comment_member_lists()
            self.assertEqual([m["id"] for m in lists["dissatisfied"]], [1])
            self.assertEqual(lists["eligible"], [])
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT comment_eligibility,dissatisfied_reason FROM members WHERE id=1"
                ).fetchone()
            self.assertEqual(row["comment_eligibility"], "DISSATISFIED")
            self.assertEqual(row["dissatisfied_reason"], "Relatou experiência ruim.")

    def test_unmarking_member_returns_them_to_eligible_list(self):
        with self.temporary_database():
            self.add_member(1)
            mark_member_dissatisfied(1)
            unmark_member_dissatisfied(1)
            lists = comment_member_lists()
            self.assertEqual([m["id"] for m in lists["eligible"]], [1])
            self.assertEqual(lists["dissatisfied"], [])
    def test_member_with_positive_comment_cannot_be_marked_dissatisfied(self):
        with self.temporary_database():
            self.add_member(1)
            self.add_comment(member_id=1, feedback_kind="POSITIVE")
            with self.assertRaises(ValueError):
                mark_member_dissatisfied(1)
            with get_connection() as conn:
                status = conn.execute(
                    "SELECT comment_eligibility FROM members WHERE id=1"
                ).fetchone()[0]
            self.assertEqual(status, "ELIGIBLE")

    def test_dissatisfied_member_cannot_be_assigned_to_any_comment(self):
        with self.temporary_database():
            self.add_member(1)
            self.add_comment(member_id=None, feedback_kind="NEGATIVE")
            mark_member_dissatisfied(1)
            with self.assertRaises(ValueError):
                assign_comment_author(1, 1)

    def test_panel_has_a_separate_dissatisfied_members_section(self):
        template = (
            Path(__file__).parents[1]
            / "app"
            / "templates"
            / "dashboard.html"
        ).read_text()
        self.assertIn('id="dissatisfied-members"', template)
        self.assertIn('/members/dissatisfied/mark', template)
        self.assertIn("data.dissatisfied_members", template)


if __name__ == "__main__":
    unittest.main()
