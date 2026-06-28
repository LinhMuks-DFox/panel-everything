"""TASK-005: tests for Settings, read_secret, scrub, setup_logging, PublicModel.

Test coverage:
  - get_settings() loads from env (PANEL_ prefix) and is lru_cache'd
  - read_secret() reads from secrets_dir and from absolute path, strips newlines
  - scrub() redacts token/secret/key/password/Bearer/PEM blocks
  - scrub() leaves ordinary text unchanged
  - setup_logging() installs scrub filter so sensitive log output is redacted
  - PublicModel subclass rejects extra fields (extra="forbid")
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from panel.config.scrub import scrub, setup_logging
from panel.config.settings import Settings, get_settings, read_secret
from panel.domain.models import PublicModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Ensure get_settings() is re-evaluated for each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Settings & get_settings
# ---------------------------------------------------------------------------


def test_settings_defaults():
    """Settings instantiate with built-in defaults (no env required)."""
    s = Settings()
    assert s.db_path == "/data/panel.db"
    assert s.port == 8080
    assert s.log_level == "info"
    assert s.stale_threshold_seconds == 180
    assert s.secrets_dir == "/secrets"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    """Settings pick up PANEL_-prefixed environment variables."""
    monkeypatch.setenv("PANEL_PORT", "9090")
    monkeypatch.setenv("PANEL_LOG_LEVEL", "debug")
    monkeypatch.setenv("PANEL_STALE_THRESHOLD_SECONDS", "60")

    s = Settings()
    assert s.port == 9090
    assert s.log_level == "debug"
    assert s.stale_threshold_seconds == 60


def test_get_settings_returns_singleton(monkeypatch: pytest.MonkeyPatch):
    """get_settings() returns the same instance on repeated calls (lru_cache)."""
    monkeypatch.setenv("PANEL_PORT", "7070")
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_get_settings_cache_clear_reinitialises(monkeypatch: pytest.MonkeyPatch):
    """After cache_clear(), get_settings() picks up updated env."""
    monkeypatch.setenv("PANEL_PORT", "1111")
    s1 = get_settings()
    assert s1.port == 1111

    get_settings.cache_clear()
    monkeypatch.setenv("PANEL_PORT", "2222")
    s2 = get_settings()
    assert s2.port == 2222
    assert s1 is not s2


def test_optional_credential_fields_default_empty():
    """Credential fields (azure, ssh) default to empty string — no error when absent."""
    s = Settings()
    assert s.azure_tenant_id == ""
    assert s.azure_client_id == ""
    assert s.azure_client_secret_file == ""
    assert s.ssh_key_path == ""


# ---------------------------------------------------------------------------
# read_secret
# ---------------------------------------------------------------------------


def test_read_secret_from_secrets_dir():
    """read_secret(name) reads <secrets_dir>/<name> and strips trailing newline."""
    with tempfile.TemporaryDirectory() as tmpdir:
        secret_file = Path(tmpdir) / "my_token"
        secret_file.write_text("supersecret\n", encoding="utf-8")

        s = Settings(secrets_dir=tmpdir)
        value = read_secret("my_token", settings=s)
        assert value == "supersecret"


def test_read_secret_from_absolute_path():
    """read_secret(absolute_path) reads the file directly, ignoring secrets_dir."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("  absolute_value  \n")
        abs_path = f.name

    try:
        s = Settings(secrets_dir="/nonexistent")
        value = read_secret(abs_path, settings=s)
        assert value == "absolute_value"
    finally:
        os.unlink(abs_path)


def test_read_secret_strips_whitespace():
    """read_secret strips leading/trailing whitespace from file content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        secret_file = Path(tmpdir) / "padded"
        secret_file.write_text("\n\n  token_value  \n\n", encoding="utf-8")

        s = Settings(secrets_dir=tmpdir)
        value = read_secret("padded", settings=s)
        assert value == "token_value"


def test_read_secret_missing_file_raises():
    """read_secret raises FileNotFoundError when the file does not exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        s = Settings(secrets_dir=tmpdir)
        with pytest.raises(FileNotFoundError):
            read_secret("nonexistent_secret", settings=s)


def test_read_secret_empty_name_raises():
    """read_secret raises ValueError for an empty name."""
    s = Settings()
    with pytest.raises(ValueError, match="must not be empty"):
        read_secret("", settings=s)


