"""Collector registry: global name-keyed registration (ARCH-001 / TASK-003).

模块采集器(azure/gpu/tailscale)在各自工厂函数内构造 Collector 实例并调用
`register(...)`。`build_scheduler` 读取本注册表为每个 collector 装配一个 job,
模块因此无需触碰调度与降级逻辑。

注册表是进程级全局字典(单进程、单 event loop 部署,见 ARCH-001)。`clear()`
仅供测试在用例间复位。
"""

from __future__ import annotations

from panel.collectors.base import Collector

# 进程级注册表:collector.name -> Collector 实例。
_REGISTRY: dict[str, Collector] = {}


def register(collector: Collector) -> None:
    """按 collector.name 注册一个采集器。

    Args:
        collector: 满足 Collector 协议的实例(具备 name/interval_seconds/
            timeout_seconds 与 async collect())。

    Raises:
        ValueError: 已存在同名 collector(name 必须全局唯一)。
    """
    name = collector.name
    if name in _REGISTRY:
        raise ValueError(f"collector already registered: {name!r}")
    _REGISTRY[name] = collector


def get(name: str) -> Collector:
    """返回已注册的 collector。

    Raises:
        KeyError: 无此 name 的 collector。
    """
    return _REGISTRY[name]


def iter_collectors() -> list[Collector]:
    """返回当前已注册的全部 collector(按注册顺序)。"""
    return list(_REGISTRY.values())


def clear() -> None:
    """清空注册表。仅供测试在用例间复位。"""
    _REGISTRY.clear()
