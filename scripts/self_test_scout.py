from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from jinja2 import Environment, FileSystemLoader

from app.config import settings
from app.database import get_connection, init_db, utc_now
from app.scout.service import (
    ACTIVE,
    EXPIRED,
    FOUND,
    RESULT_FOUND,
    RESULT_NOT_FOUND,
    _first_check_at,
    _next_check_at,
    _store_check_result,
    comment_matches,
    create_comment_scout,
    normalize_text,
    scout_dashboard_data,
)
from app.services import dashboard_data


def _scout_row(scout_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT s.*, m.first_name, m.last_name, m.username, m.birth_date
            FROM comment_scouts s
            JOIN members m ON m.id=s.member_id
            WHERE s.id=?
            """,
            (scout_id,),
        ).fetchone()
        return dict(row)


def main() -> None:
    assert normalize_text("  Bonjour\n  le monde ") == "bonjour le monde"
    assert comment_matches(
        "Auteur: Jean\nExcellent massage, merci beaucoup !",
        "Excellent massage, merci beaucoup !",
    )
    assert not comment_matches("Autre commentaire", "Excellent massage")

    published = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert _first_check_at(published) == published + timedelta(hours=6)
    assert _next_check_at(published, 1, published + timedelta(hours=6, minutes=30)) == published + timedelta(hours=8)
    assert _next_check_at(published, 1, published + timedelta(hours=10)) == published + timedelta(hours=8)

    original_db = settings.database_path
    try:
        with TemporaryDirectory(prefix="meslibertines_scout_test_") as tmp:
            object.__setattr__(settings, "database_path", Path(tmp) / "members_test.db")
            init_db()

            now = utc_now()
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO members(
                        id, first_name, last_name, birth_date, city_france,
                        username, email, created_at, updated_at
                    ) VALUES (1, 'Jean', 'Test', '1990-01-01', 'Paris',
                              'jean.test', 'jean@example.invalid', ?, ?)
                    """,
                    (now, now),
                )

            scout_id = create_comment_scout(
                1,
                "Annonce de test",
                "https://example.invalid/annonce",
                "Commentaire unique de test",
                published_at=published.isoformat(timespec="seconds"),
            )
            row = _scout_row(scout_id)
            assert row["status"] == ACTIVE
            assert row["checks_done"] == 0
            assert row["max_checks"] == 2

            out = _store_check_result(row, RESULT_NOT_FOUND, "Ainda não encontrado")
            assert out["status"] == ACTIVE
            row = _scout_row(scout_id)
            assert row["checks_done"] == 1

            out = _store_check_result(row, RESULT_FOUND, "Encontrado")
            assert out["status"] == FOUND
            row = _scout_row(scout_id)
            assert row["status"] == FOUND
            assert row["found_at"]
            assert row["next_check_at"] is None

            # Separate Scout validates the 2-check terminal expiration/recycle rule.
            scout2 = create_comment_scout(
                1,
                "Outra anúncio de teste",
                "https://example.invalid/outra",
                "Outro comentário único",
                published_at=published.isoformat(timespec="seconds"),
            )
            with get_connection() as conn:
                now2 = utc_now()
                conn.execute("INSERT INTO comment_professionals(name,created_at,updated_at) VALUES ('Amanda',?,?)", (now2, now2))
                professional_id = conn.execute("SELECT id FROM comment_professionals WHERE name='Amanda'").fetchone()[0]
                cur = conn.execute(
                    """INSERT INTO comment_bank(source_no,professional_id,age_band,comment_text,author_member_id,status,submitted_at,scout_id,created_at,updated_at)
                       VALUES (1,?,?,?,?, 'AWAITING_CONFIRMATION',?,?,?,?)""",
                    (professional_id, 'test', 'Outro comentário único', 1, now2, scout2, now2, now2),
                )
                comment_id = cur.lastrowid
                conn.execute("INSERT INTO comment_member_usage(member_id,comment_id,used_at,outcome) VALUES (1,?,?,'SUBMITTED')", (comment_id, now2))

            for attempt in range(1, 3):
                row2 = _scout_row(scout2)
                out2 = _store_check_result(row2, RESULT_NOT_FOUND, "Ainda não encontrado")
                if attempt < 2:
                    assert out2["status"] == ACTIVE
                else:
                    assert out2["status"] == EXPIRED
                    assert out2["recycled"] is True
            row2 = _scout_row(scout2)
            assert row2["checks_done"] == 2
            assert row2["status"] == EXPIRED
            with get_connection() as conn:
                recycled = conn.execute("SELECT status,author_member_id,scout_id FROM comment_bank WHERE id=?", (comment_id,)).fetchone()
                assert recycled["status"] == 'AVAILABLE'
                assert recycled["author_member_id"] is None
                assert recycled["scout_id"] is None
                assert conn.execute("SELECT COUNT(*) FROM comment_member_usage WHERE member_id=1").fetchone()[0] == 1

            data = dashboard_data()
            data["scout"] = scout_dashboard_data()
            data["promising"] = {"members": [], "error": None}
            data["lead_exports"] = {"batches": [], "pending": 0}
            env = Environment(loader=FileSystemLoader(str(PROJECT_ROOT / "app" / "templates")))
            html = env.get_template("dashboard.html").render(
                data=data,
                request=None,
                msg=None,
                error=None,
            )
            assert "Scout de comentários" in html
            assert "Comentários encontrados" in html

    finally:
        object.__setattr__(settings, "database_path", original_db)

    print("OK - Scout offline: banco, 2 checagens (+6h/+8h), FOUND/EXPIRED, matching e painel validados.")


if __name__ == "__main__":
    main()
