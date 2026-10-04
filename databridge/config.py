"""Application settings, read from environment variables prefixed DATABRIDGE_ or a .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATABRIDGE_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    # SQLAlchemy URL for the metadata store. Blank = SQLite file inside data_dir.
    database_url: str = ""
    # Fernet key used to encrypt connection secrets. Blank = generated into data_dir/secret.key.
    secret_key: str = ""
    max_upload_mb: int = 100
    preview_rows: int = 20
    default_page_size: int = 100
    max_page_size: int = 10_000
    public_base_url: str = "http://localhost:8000"
    # Serve Flutter/CanvasKit assets locally instead of from a CDN (air-gapped or proxied networks).
    no_cdn: bool = False

    # Authentication
    session_hours: int = 12            # absolute lifetime of a normal sign-in
    remember_days: int = 14            # lifetime when "keep me signed in" is ticked
    idle_minutes: int = 60             # normal sessions end after this much inactivity
    max_failed_logins: int = 5         # then the account locks for lockout_minutes
    lockout_minutes: int = 15
    min_password_length: int = 10
    # First admin, created at startup only if there are no users yet (must change password on first sign-in).
    admin_username: str = ""
    admin_password: str = ""

    # AI
    ai_enabled: bool = True
    ai_request_timeout: int = 120          # seconds per model call
    ai_monthly_token_limit: int = 0        # per user, prompt + completion tokens; 0 = unlimited
    ai_log_content: bool = False           # store prompt/response text in the usage ledger
    # Personal API keys may only call public HTTPS endpoints (blocks SSRF into the server's network).
    ai_allow_private_urls: bool = False
    # Extra CA bundle (PEM file path) trusted for all AI calls, e.g. a company root CA or TLS-inspecting proxy.
    ai_ca_bundle: str = ""
    # Company LLM configuration file (YAML), applied at startup: endpoints, model catalog, defaults.
    ai_config_file: str = ""
    # Health checks of model endpoints: every N minutes (0 = off); alerts go to the audit log and this webhook
    # (Slack / Teams / any URL accepting {"text": ...}).
    ai_health_minutes: int = 15
    ai_alert_webhook: str = ""
    ai_cert_warn_days: int = 14
    # Built-in schedule for sources (REST APIs, tables, SQL, files): off = only manual / API refreshes
    source_scheduler: bool = True

    # Transport security: HTTPS and secure websockets (wss://). See databridge/web_security.py.
    require_https: str = "auto"            # auto = on when public_base_url is https; or true / false
    allowed_origins: str = ""              # extra browser origins allowed to open websockets (same-origin always is)
    trusted_proxies: str = "127.0.0.1"     # proxies whose X-Forwarded-* headers are believed; "*" = any
    hsts_seconds: int = 31_536_000         # Strict-Transport-Security max-age on HTTPS responses (0 = off)
    ws_max_connections: int = 500          # open studio websockets in total
    stream_max_connections: int = 200      # open streaming-API websockets in total (separate budget)
    ws_max_per_ip: int = 30                # open websockets per client address
    stream_max_per_key: int = 10           # streaming API connections per API key
    stream_idle_minutes: int = 30          # close streaming connections with no subscription and no messages
    # Built-in TLS (https/wss without a proxy), used by `python -m databridge.serve`
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    tls_cert_file: str = ""                # PEM certificate (with intermediates)
    tls_key_file: str = ""                 # PEM private key
    tls_key_password: str = ""
    tls_min_version: str = "1.2"           # 1.2 or 1.3
    tls_client_ca_file: str = ""           # CA for client certificates (mutual TLS)
    tls_client_cert: str = "none"          # none | optional | required

    @property
    def https_required(self) -> bool:
        value = self.require_https.strip().lower()
        if value in ("auto", ""):
            return self.public_base_url.lower().startswith("https://")
        return value in ("1", "true", "yes", "on")

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert_file and self.tls_key_file)

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'databridge.db').resolve()}"

    def ensure_dirs(self) -> None:
        for sub in ("snapshots", "datasets", "uploads", "rejects"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
