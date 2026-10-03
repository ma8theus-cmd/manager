import unittest

from app.registration_destinations import (
    ROUTE_DEFINITIONS,
    RegistrationDestinationError,
    get_destination,
)


class RegistrationDestinationTests(unittest.TestCase):
    def test_defines_the_three_account_lots(self):
        self.assertEqual(
            get_destination("PRIMARY_VPN"),
            {"target": "PRIMARY", "network": "VPN", "limit_key": "registration_primary_vpn_limit"},
        )
        self.assertEqual(
            get_destination("SECONDARY_DIRECT"),
            {"target": "SECONDARY", "network": "DIRECT", "limit_key": "registration_secondary_direct_limit"},
        )
        self.assertEqual(
            get_destination("SECONDARY_VPN"),
            {"target": "SECONDARY", "network": "VPN", "limit_key": "registration_secondary_vpn_limit"},
        )

    def test_rejects_ambiguous_legacy_route_names(self):
        with self.assertRaises(RegistrationDestinationError):
            get_destination("DIRECT")
        with self.assertRaises(RegistrationDestinationError):
            get_destination("VPN")

    def test_only_three_destinations_are_exposed(self):
        self.assertEqual(set(ROUTE_DEFINITIONS), {"PRIMARY_VPN", "SECONDARY_DIRECT", "SECONDARY_VPN"})


if __name__ == "__main__":
    unittest.main()
