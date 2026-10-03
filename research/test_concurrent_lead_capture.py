import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/home/matheus/meslibertines_manager_v1")

from research import comment_research_collector as collector


class ConcurrentLeadCaptureTests(unittest.TestCase):
    def test_target_override_is_clamped_and_preserves_minimum(self):
        self.assertTrue(hasattr(collector, "effective_target"))
        self.assertEqual(collector.effective_target(257), 257)
        self.assertEqual(collector.effective_target(5), collector.TARGET)
        self.assertEqual(collector.effective_target(5001), 1000)

    def test_cli_help_exposes_role_and_target_controls(self):
        result = subprocess.run(
            [
                sys.executable,
                str(Path(collector.__file__)),
                "--help",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--target-total", result.stdout)
        self.assertIn("--node-role", result.stdout)

    def test_control_role_does_not_block_scouts_or_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "members.db"
            conn = collector.sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE comment_scouts (status TEXT);
                CREATE TABLE comment_bank (status TEXT, submitted_at TEXT);
                CREATE TABLE members (
                    registration_status TEXT,
                    profile_status TEXT,
                    account_created_at TEXT
                );
                INSERT INTO comment_scouts VALUES ('ACTIVE');
                INSERT INTO comment_bank VALUES ('POSTING', NULL);
                """
            )
            conn.commit()
            conn.close()

            original_db = collector.MAIN_DB
            original_role = collector.NODE_ROLE
            try:
                collector.MAIN_DB = db_path
                collector.NODE_ROLE = "all"
                self.assertTrue(collector.main_site_busy()[0])

                collector.NODE_ROLE = "control"
                self.assertFalse(collector.main_site_busy()[0])
            finally:
                collector.MAIN_DB = original_db
                collector.NODE_ROLE = original_role


if __name__ == "__main__":
    unittest.main()
