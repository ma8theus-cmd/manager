from __future__ import annotations

from typing import Any


class RegistrationDestinationError(ValueError):
    """Raised when a batch destination is missing or ambiguous."""


ROUTE_DEFINITIONS: dict[str, dict[str, str]] = {
    "PRIMARY_VPN": {
        "target": "PRIMARY",
        "network": "VPN",
        "limit_key": "registration_primary_vpn_limit",
    },
    "SECONDARY_DIRECT": {
        "target": "SECONDARY",
        "network": "DIRECT",
        "limit_key": "registration_secondary_direct_limit",
    },
    "SECONDARY_VPN": {
        "target": "SECONDARY",
        "network": "VPN",
        "limit_key": "registration_secondary_vpn_limit",
    },
}


def get_destination(name: str) -> dict[str, Any]:
    key = str(name).strip().upper()
    try:
        return dict(ROUTE_DEFINITIONS[key])
    except KeyError as exc:
        raise RegistrationDestinationError(
            "Destino inválido. Escolha PRIMARY_VPN, SECONDARY_DIRECT ou SECONDARY_VPN."
        ) from exc
