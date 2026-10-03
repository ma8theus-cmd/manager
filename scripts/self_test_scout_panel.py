from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from jinja2 import Environment, FileSystemLoader

from app.config import settings
from app.database import get_connection, init_db, utc_now
from app.scout import record_comment_published, scout_dashboard_data, scout_member_options
from app.services import dashboard_data


def main() -> None:
    original_db = settings.database_path
    try:
        with TemporaryDirectory(prefix="meslibertines_scout_panel_test_") as tmp:
            object.__setattr__(settings, "database_path", Path(tmp) / "members_test.db")
            init_db()
            now = utc_now()
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO members(
                        id, first_name, last_name, birth_date, city_france,
                        username, email, registration_status, email_status,
                        created_at, updated_at
                    ) VALUES (
                        1, 'Jean', 'Panel', '1990-01-01', 'Paris',
                        'jean.panel', 'jean@example.invalid', 'EMAIL_CONFIRMED',
                        'CONFIRMED', ?, ?
                    )
                    """,
                    (now, now),
                )

            options = scout_member_options()
            assert len(options) == 1
            assert options[0]["username"] == "jean.panel"

            scout_id = record_comment_published(
                1,
                "Annonce panneau",
                "https://m.meslibertines.com/annonce-test",
                "Commentaire exato do painel",
            )
            assert scout_id > 0

            data = dashboard_data()
            data["scout"] = scout_dashboard_data()
            data["scout_members"] = scout_member_options()
            data["promising"] = {"members": [], "error": None}
            data["lead_exports"] = {"batches": [], "pending": 0}

            env = Environment(loader=FileSystemLoader(str(PROJECT_ROOT / "app" / "templates")))
            html = env.get_template("dashboard.html").render(
                data=data,
                request=None,
                msg=None,
                error=None,
            )
            assert "Registrar comentário já publicado" in html
            assert "Registrar publicação e iniciar Scout" in html
            assert "Commentaire exato do painel" in html
            assert "jean.panel" in html

    finally:
        object.__setattr__(settings, "database_path", original_db)

    print("OK - integração do painel com o Scout validada offline.")


if __name__ == "__main__":
    main()
