from __future__ import annotations

from app.site_adapter.meslibertines import ManualIntervention


def is_cloudflare_intervention(exc: BaseException) -> bool:
    """Return true only for the adapter's explicit Cloudflare intervention."""
    return isinstance(exc, ManualIntervention) and "cloudflare" in str(exc).casefold()
