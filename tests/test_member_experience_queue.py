import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import member_experience
from app.member_experience import (
    eligible_members_for_professional,
    init_schema,
    prepare_batch,
    recover_interrupted_items,
    record_click,
    record_login,
    record_logout,
    start_batch,
)


class MemberExperienceQueueTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE comment_professionals (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            target_url TEXT,
            profile_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            feedback_kind TEXT NOT NULL DEFAULT 'POSITIVE'
        );
        CREATE TABLE members (
            id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            email TEXT,
            profile_status TEXT NOT NULL DEFAULT 'COMPLETE',
            birth_date TEXT NOT NULL DEFAULT '1980-01-01',
            top50_eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE'
        );
        CREATE TABLE comment_bank (
            id INTEGER PRIMARY KEY,
            professional_id INTEGER NOT NULL,
            author_member_id INTEGER,
            status TEXT NOT NULL,
            scout_id INTEGER,
            confirmed_at TEXT,
            submitted_at TEXT
        );
        CREATE TABLE comment_member_usage (
            comment_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE comment_scouts (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        );
        """)
        self.conn.execute(
            "INSERT INTO comment_professionals(id, name, target_url, profile_url) "
            "VALUES (1, 'Paola', 'https://example.test/paola', 'https://example.test/paola')"
        )
        self.conn.executemany(
            "INSERT INTO members(id, first_name, last_name, username, email, profile_status, birth_date) VALUES (?, ?, ?, ?, ?, 'COMPLETE', '1980-01-01')",
            [(41, "Ana", "A", "ana", "ana@example.test"),
             (42, "Bia", "B", "bia", "bia@example.test"),
             (43, "Cris", "C", "cris", "cris@example.test")],
        )
        self.conn.executemany(
            "INSERT INTO comment_bank VALUES (?, 1, ?, 'USED', NULL, '2026-09-18T00:00:00+00:00', '2026-09-18T00:00:00+00:00')",
            [(101, 41), (102, 42)],
        )
        self.conn.executemany(
            "INSERT INTO comment_member_usage VALUES (?, ?, 'CONFIRMED')",
            [(101, 41), (102, 42)],
        )
        init_schema(self.conn)

    def test_all_registered_members_are_eligible_without_professional_comment(self):
        rows = eligible_members_for_professional(self.conn, 1)

        self.assertEqual(
            [row["id"] for row in rows],
            [41, 42, 43],
        )

    def test_top50_excludes_negative_feedback_professionals(self):
        self.conn.execute(
            "INSERT INTO comment_professionals(id, name, target_url, profile_url, feedback_kind) "
            "VALUES (2, 'Tati', 'https://example.test/tati', 'https://example.test/tati', 'NEGATIVE')"
        )

        with patch.object(member_experience, "get_connection", return_value=self.conn):
            data = member_experience.batch_dashboard_data()

        self.assertEqual([item["name"] for item in data["professionals"]], ["Paola"])

    def test_top50_ineligible_members_are_excluded_from_every_future_batch(self):
        self.conn.execute(
            "UPDATE members SET top50_eligibility='INELIGIBLE' WHERE id IN (41, 42)"
        )
        self.assertEqual(
            [row["id"] for row in eligible_members_for_professional(self.conn, 1)],
            [43],
        )

    def test_completed_or_clicked_members_are_excluded_per_professional(self):
        self.conn.execute(
            "INSERT INTO comment_professionals(id, name, target_url, profile_url) "
            "VALUES (2, 'Carla', 'https://example.test/carla', 'https://example.test/carla')"
        )
        batch_id = prepare_batch(self.conn, 1, [41, 42])
        self.conn.execute(
            "UPDATE member_experience_queue SET status='COMPLETED' "
            "WHERE batch_id=? AND member_id=41",
            (batch_id,),
        )
        self.conn.execute(
            "UPDATE member_experience_queue SET status='CLICKED' "
            "WHERE batch_id=? AND member_id=42",
            (batch_id,),
        )

        self.assertEqual(
            [row["id"] for row in eligible_members_for_professional(self.conn, 1)],
            [43],
        )
        self.assertEqual(
            [row["id"] for row in eligible_members_for_professional(self.conn, 2)],
            [41, 42, 43],
        )

    def test_failed_member_remains_eligible_for_a_future_attempt(self):
        batch_id = prepare_batch(self.conn, 1, [41])
        self.conn.execute(
            "UPDATE member_experience_queue SET status='FAILED' "
            "WHERE batch_id=? AND member_id=41",
            (batch_id,),
        )

        self.assertEqual(
            [row["id"] for row in eligible_members_for_professional(self.conn, 1)],
            [41, 42, 43],
        )

    def test_ajouter_form_exposes_select_all_control(self):
        template = Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"
        content = template.read_text(encoding="utf-8")

        self.assertIn("experience-select-all", content)
        self.assertIn("Selecionar todos", content)

    def test_selection_only_creates_draft_queue(self):
        batch_id = prepare_batch(self.conn, 1, [41, 42])
        batch = self.conn.execute(
            "SELECT status FROM member_experience_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        items = self.conn.execute(
            "SELECT member_id, status, login_at, click_at FROM member_experience_queue "
            "WHERE batch_id = ? ORDER BY position",
            (batch_id,),
        ).fetchall()
        self.assertEqual(batch["status"], "DRAFT")
        self.assertEqual([(row["member_id"], row["status"]) for row in items],
                         [(41, "QUEUED"), (42, "QUEUED")])
        self.assertIsNone(items[0]["login_at"])
        self.assertIsNone(items[0]["click_at"])

    def test_recovery_requeues_orphaned_running_items(self):
        batch_id = prepare_batch(self.conn, 1, [41, 42])
        start_batch(self.conn, batch_id)
        self.conn.execute(
            "UPDATE member_experience_queue SET status='RUNNING' "
            "WHERE batch_id=? AND position=1",
            (batch_id,),
        )

        recovered = recover_interrupted_items(self.conn, batch_id)

        self.assertEqual(recovered, 1)
        item = self.conn.execute(
            "SELECT status, last_error FROM member_experience_queue "
            "WHERE batch_id=? AND position=1",
            (batch_id,),
        ).fetchone()
        self.assertEqual(item["status"], "QUEUED")
        self.assertIsNone(item["last_error"])
        event = self.conn.execute(
            "SELECT event FROM member_experience_audit "
            "WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        self.assertEqual(event["event"], "ITEMS_RECOVERED")

    def test_start_is_explicit_and_does_not_click_anything(self):
        batch_id = prepare_batch(self.conn, 1, [41])
        start_batch(self.conn, batch_id)
        batch = self.conn.execute(
            "SELECT status FROM member_experience_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        item = self.conn.execute(
            "SELECT status, login_at, click_at FROM member_experience_queue "
            "WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        self.assertEqual(batch["status"], "RUNNING")
        self.assertEqual(item["status"], "QUEUED")
        self.assertIsNone(item["login_at"])
        self.assertIsNone(item["click_at"])

    def test_audit_lifecycle_completes_batch_after_logout(self):
        batch_id = prepare_batch(self.conn, 1, [41])
        start_batch(self.conn, batch_id)
        queue_id = self.conn.execute(
            "SELECT id FROM member_experience_queue WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()["id"]
        self.conn.execute(
            "UPDATE member_experience_queue SET status='RUNNING' WHERE id = ?",
            (queue_id,),
        )
        record_login(
            queue_id,
            now="2026-09-18T10:00:00+00:00",
            conn=self.conn,
        )
        record_click(
            queue_id,
            now="2026-09-18T10:01:00+00:00",
            conn=self.conn,
        )
        completed = record_logout(
            queue_id,
            now="2026-09-18T10:02:00+00:00",
            conn=self.conn,
        )
        self.assertTrue(completed)
        batch = self.conn.execute(
            "SELECT status FROM member_experience_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        item = self.conn.execute(
            "SELECT status, login_at, click_at, logout_at "
            "FROM member_experience_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        self.assertEqual(batch["status"], "COMPLETED")
        self.assertEqual(item["status"], "COMPLETED")
        self.assertEqual(item["click_at"], "2026-09-18T10:01:00+00:00")
        self.assertEqual(item["logout_at"], "2026-09-18T10:02:00+00:00")

    def test_last_click_reports_when_every_selected_member_clicked(self):
        batch_id = prepare_batch(self.conn, 1, [41, 42])
        start_batch(self.conn, batch_id)
        rows = self.conn.execute(
            "SELECT id, member_id FROM member_experience_queue "
            "WHERE batch_id = ? ORDER BY position",
            (batch_id,),
        ).fetchall()
        self.conn.execute(
            "UPDATE member_experience_queue SET status='RUNNING' WHERE batch_id = ?",
            (batch_id,),
        )

        for index, row in enumerate(rows):
            record_login(
                row["id"],
                now=f"2026-09-18T10:0{index}:00+00:00",
                conn=self.conn,
            )

        self.assertFalse(
            record_click(
                rows[0]["id"],
                now="2026-09-18T10:02:00+00:00",
                conn=self.conn,
            )
        )
        self.assertTrue(
            record_click(
                rows[1]["id"],
                now="2026-09-18T10:03:00+00:00",
                conn=self.conn,
            )
        )
        batch = self.conn.execute(
            "SELECT status FROM member_experience_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        self.assertEqual(batch["status"], "RUNNING")
        event = self.conn.execute(
            "SELECT event FROM member_experience_audit "
            "WHERE batch_id = ? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        self.assertEqual(event["event"], "BATCH_ALL_CLICKED")


    def test_launch_starts_executor_as_importable_project_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            logs_dir = project_root / "logs"
            logs_dir.mkdir()
            fake_settings = SimpleNamespace(
                project_root=project_root,
                logs_dir=logs_dir,
            )
            with patch.object(member_experience, "settings", fake_settings), \
                 patch.object(member_experience.subprocess, "Popen") as popen:
                popen.return_value.pid = 4321
                pid = member_experience.launch_batch(7)

        self.assertEqual(pid, 4321)
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                member_experience.sys.executable,
                "-m",
                "scripts.run_member_experience",
                "--batch-id",
                "7",
            ],
        )
        self.assertEqual(popen.call_args.kwargs["cwd"], str(project_root))


if __name__ == "__main__":
    unittest.main()
