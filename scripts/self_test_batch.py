from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.database import init_db
import app.services as services


def main():
    original_path = settings.database_path
    original_direct = settings.registration_direct_limit
    original_vpn = settings.registration_vpn_daily_limit
    original_authorized = settings.registration_vpn_authorized

    with tempfile.TemporaryDirectory() as td:
        database_path = Path(td) / "members.db"
        object.__setattr__(settings, "database_path", database_path)
        object.__setattr__(settings, "registration_direct_limit", 5)
        object.__setattr__(settings, "registration_vpn_daily_limit", 5)
        object.__setattr__(settings, "registration_vpn_authorized", True)
        init_db()

        with sqlite3.connect(database_path) as conn:
            conn.executemany(
                """
                INSERT INTO members(
                    id, first_name, last_name, birth_date, city_france,
                    username, email, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        member_id,
                        f"First{member_id}",
                        "Member",
                        "1990-01-01",
                        "Paris",
                        f"member{member_id}",
                        f"member{member_id}@test.invalid",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    )
                    for member_id in range(1, 16)
                ],
            )
            conn.commit()

        try:
            with patch.object(services, "vpn_worker_ready", return_value=True):
                assert services.reserve_batch("DIRECT") == [1, 2, 3, 4, 5]
                assert services.reserve_batch("DIRECT") == [6, 7, 8, 9, 10]
                assert services.reserve_batch("VPN") == [11, 12, 13, 14, 15]
        finally:
            object.__setattr__(settings, "database_path", original_path)
            object.__setattr__(settings, "registration_direct_limit", original_direct)
            object.__setattr__(settings, "registration_vpn_daily_limit", original_vpn)
            object.__setattr__(settings, "registration_vpn_authorized", original_authorized)

    print("OK - lotes direto/VPN de 5 e acionamento repetido validados offline.")


if __name__ == "__main__":
    main()
