import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path('/home/matheus/meslibertines_manager_v1')


class BatchWindowTests(unittest.TestCase):
    def _env_values(self):
        values = {}
        for line in (ROOT / '.env').read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if key in {
                    'DAILY_ACCOUNT_LIMIT',
                    'BATCH_INTERVAL_HOURS',
                    'REGISTRATION_DIRECT_INTERVAL_HOURS',
                    'REGISTRATION_DIRECT_LIMIT',
                    'REGISTRATION_VPN_DAILY_LIMIT',
                }:
                    values[key] = value
        return values

    def _fresh_database(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from app.config import settings
        from app.database import init_db

        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / 'members.db'
        original_path = settings.database_path
        object.__setattr__(settings, 'database_path', database_path)
        init_db()

        with sqlite3.connect(database_path) as conn:
            rows = [
                (
                    member_id,
                    f'First{member_id}',
                    'Member',
                    '1990-01-01',
                    'Paris',
                    '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00',
                )
                for member_id in range(1, 16)
            ]
            conn.executemany(
                '''
                INSERT INTO members(
                    id, first_name, last_name, birth_date, city_france,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                rows,
            )
            conn.commit()
        return temp_dir, original_path

    def test_environment_has_five_accounts_per_route_and_no_manager_timer(self):
        values = self._env_values()
        self.assertEqual(values.get('REGISTRATION_DIRECT_LIMIT'), '5')
        self.assertEqual(values.get('REGISTRATION_VPN_DAILY_LIMIT'), '5')
        self.assertNotIn('BATCH_INTERVAL_HOURS', values)
        self.assertNotIn('REGISTRATION_DIRECT_INTERVAL_HOURS', values)

    def test_reserve_batch_uses_independent_five_account_routes_without_timer(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from app.config import settings
        import app.services as services

        temp_dir, original_path = self._fresh_database()
        original_direct = settings.registration_direct_limit
        original_vpn = settings.registration_vpn_daily_limit
        original_authorized = settings.registration_vpn_authorized
        object.__setattr__(settings, 'registration_direct_limit', 5)
        object.__setattr__(settings, 'registration_vpn_daily_limit', 5)
        object.__setattr__(settings, 'registration_vpn_authorized', True)
        try:
            reserve_batch = getattr(services, 'reserve_batch', None)
            self.assertTrue(
                callable(reserve_batch),
                'reserve_batch(route) deve existir para os dois botões',
            )
            with patch.object(services, 'vpn_worker_ready', return_value=True):
                direct_first = reserve_batch('DIRECT')
                direct_second = reserve_batch('DIRECT')
                vpn_batch = reserve_batch('VPN')

            self.assertEqual(direct_first, [1, 2, 3, 4, 5])
            self.assertEqual(direct_second, [6, 7, 8, 9, 10])
            self.assertEqual(vpn_batch, [11, 12, 13, 14, 15])

            with sqlite3.connect(settings.database_path) as conn:
                routes = conn.execute(
                    'SELECT registration_route FROM members ORDER BY id'
                ).fetchall()
            self.assertEqual(
                [route[0] for route in routes],
                ['DIRECT'] * 10 + ['VPN'] * 5,
            )
        finally:
            object.__setattr__(settings, 'database_path', original_path)
            object.__setattr__(settings, 'registration_direct_limit', original_direct)
            object.__setattr__(settings, 'registration_vpn_daily_limit', original_vpn)
            object.__setattr__(settings, 'registration_vpn_authorized', original_authorized)
            temp_dir.cleanup()

    def test_batch_service_has_no_cooldown_gate(self):
        source = (ROOT / 'app/services.py').read_text()
        self.assertNotIn('batch_interval_hours', source)
        self.assertNotIn('_route_capacity', source)


if __name__ == '__main__':
    unittest.main()
