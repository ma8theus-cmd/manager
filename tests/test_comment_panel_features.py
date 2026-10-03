import hashlib
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.comment_automation import service


class CommentPanelFeatureTests(unittest.TestCase):
    @staticmethod
    def importer():
        try:
            return importlib.import_module("app.comment_automation.importer")
        except ModuleNotFoundError:
            return None

    def test_parser_reads_sections_and_numbered_comments(self):
        importer = self.importer()
        self.assertIsNotNone(importer, "Implementar o módulo de importação.")
        if importer is None:
            return
        body = """========================================================================
PAOLA — 2 COMMENTAIRES
========================================================================

1. Accueil très professionnel et massage de qualité.

2. Très ponctuelle, gentille et attentive.

========================================================================
AGNES — 1 COMMENTAIRE
========================================================================

1. Cadre propre et moment de détente agréable.
"""
        rows = importer.parse_comment_txt(body)
        self.assertEqual(
            [(r["professional"], r["item_no"]) for r in rows],
            [("Paola", 1), ("Paola", 2), ("Agnes", 1)],
        )
        self.assertIn("massage", rows[0]["text"].lower())

    def test_filter_accepts_service_feedback_and_rejects_explicit_content(self):
        importer = self.importer()
        self.assertIsNotNone(importer, "Implementar o módulo de importação.")
        if importer is None:
            return
        self.assertTrue(importer.is_comment_eligible(
            "Accueil très professionnel, massage de qualité et hygiène irréprochable."
        ))
        self.assertFalse(importer.is_comment_eligible(
            "Accueil chaleureux, préliminaires et moment sexuel exceptionnel."
        ))
        self.assertFalse(importer.is_comment_eligible(
            "Accueil professionnel, massage de qualité et moment sensuel."
        ))

    def test_import_records_inserts_only_eligible_comments(self):
        importer = self.importer()
        self.assertIsNotNone(importer, "Implementar o módulo de importação.")
        if importer is None:
            return
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                target_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_no INTEGER NOT NULL UNIQUE,
                professional_id INTEGER NOT NULL,
                age_band TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO comment_professionals(name,created_at,updated_at) VALUES ('Paola','x','x')"
        )
        conn.execute(
            "INSERT INTO comment_bank(source_no,professional_id,age_band,comment_text,status,created_at,updated_at) "
            "VALUES (132,1,'existing','Existing feedback','AVAILABLE','x','x')"
        )
        rows = [
            {"professional": "Paola", "item_no": 1,
             "text": "Accueil professionnel et massage de qualité."},
            {"professional": "Paola", "item_no": 2,
             "text": "Massage et préliminaires exceptionnels."},
        ]
        summary = importer.insert_records(conn, rows, now="2026-09-17T00:00:00+00:00")
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(conn.execute("SELECT MAX(source_no) FROM comment_bank").fetchone()[0], 133)
        conn.close()

    def test_import_records_deduplicates_text_across_professionals(self):
        importer = self.importer()
        self.assertIsNotNone(importer, "Implementar o módulo de importação.")
        if importer is None:
            return

        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                target_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_no INTEGER NOT NULL UNIQUE,
                professional_id INTEGER NOT NULL,
                age_band TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.executemany(
            "INSERT INTO comment_professionals(name,created_at,updated_at) VALUES (?, ?, ?)",
            [("Paola", "x", "x"), ("Amanda", "x", "x")],
        )
        text = "Accueil professionnel et massage de qualité."
        rows = [
            {"professional": "Paola", "item_no": 1, "text": text},
            {"professional": "Amanda", "item_no": 1, "text": text},
        ]

        summary = importer.insert_records(conn, rows, now="2026-09-21T00:00:00+00:00")

        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comment_bank").fetchone()[0], 1)
        conn.close()

    def test_rows_are_grouped_under_each_professional(self):
        grouper = getattr(service, "group_comment_rows", None)
        self.assertTrue(callable(grouper), "Criar o agrupador de comentários por profissional.")
        if not callable(grouper):
            return
        professionals = [{"id": 1, "name": "Amanda"}, {"id": 2, "name": "Paola"}]
        rows = [
            {"professional_id": 2, "source_no": 3},
            {"professional_id": 1, "source_no": 1},
        ]
        grouped = grouper(professionals, rows)
        self.assertEqual([r["source_no"] for r in grouped[0]["rows"]], [1])
        self.assertEqual([r["source_no"] for r in grouped[1]["rows"]], [3])

    def test_import_cli_help_works_from_project_root(self):
        import subprocess
        import sys

        project = Path(__file__).parents[1]
        result = subprocess.run(
            [sys.executable, str(project / "scripts" / "import_comment_txt.py"), "--help"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout.lower())

    def test_comment_tabs_are_closed_by_default(self):
        template = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        self.assertIn("comment-professional-group", template)
        self.assertNotIn('id="members" open', template)

    def test_lead_batch_dashboard_reuses_eligible_hashes(self):
        from app import lead_batches

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE candidates (
                    profile_id TEXT,
                    score REAL,
                    date_text TEXT,
                    comment TEXT,
                    source_url TEXT,
                    updated_at TEXT
                );
                CREATE TABLE lead_txt_batches (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    item_count INTEGER
                );
                CREATE TABLE lead_batch_items (
                    comment_hash TEXT PRIMARY KEY,
                    batch_id INTEGER NOT NULL
                );
                """
            )
            rows = [
                (f"profile-{index}", 8.0, "2026-09-20", f"Feedback {index}", "https://example.test", "2026-09-20")
                for index in range(100)
            ]
            conn.executemany("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.executemany(
                "INSERT INTO lead_txt_batches VALUES (?, ?, ?)",
                [(1, "2026-09-20", 50), (2, "2026-09-20", 50)],
            )
            items = [
                (hashlib.sha256(row[3].encode("utf-8")).hexdigest(), 1 if index < 50 else 2)
                for index, row in enumerate(rows)
            ]
            conn.executemany("INSERT INTO lead_batch_items VALUES (?, ?)", items)
            conn.commit()
            conn.close()

            with patch.object(lead_batches, "is_acceptable_comment", return_value=True), \
                 patch.object(lead_batches, "score_comment", return_value=8.0), \
                 patch.object(
                     lead_batches,
                     "_eligible_hashes",
                     wraps=lead_batches._eligible_hashes,
                 ) as eligible_hashes:
                lead_batches.batch_dashboard_data(db_path)

            self.assertEqual(eligible_hashes.call_count, 1)

    def test_manual_comment_is_saved_with_member_and_professional_without_auto_send(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE comment_professionals (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, feedback_kind TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE members (
                id INTEGER PRIMARY KEY, profile_status TEXT NOT NULL,
                comment_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE'
            );
            CREATE TABLE comment_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_no INTEGER NOT NULL UNIQUE,
                professional_id INTEGER NOT NULL, age_band TEXT NOT NULL,
                comment_text TEXT NOT NULL, feedback_kind TEXT NOT NULL,
                author_member_id INTEGER, status TEXT NOT NULL, scheduled_for TEXT,
                confirmed_at TEXT, confirmed_by_member_id INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE comment_member_usage (
                member_id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL,
                used_at TEXT NOT NULL, outcome TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO comment_professionals VALUES (1,'Brenda','https://example.test/brenda',1,'POSITIVE','x','x')")
        conn.execute("INSERT INTO members VALUES (7,'COMPLETE','ELIGIBLE')")

        result = service.create_manual_comment(
            conn, professional_id=1, member_id=7,
            comment_text="Accueil professionnel et massage de qualité.",
            feedback_kind="POSITIVE", now="2026-09-27T15:00:00+00:00"
        )

        row = conn.execute("SELECT * FROM comment_bank WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(row["professional_id"], 1)
        self.assertEqual(row["author_member_id"], 7)
        self.assertEqual(row["status"], "AVAILABLE")
        self.assertIsNone(row["scheduled_for"])
        self.assertEqual(row["confirmed_by_member_id"], 7)
        conn.close()


if __name__ == "__main__":
    unittest.main()
