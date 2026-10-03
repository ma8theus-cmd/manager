from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    # Node role for the two-VPS architecture.
    # all = legacy/single-node compatibility during migration
    # control = VPS1: registrations + public research collector
    # comment_worker = VPS2: comment posting + comment Scout
    node_role: str = os.getenv("MESLIB_NODE_ROLE", "all").strip().lower() or "all"
    worker_control_url: str = os.getenv("WORKER_CONTROL_URL", "").strip().rstrip("/")
    worker_api_token: str = os.getenv("WORKER_API_TOKEN", "").strip()
    database_path: Path = PROJECT_ROOT / os.getenv("DATABASE_PATH", "data/members.db")
    csv_path: Path = PROJECT_ROOT / os.getenv("CSV_PATH", "data/perfildosmembros.csv")
    email_domain: str = os.getenv("EMAIL_DOMAIN", "").strip()
    universal_password: str = os.getenv("UNIVERSAL_ACCOUNT_PASSWORD", "")
    # Manual registration batches. The site may apply its own cooldown;
    # the Manager does not impose a timer between button presses.
    registration_direct_limit: int = max(0, int(os.getenv("REGISTRATION_DIRECT_LIMIT", "5")))
    registration_vpn_daily_limit: int = max(0, int(os.getenv("REGISTRATION_VPN_DAILY_LIMIT", "5")))
    registration_primary_vpn_limit: int = max(0, int(os.getenv("REGISTRATION_PRIMARY_VPN_LIMIT", os.getenv("REGISTRATION_VPN_DAILY_LIMIT", "5"))))
    registration_secondary_direct_limit: int = max(0, int(os.getenv("REGISTRATION_SECONDARY_DIRECT_LIMIT", os.getenv("REGISTRATION_DIRECT_LIMIT", "5"))))
    registration_secondary_vpn_limit: int = max(0, int(os.getenv("REGISTRATION_SECONDARY_VPN_LIMIT", os.getenv("REGISTRATION_VPN_DAILY_LIMIT", "5"))))
    worker_request_timeout_seconds: int = max(5, int(os.getenv("WORKER_REQUEST_TIMEOUT_SECONDS", "20")))
    worker_sync_poll_seconds: int = max(5, int(os.getenv("WORKER_SYNC_POLL_SECONDS", "15")))
    discord_notification_poll_seconds: int = max(5, int(os.getenv("DISCORD_NOTIFICATION_POLL_SECONDS", "5")))
    registration_timezone: str = os.getenv("REGISTRATION_TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
    registration_vpn_authorized: bool = _as_bool(os.getenv("REGISTRATION_VPN_AUTHORIZED"), False)
    registration_vpn_exec: str = os.getenv("REGISTRATION_VPN_EXEC", "").strip()
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    twocaptcha_api_key: str = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
    comment_captcha_mode: str = os.getenv("COMMENT_CAPTCHA_MODE", "auto").strip().lower()
    comment_manual_wait_seconds: int = max(
        60, int(os.getenv("COMMENT_MANUAL_WAIT_SECONDS", "600"))
    )
    headless: bool = _as_bool(os.getenv("HEADLESS"), False)
    registration_url: str = os.getenv(
        "REGISTRATION_URL",
        "https://m.meslibertines.com/en/users/register/",
    )

    # Two absolute offsets from publication; legacy hourly settings are retired.
    scout_check_offsets_minutes: tuple[int, ...] = (360, 480)
    scout_max_checks: int = 2
    scout_scheduler_poll_seconds: int = int(os.getenv("SCOUT_SCHEDULER_POLL_SECONDS", "60"))
    scout_page_timeout_seconds: int = int(os.getenv("SCOUT_PAGE_TIMEOUT_SECONDS", "30"))
    scout_max_due_per_loop: int = int(os.getenv("SCOUT_MAX_DUE_PER_LOOP", "5"))
    scout_headless: bool = _as_bool(os.getenv("SCOUT_HEADLESS"), True)

    # Authentic-comment automation. URLs remain empty until configured.
    comment_timezone: str = os.getenv("COMMENT_TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
    comment_window_start_hour: int = max(0, min(23, int(os.getenv("COMMENT_WINDOW_START_HOUR", "10"))))
    comment_window_end_hour: int = max(1, min(24, int(os.getenv("COMMENT_WINDOW_END_HOUR", "22"))))
    comment_scheduler_poll_seconds: int = max(30, int(os.getenv("COMMENT_SCHEDULER_POLL_SECONDS", "60")))
    comment_headless: bool = _as_bool(os.getenv("COMMENT_HEADLESS"), True)
    comment_require_author_confirmation: bool = _as_bool(
        os.getenv("COMMENT_REQUIRE_AUTHOR_CONFIRMATION"), True
    )
    comment_min_interval_minutes: int = max(
        4, int(os.getenv("COMMENT_MIN_INTERVAL_MINUTES", "4"))
    )
    comment_professional_interval_minutes: int = max(
        1, int(os.getenv("COMMENT_PROFESSIONAL_INTERVAL_MINUTES", "480"))
    )

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    @property
    def runs_comment_worker(self) -> bool:
        return self.node_role in {"all", "comment_worker"}

    @property
    def runs_control_plane(self) -> bool:
        return self.node_role in {"all", "control"}

    @property
    def screenshots_dir(self) -> Path:
        return self.project_root / "screenshots"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"


settings = Settings()
