from __future__ import annotations

from functools import lru_cache
import ipaddress
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PLATFORM_SCHEMA = "platform"
PLATFORM_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_SECRET_KEY = "development-only-secret-key-change-before-production"


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PLATFORM_ROOT / ".env.platform",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    platform_environment: str = "development"
    platform_log_level: str = "INFO"
    # Nginx is the production request log owner.  Keep Gunicorn access logs
    # available for local diagnostics but disable the duplicate stream in the
    # reviewed production baseline.
    platform_gunicorn_access_log: bool = True
    platform_web_origin: str = "http://127.0.0.1:3000"
    platform_api_host: str = "127.0.0.1"
    platform_api_port: int = 8010
    platform_api_forwarded_allow_ips: str = "127.0.0.1"
    # Exact egress addresses used by approved load generators. This only
    # bypasses per-IP throttles; account, user, byte and global capacity
    # protections remain authoritative.
    platform_load_test_source_ips: str = ""
    platform_web_bind_host: str = "127.0.0.1"
    platform_web_port: int = 3000
    platform_database_url: str = (
        "postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/platformdb"
    )
    platform_db_schema: str = PLATFORM_SCHEMA
    platform_secret_key: str = DEVELOPMENT_SECRET_KEY
    platform_session_cookie_name: str = "deadlock_platform_session"
    platform_session_ttl_days: int = 14
    platform_session_max_active: int = Field(default=5, ge=1, le=50)
    platform_session_touch_interval_seconds: int = Field(default=300, ge=60, le=3600)
    platform_cookie_secure: bool = False
    platform_public_registration_enabled: bool | None = None
    platform_email_verification_required: bool | None = None
    platform_email_verification_ttl_minutes: int = Field(default=10, ge=5, le=30)
    platform_password_reset_ttl_minutes: int = Field(default=10, ge=5, le=30)
    platform_steam_login_enabled: bool = False
    # Steam OpenID always verifies against the provider endpoint hard-coded in
    # the API service. These settings only control the local callback and flow.
    platform_steam_callback_url: str | None = None
    platform_steam_openid_timeout_seconds: float = Field(default=5.0, gt=0, le=15)
    platform_auth_flow_ttl_minutes: int = Field(default=15, ge=5, le=30)
    platform_auth_delivery_cooldown_seconds: int = Field(default=60, ge=30, le=300)
    platform_csrf_enabled: bool | None = None
    platform_auth_rate_limit_enabled: bool | None = None
    platform_auth_login_window_seconds: int = Field(default=600, ge=60, le=86_400)
    platform_auth_login_account_limit: int = Field(default=8, ge=1, le=1_000)
    platform_auth_login_account_cooldown_seconds: int = Field(
        default=60, ge=30, le=3_600
    )
    platform_auth_login_ip_limit: int = Field(default=60, ge=1, le=10_000)
    platform_auth_register_window_seconds: int = Field(default=600, ge=60, le=86_400)
    platform_auth_register_ip_limit: int = Field(default=5, ge=1, le=1_000)
    platform_auth_reset_window_seconds: int = Field(default=900, ge=60, le=86_400)
    platform_auth_reset_ip_limit: int = Field(default=6, ge=1, le=1_000)
    platform_auth_reset_account_limit: int = Field(default=3, ge=1, le=100)
    platform_auth_generic_response_min_seconds: float = Field(
        default=0.35,
        ge=0,
        le=2,
    )
    platform_auth_progressive_delay_base_seconds: float = Field(
        default=0.15, ge=0, le=5
    )
    platform_auth_progressive_delay_max_seconds: float = Field(default=1.5, ge=0, le=10)
    platform_auth_adaptive_turnstile_threshold: int = Field(default=3, ge=1, le=100)
    platform_invite_rate_limit_enabled: bool | None = None
    platform_invite_rate_window_seconds: int = Field(default=900, ge=60, le=86_400)
    platform_invite_lookup_user_limit: int = Field(default=60, ge=1, le=10_000)
    platform_invite_lookup_ip_limit: int = Field(default=120, ge=1, le=50_000)
    platform_invite_claim_user_limit: int = Field(default=12, ge=1, le=1_000)
    platform_invite_claim_ip_limit: int = Field(default=60, ge=1, le=10_000)
    platform_invite_manage_user_limit: int = Field(default=30, ge=1, le=1_000)
    platform_invite_manage_ip_limit: int = Field(default=120, ge=1, le=10_000)
    platform_turnstile_site_key: str | None = None
    platform_turnstile_secret_key: str | None = None
    platform_turnstile_mode: str = "off"
    platform_turnstile_expected_hostname: str | None = None
    platform_turnstile_timeout_seconds: float = Field(default=3.0, gt=0, le=15)
    platform_redis_url: str = "redis://127.0.0.1:6379/0"
    platform_celery_broker_url: str = "redis://127.0.0.1:6379/1"
    platform_celery_result_backend: str = "redis://127.0.0.1:6379/2"
    platform_deadlock_automation_max_tournaments_per_tick: int = Field(
        default=4,
        gt=0,
    )
    platform_deadlock_automation_retry_base_minutes: int = Field(default=1, gt=0)
    platform_deadlock_automation_retry_max_minutes: int = Field(default=60, gt=0)
    platform_home_content_cache_seconds: int = Field(default=2100, ge=60)
    platform_home_content_stale_seconds: int = Field(default=604800, ge=900)
    platform_external_content_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    platform_openai_api_key: str | None = None
    platform_openai_model: str = "gpt-5.6-luna"
    platform_openai_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    platform_support_recipient_email: str | None = None
    platform_email_sender_email: str | None = None
    platform_resend_api_key: str | None = None
    platform_resend_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    platform_support_smtp_host: str | None = None
    platform_support_smtp_port: int = Field(default=587, gt=0, le=65535)
    platform_support_smtp_username: str | None = None
    platform_support_smtp_password: str | None = None
    platform_support_smtp_sender_email: str | None = None
    platform_support_smtp_starttls: bool = True
    platform_support_smtp_ssl: bool = False
    platform_support_rate_limit_per_hour: int = Field(default=3, gt=0, le=100)
    platform_upload_dir: Path = Field(
        default_factory=lambda: (
            Path(os.environ.get("PLATFORM_SHARED_DIR", PLATFORM_ROOT)) / "uploads"
        )
    )
    platform_avatar_max_bytes: int = 512 * 1024
    platform_tournament_cover_max_bytes: int = 512 * 1024
    platform_object_storage_backend: str = "local"
    platform_r2_endpoint_url: str | None = None
    platform_r2_access_key_id: str | None = None
    platform_r2_secret_access_key: str | None = None
    platform_r2_bucket_name: str | None = None
    platform_r2_region: str = "auto"
    platform_r2_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    platform_r2_read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    platform_r2_max_attempts: int = Field(default=4, ge=1, le=10)
    platform_media_public_base_url: str | None = None
    platform_media_staging_dir: Path = Field(
        default_factory=lambda: (
            Path(os.environ.get("PLATFORM_SHARED_DIR", PLATFORM_ROOT)) / "media-staging"
        )
    )
    platform_media_max_input_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=64 * 1024,
        le=25 * 1024 * 1024,
    )
    platform_media_max_pixels: int = Field(
        default=25_000_000, ge=1_000_000, le=100_000_000
    )
    platform_media_max_dimension: int = Field(default=10_000, ge=512, le=25_000)
    platform_media_max_variant_bytes: int = Field(
        default=512 * 1024,
        ge=32 * 1024,
        le=5 * 1024 * 1024,
    )
    platform_media_processing_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    platform_media_processing_max_attempts: int = Field(default=3, ge=1, le=10)
    platform_media_processing_stale_seconds: int = Field(default=300, ge=60, le=86_400)
    platform_media_retry_base_seconds: int = Field(default=10, ge=1, le=3_600)
    platform_media_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    platform_media_cleanup_grace_seconds: int = Field(
        default=86_400, ge=60, le=2_592_000
    )
    platform_media_reconciliation_batch_size: int = Field(default=32, ge=1, le=500)
    platform_media_staging_orphan_grace_seconds: int = Field(
        default=3_600,
        ge=300,
        le=604_800,
    )
    platform_media_max_staged_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=10 * 1024 * 1024 * 1024,
    )
    platform_media_max_staged_files: int = Field(default=256, ge=1, le=10_000)
    platform_media_processing_concurrency: int = Field(default=1, ge=1, le=8)
    platform_media_rate_limit_enabled: bool | None = None
    platform_media_upload_window_seconds: int = Field(default=3_600, ge=60, le=86_400)
    platform_media_upload_user_limit: int = Field(default=20, ge=1, le=1_000)
    platform_media_upload_ip_limit: int = Field(default=60, ge=1, le=10_000)
    platform_media_upload_user_byte_limit: int = Field(
        default=100 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=10 * 1024 * 1024 * 1024,
    )
    platform_perf_log_enabled: bool = True
    platform_perf_slow_request_ms: int = Field(default=1000, ge=0)
    platform_perf_slow_db_ms: int = Field(default=500, ge=0)
    platform_perf_sql_count_threshold: int = Field(default=25, ge=0)
    # Mutation request performance is still captured when slow, contended or
    # failed.  Logging every successful mutation in production adds more I/O
    # and CPU precisely during the bursts we need to measure.
    platform_perf_log_mutations: bool = False
    platform_api_workers: int = Field(default=2, gt=0)
    # Keep PostgreSQL connection count bounded per process. API and Celery
    # processes use separate budgets so background bursts cannot consume the
    # entire database capacity reserved for user requests.
    # Two API workers at 32 connections each plus the bounded worker pool fit
    # the default connection budget of 68.  Keep overflow disabled: a fixed
    # pool makes burst pressure observable without creating unbounded database
    # fan-out on the two-core production host.  The staged increase from 20 to
    # 24 connections reduced neither the checkout timeout nor the queue enough
    # for the measured zero-spread safety profile; PostgreSQL and Redis still
    # had headroom, so this remains a bounded burst cushion rather than
    # unbounded overflow.
    platform_db_pool_size: int = Field(default=32, gt=0)
    platform_db_max_overflow: int = Field(default=0, ge=0)
    platform_db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    platform_db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    platform_worker_db_pool_size: int = Field(default=2, gt=0)
    platform_worker_db_max_overflow: int = Field(default=0, ge=0)
    platform_worker_db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    platform_worker_db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    platform_worker_concurrency: int = Field(default=2, gt=0)
    platform_db_connection_budget: int = Field(default=68, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> PlatformSettings:
    return PlatformSettings()


@lru_cache(maxsize=32)
def parse_load_test_source_ips(raw_value: str) -> frozenset[str]:
    """Parse the exact host addresses trusted for an approved load run.

    CIDR ranges and wildcard/unspecified addresses are deliberately not
    accepted. The value is configuration, never request input.
    """

    # A few infrastructure unit tests use a partial Mock for settings. Treat
    # an absent/non-string value as the unset default, while keeping strict
    # validation for all real environment/configuration strings.
    if not isinstance(raw_value, str):
        return frozenset()

    addresses: set[str] = set()
    for raw_address in raw_value.split(","):
        address_text = raw_address.strip()
        if not address_text:
            continue
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise ValueError(
                "PLATFORM_LOAD_TEST_SOURCE_IPS must contain exact IP addresses."
            ) from exc
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError(
                "PLATFORM_LOAD_TEST_SOURCE_IPS cannot contain unspecified, loopback or multicast addresses."
            )
        addresses.add(str(address))
    return frozenset(addresses)


def is_load_test_source(settings: PlatformSettings, address: str) -> bool:
    try:
        normalized = str(ipaddress.ip_address(address.strip()))
    except ValueError:
        return False
    return normalized in parse_load_test_source_ips(
        settings.platform_load_test_source_ips
    )


def validate_platform_settings(
    settings: PlatformSettings,
    *,
    require_api_secret: bool | None = None,
) -> None:
    """Fail closed when a production process starts with unsafe core settings.

    The worker intentionally receives no API/session secret. When callers do
    not specify the boundary explicitly, the isolated worker service identity
    selects that reduced validation contract.
    """

    environment = settings.platform_environment.strip().lower()
    if environment not in {"development", "test", "production"}:
        raise RuntimeError(
            "PLATFORM_ENVIRONMENT must be development, test, or production."
        )
    normalized_database_url = settings.platform_database_url.replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    database_url = urlsplit(normalized_database_url)
    database_name = unquote(database_url.path.lstrip("/"))
    expected_database = "platformdb_test" if environment == "test" else "platformdb"
    if (
        database_url.scheme not in {"postgresql", "postgres"}
        or database_name != expected_database
    ):
        raise RuntimeError(
            f"{environment.title()} must use the isolated {expected_database} database."
        )
    if settings.platform_db_schema != PLATFORM_SCHEMA:
        raise RuntimeError(
            f"{environment.title()} must use the isolated platform schema."
        )
    try:
        parse_load_test_source_ips(settings.platform_load_test_source_ips)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    api_connection_budget = settings.platform_api_workers * (
        settings.platform_db_pool_size + settings.platform_db_max_overflow
    )
    worker_connection_budget = settings.platform_worker_concurrency * (
        settings.platform_worker_db_pool_size
        + settings.platform_worker_db_max_overflow
    )
    if (
        api_connection_budget + worker_connection_budget
        > settings.platform_db_connection_budget
    ):
        raise RuntimeError(
            "Configured API and worker PostgreSQL pools exceed PLATFORM_DB_CONNECTION_BUDGET."
        )
    if environment == "test":
        redis_url = urlsplit(settings.platform_redis_url)
        if redis_url.scheme not in {"redis", "rediss"} or redis_url.path != "/15":
            raise RuntimeError("Test runtime must use isolated Redis database 15.")
        if settings.platform_object_storage_backend.strip().lower() != "local":
            raise RuntimeError("Test runtime must not use production object storage.")
        return
    if environment != "production":
        return

    if require_api_secret is None:
        require_api_secret = os.environ.get("PLATFORM_RUNTIME_SERVICE") != "worker"

    if settings.platform_api_host != "127.0.0.1":
        raise RuntimeError("Production API must bind to loopback only.")
    if settings.platform_web_bind_host != "127.0.0.1":
        raise RuntimeError("Production web must bind to loopback only.")
    if settings.platform_api_forwarded_allow_ips != "127.0.0.1":
        raise RuntimeError(
            "Production forwarded headers must be trusted only from loopback."
        )

    if require_api_secret:
        secret = settings.platform_secret_key.strip()
        if len(secret) < 32 or secret == DEVELOPMENT_SECRET_KEY:
            raise RuntimeError(
                "Production requires an explicit PLATFORM_SECRET_KEY of at least 32 characters."
            )
    if settings.platform_object_storage_backend.strip().lower() != "r2":
        raise RuntimeError(
            "Production media storage must use PLATFORM_OBJECT_STORAGE_BACKEND=r2."
        )
    required_r2 = {
        "PLATFORM_R2_ENDPOINT_URL": settings.platform_r2_endpoint_url,
        "PLATFORM_R2_ACCESS_KEY_ID": settings.platform_r2_access_key_id,
        "PLATFORM_R2_SECRET_ACCESS_KEY": settings.platform_r2_secret_access_key,
        "PLATFORM_R2_BUCKET_NAME": settings.platform_r2_bucket_name,
        "PLATFORM_MEDIA_PUBLIC_BASE_URL": settings.platform_media_public_base_url,
    }
    missing = [name for name, value in required_r2.items() if not (value or "").strip()]
    if missing:
        raise RuntimeError(
            f"Production media settings are missing: {', '.join(missing)}."
        )

    endpoint = urlsplit(str(settings.platform_r2_endpoint_url))
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.path not in {"", "/"}
    ):
        raise RuntimeError(
            "PLATFORM_R2_ENDPOINT_URL must be an HTTPS account endpoint without a bucket path."
        )
    public_base = urlsplit(str(settings.platform_media_public_base_url))
    if public_base.scheme != "https" or not public_base.hostname:
        raise RuntimeError("PLATFORM_MEDIA_PUBLIC_BASE_URL must be an HTTPS origin.")
    if public_base.query or public_base.fragment:
        raise RuntimeError(
            "PLATFORM_MEDIA_PUBLIC_BASE_URL cannot contain a query or fragment."
        )
    if settings.platform_media_processing_concurrency != 1:
        raise RuntimeError(
            "Production currently requires PLATFORM_MEDIA_PROCESSING_CONCURRENCY=1."
        )
    if (
        settings.platform_media_max_staged_bytes
        < settings.platform_media_max_input_bytes
    ):
        raise RuntimeError(
            "PLATFORM_MEDIA_MAX_STAGED_BYTES must be at least PLATFORM_MEDIA_MAX_INPUT_BYTES."
        )
    web_origin = urlsplit(settings.platform_web_origin)
    callback_url = (settings.platform_steam_callback_url or "").strip()
    callback = urlsplit(callback_url)
    try:
        web_origin.port
        if settings.platform_steam_login_enabled:
            callback.port
    except ValueError as exc:
        raise RuntimeError(
            "Production web and Steam callback URLs must use valid ports."
        ) from exc
    if (
        web_origin.scheme != "https"
        or not web_origin.hostname
        or web_origin.username
        or web_origin.password
        or web_origin.path not in {"", "/"}
        or web_origin.query
        or web_origin.fragment
    ):
        raise RuntimeError(
            "PLATFORM_WEB_ORIGIN must be an HTTPS origin without a path, query, or fragment."
        )
    if settings.platform_steam_login_enabled and (
        not callback_url
        or callback.scheme != "https"
        or not callback.hostname
        or callback.username
        or callback.password
        or (callback.hostname, callback.port or 443)
        != (web_origin.hostname, web_origin.port or 443)
        or callback.path != "/api/v1/auth/steam/callback"
        or callback.query
        or callback.fragment
    ):
        raise RuntimeError(
            "PLATFORM_STEAM_CALLBACK_URL must use the public web HTTPS origin and exact /api/v1/auth/steam/callback path without a query or fragment."
        )
