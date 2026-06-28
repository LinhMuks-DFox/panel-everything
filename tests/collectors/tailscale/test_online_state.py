"""TASK-020: determine_online_state() 单元测试。

覆盖全部三态边界:
  - online=True → ONLINE (无视 last_seen)
  - online=False, last_seen=None → OFFLINE
  - online=False, last_seen=23h55m ago → OFFLINE (在阈值内)
  - online=False, last_seen=24h0m ago → OFFLINE (恰好等于阈值, 不超过)
  - online=False, last_seen=24h01m ago → LONG_OFFLINE (超过阈值 1 分钟)
  - online=False, last_seen=48h ago → LONG_OFFLINE (远超阈值)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panel.collectors.tailscale.collector import determine_online_state


def _now() -> datetime:
    return datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# ONLINE 态
# --------------------------------------------------------------------------- #


def test_online_true_returns_online():
    assert determine_online_state(online=True, last_seen=None, now=_now()) == "ONLINE"


def test_online_true_ignores_last_seen():
    """即使 last_seen 非常久远, online=True 仍返回 ONLINE。"""
    old = _now() - timedelta(days=365)
    assert determine_online_state(online=True, last_seen=old, now=_now()) == "ONLINE"


# --------------------------------------------------------------------------- #
# OFFLINE 态
# --------------------------------------------------------------------------- #


def test_offline_no_last_seen_returns_offline():
    assert determine_online_state(online=False, last_seen=None, now=_now()) == "OFFLINE"


def test_offline_last_seen_within_threshold_returns_offline():
    """last_seen = 23h55m 前,在 24h 阈值内 → OFFLINE。"""
    last_seen = _now() - timedelta(hours=23, minutes=55)
    assert determine_online_state(online=False, last_seen=last_seen, now=_now()) == "OFFLINE"


def test_offline_last_seen_exactly_at_threshold_returns_offline():
    """last_seen = 恰好 24h 前 (delta == threshold), 不超过 → OFFLINE。"""
    last_seen = _now() - timedelta(hours=24)
    assert determine_online_state(online=False, last_seen=last_seen, now=_now()) == "OFFLINE"


# --------------------------------------------------------------------------- #
# LONG_OFFLINE 态
# --------------------------------------------------------------------------- #


def test_long_offline_just_over_threshold():
    """last_seen = 24h01m 前,超过 24h 阈值 1 分钟 → LONG_OFFLINE。"""
    last_seen = _now() - timedelta(hours=24, minutes=1)
    assert (
        determine_online_state(online=False, last_seen=last_seen, now=_now()) == "LONG_OFFLINE"
    )


def test_long_offline_far_past():
    """last_seen = 48h 前 → LONG_OFFLINE。"""
    last_seen = _now() - timedelta(hours=48)
    assert (
        determine_online_state(online=False, last_seen=last_seen, now=_now()) == "LONG_OFFLINE"
    )


# --------------------------------------------------------------------------- #
# 自定义阈值
# --------------------------------------------------------------------------- #


def test_custom_threshold_12h():
    """自定义 long_offline_hours=12 时,13h 前下线 → LONG_OFFLINE。"""
    last_seen = _now() - timedelta(hours=13)
    assert (
        determine_online_state(
            online=False, last_seen=last_seen, now=_now(), long_offline_hours=12
        )
        == "LONG_OFFLINE"
    )


def test_custom_threshold_12h_within():
    """自定义 long_offline_hours=12 时,11h 前下线 → OFFLINE。"""
    last_seen = _now() - timedelta(hours=11)
    assert (
        determine_online_state(
            online=False, last_seen=last_seen, now=_now(), long_offline_hours=12
        )
        == "OFFLINE"
    )


# --------------------------------------------------------------------------- #
# 参数化覆盖
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("online", "delta_hours", "expected"),
    [
        (True, None, "ONLINE"),
        (False, None, "OFFLINE"),      # no last_seen
        (False, 0.5, "OFFLINE"),       # 30min ago
        (False, 23.9, "OFFLINE"),      # just under 24h
        (False, 24.0, "OFFLINE"),      # exactly 24h (boundary, not exceeded)
        (False, 24.1, "LONG_OFFLINE"), # 24h + 6min
        (False, 72.0, "LONG_OFFLINE"), # 3 days
    ],
)
def test_parametrize_states(
    online: bool,
    delta_hours: float | None,
    expected: str,
) -> None:
    now = _now()
    if delta_hours is None:
        last_seen = None
    else:
        last_seen = now - timedelta(hours=delta_hours)
    result = determine_online_state(online=online, last_seen=last_seen, now=now)
    assert result == expected
