"""Configuration management: pydantic-settings based Settings singleton.

All environment variables use the `PANEL_` prefix.
Credentials are referenced by *path* only — never stored as plain text.
The `read_secret` helper reads the actual value at runtime from the secrets dir
or an absolute path.

Usage:
    from panel.config.settings import get_settings, read_secret

    settings = get_settings()           # cached singleton
    value = read_secret("my_token")     # reads /secrets/my_token (strips newline)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised application settings.

    All fields load from environment variables with the PANEL_ prefix.
    Credential fields accept *file paths* only; call read_secret() to get values.

    Sensitive-field naming convention (these must never appear in API responses):
        - Any field matching *secret*, *token*, *key*, *password*, *private_*
        - Any field ending in _file (path reference to a credential)
    """

    model_config = SettingsConfigDict(
        env_prefix="PANEL_",
        env_file=".env",
        extra="ignore",
    )

    # --- Core ---
    db_path: str = "/data/panel.db"
    port: int = 8080
    log_level: str = "info"

    # --- Collector ---
    stale_threshold_seconds: int = 180  # seconds before a collector is considered stale

    # --- Secrets directory ---
    secrets_dir: str = "/secrets"  # read-only mounted credentials directory

    # --- Azure (optional; disabled if not set) ---
    # Store path to a file containing the Azure client secret, not the secret itself.
    # (ARCH-001 credential-by-path convention: the secret value never lives in env/DB.)
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret_file: str = ""  # path: /secrets/azure_client_secret
    azure_subscription_id: str = ""

    @property
    def azure_configured(self) -> bool:
        """True iff all four Azure Service Principal fields are present.

        AzureVmCollector is disabled (register() skips with a warning) unless this
        is True. azure_client_secret_file is a *path*; its content is read lazily.
        """
        return bool(
            self.azure_tenant_id
            and self.azure_client_id
            and self.azure_client_secret_file
            and self.azure_subscription_id
        )

    # --- Tailscale (optional) ---
    tailscale_socket: str = "/var/run/tailscale/tailscaled.sock"

    # --- SSH (optional) ---
    # Path to the SSH private key file; never store the key content here.
    ssh_key_path: str = ""  # e.g. /secrets/id_ed25519

    # --- Ingest (ARCH-004 / TASK-030) ---
    # Bearer token for POST /api/ingest/* endpoints. Empty string disables auth
    # (endpoint accepts any request). Set via PANEL_INGEST_TOKEN to require a token.
    ingest_token: str = ""


def read_secret(name_or_path: str, settings: Settings | None = None) -> str:
    """Read a secret value from the secrets directory or an absolute path.

    Args:
        name_or_path: Either a bare filename (looked up inside settings.secrets_dir)
                      or an absolute path directly to the secret file.
        settings:     Optional Settings instance; defaults to get_settings().

    Returns:
        File contents with leading/trailing whitespace stripped.

    Raises:
        FileNotFoundError: If the secret file does not exist.
        ValueError: If name_or_path is empty.
    """
    if not name_or_path:
        raise ValueError("name_or_path must not be empty")

    cfg = settings if settings is not None else get_settings()
    candidate = Path(name_or_path)
    path = candidate if candidate.is_absolute() else Path(cfg.secrets_dir) / name_or_path
    return path.read_text(encoding="utf-8").strip()


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    In tests, call get_settings.cache_clear() before patching env vars
    to force re-instantiation.
    """
    return Settings()
