import unittest

from app.site_adapter.meslibertines import MesLibertinesAdapter


class ProfileCitySearchTests(unittest.TestCase):
    def test_multiword_city_gets_safe_prefix_retry(self):
        self.assertEqual(
            MesLibertinesAdapter._profile_city_searches("Le Mans"),
            ["Le Mans", "Le Man"],
        )


if __name__ == "__main__":
    unittest.main()
