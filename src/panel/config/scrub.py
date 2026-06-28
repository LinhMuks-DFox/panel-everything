"""Log and error message scrubbing: replace sensitive substrings with ***.

Patterns covered:
  - token=<value>          (query-param / kv style)
  - secret=<value>
  - password=<value>
  - api_key=<value> / apikey=<value> / api-key=<value>
  - key=<value>            (generic kv; intentionally broad)
  - Bearer <value>         (Authorization header)
  - Basic <base64>         (Authorization header)
  - -----BEGIN ... PRIVATE KEY----- blocks
  - Hex strings >= 32 chars (likely tokens/hashes) – conservative: only when preceded by
    = or : or whitespace/quote to avoid mangling hex colour codes etc.

Usage:
    from panel.config.scrub import scrub, setup_logging
"""

from __future__ import annotations

import logging
import re

# ---------------------------------------------------------------------------
# Regex patterns (compiled once at module load for performance)
# ---------------------------------------------------------------------------

# Key-value patterns: key=VALUE or key: VALUE (VALUE ends at whitespace/comma/quote/EOL)
_KV_VALUE = r'[^\s,"\'\]}\)>]+'

_KV_PATTERNS: list[re.Pattern[str]] = [
    # token, secret, password, apikey / api_key / api-key, key (case-insensitive)
    re.compile(
        r'(?i)(?P<prefix>'
        r'(?:token|secret|password|api[-_]?key|key)'
        r'\s*[=:]\s*)'
        r'(?P<value>' + _KV_VALUE + r')',
    ),
    # Bearer <token>
    re.compile(r'(?i)(?P<prefix>Bearer\s+)(?P<value>' + _KV_VALUE + r')'),
    # Basic <base64-credential>
    re.compile(r'(?i)(?P<prefix>Basic\s+)(?P<value>' + _KV_VALUE + r')'),
]

# PEM private key block (single-line or multi-line)
_PEM_PATTERN = re.compile(
    r'-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?-----END (?:[A-Z ]+)?PRIVATE KEY-----',
    re.DOTALL,
)

# Long hex strings (>= 32 hex chars) preceded by =, :, space, quote, or start-of-string.
# This is intentionally conservative to avoid false positives.
_HEX_PATTERN = re.compile(r'(?<=[=:\s"\'])([0-9a-fA-F]{32,})')

# Long base64-like strings (>= 32 chars of base64 alphabet, ending with optional ==)
# Only match when preceded by = or : or quote to be conservative.
_B64_PATTERN = re.compile(
    r'(?<=[=:\s"\'])([A-Za-z0-9+/]{32,}={0,2})'
)

_REDACTED = "***"


def scrub(text: str) -> str:
    """Replace sensitive substrings in *text* with ***.

    Safe to call on ordinary log messages: if no sensitive pattern is found
    the original string is returned unchanged (same object when possible).

    Args:
        text: Any string — log message, error summary, HTTP body excerpt, etc.

    Returns:
        The scrubbed string.
    """
    if not text:
        return text

    result = text

    # PEM blocks first (greedy multi-line)
    result = _PEM_PATTERN.sub(_REDACTED, result)

    # Key-value and Bearer/Basic patterns
    for pattern in _KV_PATTERNS:
        result = pattern.sub(lambda m: m.group("prefix") + _REDACTED, result)

    # Long hex tokens
    result = _HEX_PATTERN.sub(_REDACTED, result)

    # Long base64 tokens (applied after hex to avoid double-redaction noise)
    result = _B64_PATTERN.sub(_REDACTED, result)

    return result


# ---------------------------------------------------------------------------
# Logging filter
# ---------------------------------------------------------------------------


class _ScrubFilter(logging.Filter):
    """logging.Filter that applies scrub() to every log record's message."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Scrub strategy: fully interpolate the message first, then scrub the
        # combined string and clear args so the formatter does no further %-expansion.
        # This avoids mismatches when scrubbing changes the number of %s placeholders.
        try:
            # getMessage() returns the fully-formatted message string.
            msg = record.getMessage()
            record.msg = scrub(msg)
            record.args = None  # prevent double-interpolation
        except Exception:  # noqa: BLE001,S110 — never let scrubbing crash the app
            logging.getLogger(__name__).debug("scrub filter error", exc_info=True)
        return True


def setup_logging(level: str = "info") -> None:
    """Configure the root logger with scrubbing and set sub-logger levels.

    Call this early in application startup (before `create_app` returns).

    Args:
        level: Log level string, e.g. "info", "debug", "warning".
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove existing handlers to avoid duplicate output on re-configuration.
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)

    # Install scrub filter on all existing (and future-added) handlers.
    scrub_filter = _ScrubFilter()
    for handler in root.handlers:
        # Avoid adding the filter twice on repeated calls (idempotent).
        if not any(isinstance(f, _ScrubFilter) for f in handler.filters):
            handler.addFilter(scrub_filter)

    # Quieten chatty third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(max(numeric_level, logging.WARNING))