# ---------------------------------------------------------------------------
# scrub()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_text, should_be_redacted",
    [
        ("token=abc123def456", True),
        ("secret=mysecretvalue", True),
        ("password=hunter2", True),
        ("api_key=someapikey123", True),
        ("apikey=someapikey", True),
        ("api-key=somekey", True),
        ("key=mykey123", True),
        ("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload", True),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", True),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\n-----END RSA PRIVATE KEY-----",
            True,
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANB\n-----END PRIVATE KEY-----",
            True,
        ),
    ],
)
def test_scrub_redacts_sensitive_patterns(input_text: str, should_be_redacted: bool):
    """scrub() replaces sensitive substrings with ***."""
    result = scrub(input_text)
    assert "***" in result, f"Expected redaction in: {result!r}"
    # Original sensitive value should not appear verbatim
    # (we check *** is present, original kv structure may remain with *** value)


def test_scrub_preserves_ordinary_text():
    """scrub() does not mangle normal log messages."""
    ordinary = "Collector azure ran successfully. Elapsed: 123ms, samples: 5."
    assert scrub(ordinary) == ordinary


def test_scrub_preserves_urls_without_credentials():
    """scrub() does not mangle plain URLs."""
    url = "http://example.com/api/v1/status"
    assert scrub(url) == url


def test_scrub_empty_string():
    """scrub('') returns ''."""
    assert scrub("") == ""


def test_scrub_token_kv_value_replaced():
    """The value part after token= is replaced, not the key name."""
    result = scrub("Sending request with token=ABCD1234EFGH")
    assert "token=" in result
    assert "ABCD1234EFGH" not in result
    assert "***" in result


def test_scrub_secret_kv_value_replaced():
    """The value after secret= is replaced."""
    result = scrub("config secret=my_very_secret_value end")
    assert "secret=" in result
    assert "my_very_secret_value" not in result


def test_scrub_bearer_token_replaced():
    """Bearer token is redacted, 'Bearer' keyword is preserved."""
    result = scrub("Authorization: Bearer eyToken123abc")
    assert "Bearer" in result
    assert "eyToken123abc" not in result
    assert "***" in result


def test_scrub_pem_block_replaced():
    """Full PEM private key block is replaced."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA2a2rwplBQLzHPZe5TNJKF7fKBMSCCDef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = scrub(pem)
    assert "MIIEpAIBAAK" not in result
    assert "***" in result


# ---------------------------------------------------------------------------
# setup_logging() and log scrubbing
# ---------------------------------------------------------------------------


def test_setup_logging_installs_scrub_filter(caplog: pytest.LogCaptureFixture):
    """After setup_logging(), log records containing secrets are scrubbed."""
    setup_logging("info")

    logger = logging.getLogger("panel.test_scrub")

    with caplog.at_level(logging.INFO, logger="panel.test_scrub"):
        logger.info("Loaded config with token=%s", "supersecrettoken123")

    # The captured log text should not contain the original secret.
    full_output = caplog.text
    assert "supersecrettoken123" not in full_output
    # The word "token" (the key name) should still be present.
    assert "token" in full_output


def test_setup_logging_idempotent():
    """Calling setup_logging() multiple times does not raise."""
    setup_logging("info")
    setup_logging("debug")
    setup_logging("warning")
    # No assertion needed — just must not crash.


# ---------------------------------------------------------------------------
# PublicModel
# ---------------------------------------------------------------------------


class _SampleResponse(PublicModel):
    status: str
    count: int


def test_public_model_accepts_declared_fields():
    """PublicModel subclass instantiates with declared fields."""
    obj = _SampleResponse(status="ok", count=42)
    assert obj.status == "ok"
    assert obj.count == 42


def test_public_model_rejects_extra_fields():
    """PublicModel subclass raises ValidationError when extra fields are provided."""
    extra: dict[str, object] = {"status": "ok", "count": 1, "extra_field": "leaked"}
    with pytest.raises(ValidationError):
        _SampleResponse(**extra)  # type: ignore[arg-type]


def test_public_model_rejects_extra_fields_with_credential_name():
    """Extra fields with credential-like names are also rejected (extra=forbid)."""
    extra: dict[str, object] = {"status": "ok", "count": 1, "api_key": "somekey"}
    with pytest.raises(ValidationError):
        _SampleResponse(**extra)  # type: ignore[arg-type]
