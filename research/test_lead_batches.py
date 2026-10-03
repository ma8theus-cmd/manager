import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/home/matheus/meslibertines_manager_v1")

from app.lead_batches import _pending, batch_content, batch_dashboard_data


class LeadBatchFilterTests(unittest.TestCase):
    def test_pending_excludes_ineligible_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "comments.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE candidates (
                    profile_id TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    date_text TEXT,
                    comment TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE panel_archived_comments (
                    comment_hash TEXT PRIMARY KEY,
                    archived_at TEXT NOT NULL
                );
                CREATE TABLE lead_batch_items (
                    comment_hash TEXT PRIMARY KEY,
                    batch_id INTEGER NOT NULL
                );
            """)
            conn.executemany(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("P1", 9.0, "today", "Accueil chaleureux, hygiène irréprochable, massage de qualité et personne très professionnelle.", "https://example/1", "2026-01-01"),
                    ("P2", 9.0, "today", "Hygiène irréprochable, mais parfaite au lit.", "https://example/2", "2026-01-02"),
                ],
            )
            conn.commit()

            pending = _pending(conn)

            self.assertEqual([row["profile_id"] for row in pending], ["P1"])
            conn.close()

    def test_dashboard_hides_batch_with_ineligible_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "comments.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE candidates (
                    profile_id TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    date_text TEXT,
                    comment TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE panel_archived_comments (
                    comment_hash TEXT PRIMARY KEY,
                    archived_at TEXT NOT NULL
                );
                CREATE TABLE lead_batch_items (
                    comment_hash TEXT PRIMARY KEY,
                    batch_id INTEGER NOT NULL
                );
                CREATE TABLE lead_txt_batches (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    item_count INTEGER NOT NULL
                );
            """)
            valid = "Accueil chaleureux, hygiène irréprochable, massage de qualité et personne très professionnelle."
            invalid = "Hygiène irréprochable, mais parfaite au lit."
            conn.executemany(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("P1", 9.0, "today", valid, "https://example/1", "2026-01-01"),
                    ("P2", 9.0, "today", invalid, "https://example/2", "2026-01-02"),
                ],
            )
            import hashlib
            conn.executemany(
                "INSERT INTO lead_batch_items VALUES (?, 1)",
                [(hashlib.sha256(value.encode()).hexdigest(),) for value in (valid, invalid)],
            )
            conn.execute("INSERT INTO lead_txt_batches VALUES (1, 'today', 'invalid batch', 50)")
            conn.commit()
            conn.close()

            self.assertEqual(batch_dashboard_data(db_path)["batches"], [])
            self.assertIsNone(batch_content(db_path, 1))


if __name__ == "__main__":
    unittest.main()
