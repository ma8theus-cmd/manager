import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.comment_automation import service
from app.comment_automation.feedback import normalize_feedback_kind
from app.comment_automation.importer import insert_records
from app.config import settings
from app.database import init_db


class NegativeCommentsTests(unittest.TestCase):
    def make_comment_schema(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                target_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_no INTEGER NOT NULL UNIQUE,
                professional_id INTEGER NOT NULL,
                age_band TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        return conn

    def test_normalizes_only_positive_or_negative_lane(self):
        self.assertEqual(normalize_feedback_kind("negative"), "NEGATIVE")
        self.assertEqual(normalize_feedback_kind("POSITIVE"), "POSITIVE")
        with self.assertRaises(ValueError):
            normalize_feedback_kind("mixed")

    def test_negative_import_is_not_mixed_with_positive_lane(self):
        conn = self.make_comment_schema()
        records = [{"professional": "Rita", "item_no": 1, "text": "Accueil professionnel mais prestation décevante et hygiène insuffisante."}]
        summary = insert_records(conn, records, feedback_kind="NEGATIVE", now="2026-09-24T00:00:00+00:00")
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(conn.execute("SELECT feedback_kind FROM comment_professionals").fetchone()[0], "NEGATIVE")
        self.assertEqual(conn.execute("SELECT feedback_kind FROM comment_bank").fetchone()[0], "NEGATIVE")
        conn.close()

    def test_professional_cannot_be_reused_across_lanes(self):
        conn = self.make_comment_schema()
        row = [{"professional": "Rita", "item_no": 1, "text": "Accueil professionnel, massage de qualité et hygiène irréprochable."}]
        insert_records(conn, row, feedback_kind="POSITIVE", now="2026-09-24T00:00:00+00:00")
        with self.assertRaises(ValueError):
            insert_records(conn, row, feedback_kind="NEGATIVE", now="2026-09-24T00:00:01+00:00")
        conn.close()

    def test_explicit_professional_name_is_saved_in_each_feedback_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "members.db"
            original_db_path = settings.database_path
            object.__setattr__(settings, "database_path", db_path)
            try:
                init_db()
                with patch.object(service, "_infer_professional_name", side_effect=AssertionError("não deve inferir o nome")):
                    positive = service.add_professional_from_url(
                        "https://www.meslibertines.com/escort/brenda",
                        feedback_kind="POSITIVE",
                        name="Brenda",
                    )
                    negative = service.add_professional_from_url(
                        "https://www.meslibertines.com/escort/taty",
                        feedback_kind="NEGATIVE",
                        name="Taty",
                    )
                self.assertEqual(positive["name"], "Brenda")
                self.assertEqual(negative["name"], "Taty")
            finally:
                object.__setattr__(settings, "database_path", original_db_path)

    def test_dashboard_add_forms_request_professional_name_and_url(self):
        template = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        self.assertGreaterEqual(template.count('name="professional_name"'), 2)
        self.assertGreaterEqual(template.count('name="target_url"'), 4)

    def test_negative_professional_is_created_in_negative_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "members.db"
            original_db_path = settings.database_path
            object.__setattr__(settings, "database_path", db_path)
            try:
                init_db()
                with patch.object(service, "_infer_professional_name", return_value="Rita"):
                    row = service.add_professional_from_url(
                        "https://www.meslibertines.com/escort/rita",
                        feedback_kind="NEGATIVE",
                    )
                self.assertEqual(row["feedback_kind"], "NEGATIVE")
                with service.get_connection() as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM comment_professionals WHERE feedback_kind='POSITIVE'").fetchone()[0],
                        0,
                    )
            finally:
                object.__setattr__(settings, "database_path", original_db_path)

    def test_feedback_rows_can_be_filtered_without_crossing_lanes(self):
        rows = [
            {"feedback_kind": "POSITIVE", "id": 1},
            {"feedback_kind": "NEGATIVE", "id": 2},
        ]
        self.assertEqual(service.filter_feedback_rows(rows, "NEGATIVE"), [rows[1]])

    def test_dashboard_exposes_a_separate_negative_comments_tab(self):
        template = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        self.assertIn('id="negative-comments"', template)
        self.assertIn("/negative-comments/professionals/add", template)
        self.assertIn("/negative-comments/{{ c.id }}/professional", template)
        self.assertIn("/negative-comments/bulk-professional", template)
        self.assertIn("negative-comment-select", template)
        self.assertIn("negative-comment-select-all", template)
        self.assertIn("Vincular selecionados", template)
        self.assertIn("Pool sem anúncio vinculado", template)
        self.assertIn("data.negative_comments", template)

    def test_negative_import_can_stage_without_professional(self):
        conn = self.make_comment_schema()
        records = [{"professional": "", "item_no": 1, "text": "Le massage était décevant et l'accueil manquait de professionnalisme."}]
        summary = insert_records(conn, records, feedback_kind="NEGATIVE", now="2026-09-24T00:00:00+00:00")
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(
            conn.execute("SELECT name FROM comment_professionals").fetchone()[0],
            "Feedbacks negativos sem anúncio",
        )
        conn.close()

    def test_negative_queue_waits_one_hour_between_submissions(self):
        retry_at = service.negative_comment_retry_at(
            now_utc="2026-09-24T00:59:00+00:00",
            previous_submissions=["2026-09-24T00:00:00+00:00"],
        )
        self.assertEqual(retry_at, "2026-09-24T01:00:00+00:00")
        self.assertIsNone(service.negative_comment_retry_at(
            now_utc="2026-09-24T01:00:00+00:00",
            previous_submissions=["2026-09-24T00:00:00+00:00"],
        ))

    def test_negative_comment_can_be_linked_to_an_ad_later(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "members.db"
            original_db_path = settings.database_path
            object.__setattr__(settings, "database_path", db_path)
            try:
                init_db()
                with service.get_connection() as conn:
                    summary = insert_records(
                        conn,
                        [{"professional": "", "item_no": 1, "text": "Le massage était décevant."}],
                        feedback_kind="NEGATIVE",
                        now="2026-09-24T00:00:00+00:00",
                    )
                    comment_id = conn.execute("SELECT id FROM comment_bank").fetchone()[0]
                with patch.object(service, "_infer_professional_name", return_value="Rita"):
                    professional = service.add_professional_from_url(
                        "https://www.meslibertines.com/escort/rita",
                        feedback_kind="NEGATIVE",
                    )
                service.assign_comment_professional(comment_id, professional["id"])
                with service.get_connection() as conn:
                    self.assertEqual(
                        conn.execute("SELECT professional_id FROM comment_bank WHERE id=?", (comment_id,)).fetchone()[0],
                        professional["id"],
                    )
            finally:
                object.__setattr__(settings, "database_path", original_db_path)

    def test_comment_package_exports_professional_assignment(self):
        from app.comment_automation import assign_comment_professional
        self.assertIs(assign_comment_professional, service.assign_comment_professional)

    def test_negative_discord_notice_uses_one_hour(self):
        source = (Path(__file__).parents[1] / "app" / "comment_automation" / "service.py").read_text()
        self.assertIn("Próximo envio negativo após 1 hora.", source)
        self.assertNotIn("Próximo envio negativo após 6 horas.", source)

    def test_discord_submission_notice_identifies_the_member(self):
        source = (Path(__file__).parents[1] / "app" / "comment_automation" / "service.py").read_text()
        self.assertIn("Membro:", source)
        self.assertIn('row.get("first_name")', source)
        self.assertIn('row.get("last_name")', source)
        self.assertIn('row.get("username")', source)

    def test_negative_submission_keeps_scout_association(self):
        source = (Path(__file__).parents[1] / "app" / "comment_automation" / "service.py").read_text()
        self.assertNotIn("if row.get('feedback_kind') == FEEDBACK_NEGATIVE:\n        return None", source)
        self.assertIn("status='SUBMITTED_MODERATION',submitted_at=?,scout_id=?", source)

    def test_multiple_negative_comments_can_be_linked_in_one_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "members.db"
            original_db_path = settings.database_path
            object.__setattr__(settings, "database_path", db_path)
            try:
                init_db()
                with service.get_connection() as conn:
                    insert_records(
                        conn,
                        [
                            {"professional": "", "item_no": 101, "text": "Le massage était décevant."},
                            {"professional": "", "item_no": 102, "text": "L'accueil manquait de professionnalisme."},
                        ],
                        feedback_kind="NEGATIVE",
                        now="2026-09-24T00:00:00+00:00",
                    )
                with patch.object(service, "_infer_professional_name", return_value="Rita"):
                    professional = service.add_professional_from_url(
                        "https://www.meslibertines.com/escort/rita-bulk",
                        feedback_kind="NEGATIVE",
                    )
                with service.get_connection() as conn:
                    ids = [row[0] for row in conn.execute("SELECT id FROM comment_bank ORDER BY id")]
                result = service.assign_comment_professionals(ids, professional["id"])
                self.assertEqual(result["linked"], 2)
                self.assertEqual(result["errors"], [])
                with service.get_connection() as conn:
                    linked = conn.execute(
                        "SELECT COUNT(*) FROM comment_bank WHERE professional_id=?",
                        (professional["id"],),
                    ).fetchone()[0]
                self.assertEqual(linked, 2)
            finally:
                object.__setattr__(settings, "database_path", original_db_path)


if __name__ == "__main__":
    unittest.main()
