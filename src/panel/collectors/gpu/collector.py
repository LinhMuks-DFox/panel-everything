"""GpuCollector — SSH + nvidia-smi 多卡指标采集器 (ARCH-002 / TASK-013).

通过 asyncssh 并发 SSH 到 servers 表中 has_gpu=True 的机器,执行
`nvidia-smi --query-gpu=... --format=csv,noheader,nounits`,解析多卡 CSV 输出,
将每卡的 GpuSample 写入 gpu_metrics 专用表(GpuRepository),并为每台机产出一条
汇总 MetricSample(metric="gpu_any_running")交框架写通用 latest_snapshot /
metric_history。

设计要点(遵循 ARCH-001 Collector 协议与降级语义):

  - 各机并发:`asyncio.gather(..., return_exceptions=True)`,单台失败完全隔离,
    不影响其它机,也不向 collect() 外抛(整体永不塌)。
  - 单台异常分类(见 _collect_one):
      连接失败(asyncssh.Error / OSError)/超时(TimeoutError) → status='unreachable'
      nvidia-smi exit_status≠0 / 输出为空 / 解析失败            → status='error'
    非 'ok' 时所有数值字段置 None。
  - SSH 执行层(asyncssh.connect)经实例属性注入,测试可替换为 mock,单测不连真实机。
  - known_hosts=None 为内网 Tailscale 隔离下首期假设(见连接处注释)。

凭证保护:私钥以路径(server.ssh_key_path)形式由 asyncssh 读取;本类不持有也不
记录私钥内容,日志只写主机/卡数/状态。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import asyncssh

from panel.collectors.base import MetricSample
from panel.db.gpu_repository import GpuSample

if TYPE_CHECKING:
    from panel.db.gpu_repository import GpuRepository, ServerRow
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)

# nvidia-smi 查询命令(字段顺序与 _parse_nvidia_smi_csv 解析顺序严格对应):
#   index, name, utilization.gpu, memory.used, memory.total, temperature.gpu, power.draw
NVIDIA_SMI_CMD = (
    "nvidia-smi "
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw "
    "--format=csv,noheader,nounits"
)

# CSV 每行期望的字段数(index,name,util,mem_used,mem_total,temp,power)。
_EXPECTED_FIELDS = 7
# 表示"无数值"的占位串(nvidia-smi 对不支持的指标输出 [Not Supported] / [N/A])。
_NULL_TOKENS = frozenset({"[not supported]", "[n/a]", "n/a", "", "[unknown error]"})


# --------------------------------------------------------------------------- #
# SSH 执行抽象 — 便于测试注入(不连真实主机)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SshResult:
    """单台 SSH 命令执行结果(执行层的归一返回)。"""

    stdout: str
    exit_status: int


class SshRunner(Protocol):
    """SSH 执行层协议:对单台服务器跑一条命令,返回 stdout + exit_status。

    约定:连接/认证/超时类故障由实现以异常形式抛出(asyncssh.Error / OSError /
    TimeoutError),由 _collect_one 捕获分类。命令成功执行但返回非零退出码时,
    通过 SshResult.exit_status 表达(不抛异常)。
    """

    async def run(
        self, server: ServerRow, command: str, timeout_seconds: float
    ) -> SshResult: ...


class AsyncSshRunner:
    """基于 asyncssh 的默认 SSH 执行层。"""

    async def run(
        self, server: ServerRow, command: str, timeout_seconds: float
    ) -> SshResult:
        """连接 server 并执行 command;返回 stdout 与退出码。

        连接参数遵循 ARCH-002 / TASK-013 技术规格。
        """
        # known_hosts=None:ARCH-001 裁定——内网 Tailscale 隔离下可接受;
        # P3 增强强校验(改为加载 known_hosts 并校验主机指纹)。
        async with asyncssh.connect(
            host=server.ssh_host,
            port=server.ssh_port,
            username=server.ssh_user,
            client_keys=[server.ssh_key_path] if server.ssh_key_path else None,
            known_hosts=None,
            connect_timeout=timeout_seconds,
        ) as conn:
            result = await conn.run(command, check=False, timeout=timeout_seconds)
        stdout = result.stdout if isinstance(result.stdout, str) else str(result.stdout or "")
        exit_status = result.exit_status if result.exit_status is not None else 0
        return SshResult(stdout=stdout, exit_status=exit_status)


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #


def _to_float(token: str) -> float | None:
    """安全转换单字段为 float;占位串/非数字返回 None。"""
    t = token.strip()
    if t.lower() in _NULL_TOKENS:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_nvidia_smi_csv(
    server_id: int, output: str, now: datetime
) -> list[GpuSample]:
    """解析 nvidia-smi CSV 输出为 GpuSample 列表。

    每张卡一行,字段顺序:index, name, util, mem_used, mem_total, temp, power。
    空行跳过;字段数不符或 index 非整数的行 → 该行 status='error'(数值置 None)。
    数值字段单独转换失败(如 [Not Supported])置 None,但不影响整行 status='ok'。

    Args:
        server_id: 该机的 servers.id。
        output: nvidia-smi 原始 stdout。
        now: 本轮采集时刻(UTC),所有行共用。

    Returns:
        GpuSample 列表(可能为空,代表无可识别行,调用方据此判 error)。
    """
    samples: list[GpuSample] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != _EXPECTED_FIELDS:
            # 字段数不符:该行无法可靠解析,标 error(尽量保留 index)。
            idx = _to_float(parts[0]) if parts else None
            samples.append(
                _error_sample(server_id, int(idx) if idx is not None else 0, now)
            )
            continue

        idx_raw = _to_float(parts[0])
        if idx_raw is None:
            # index 列必须可解析为整数;否则该行 error。
            samples.append(_error_sample(server_id, 0, now))
            continue

        gpu_name = parts[1] or None
        samples.append(
            GpuSample(
                server_id=server_id,
                gpu_index=int(idx_raw),
                gpu_name=gpu_name,
                util_pct=_to_float(parts[2]),
                mem_used_mib=_to_float(parts[3]),
                mem_total_mib=_to_float(parts[4]),
                temp_c=_to_float(parts[5]),
                power_w=_to_float(parts[6]),
                status="ok",
                collected_at=now,
            )
        )
    return samples


def _error_sample(server_id: int, gpu_index: int, now: datetime) -> GpuSample:
    """构造一条数值全 None 的 error GpuSample。"""
    return GpuSample(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name=None,
        util_pct=None,
        mem_used_mib=None,
        mem_total_mib=None,
        temp_c=None,
        power_w=None,
        status="error",
        collected_at=now,
    )


def _status_sample(
    server_id: int, status: str, now: datetime, gpu_index: int = 0
) -> GpuSample:
    """构造一条数值全 None 的 unreachable/error 占位 GpuSample(整机级)。"""
    return GpuSample(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name=None,
        util_pct=None,
        mem_used_mib=None,
        mem_total_mib=None,
        temp_c=None,
        power_w=None,
        status=status,  # type: ignore[arg-type]
        collected_at=now,
    )


# --------------------------------------------------------------------------- #
# GpuCollector
# --------------------------------------------------------------------------- #


@dataclass
class GpuCollector:
    """SSH GPU 采集器。满足 ARCH-001 Collector 协议。

    构造参数(由 register() 工厂注入,测试可替换):
        gpu_repo:  写 gpu_metrics 专用表 + 读 servers。
        base_repo: 保留以备读取(汇总 MetricSample 由框架经返回值写通用表)。
        ssh_runner: SSH 执行层(默认 AsyncSshRunner;测试注入 mock)。
    """

    gpu_repo: GpuRepository
    base_repo: Repository
    ssh_runner: SshRunner = field(default_factory=AsyncSshRunner)
    name: str = "gpu"
    interval_seconds: int = 60
    timeout_seconds: int = 30  # 单台 SSH+命令 超时

    async def collect(self) -> list[MetricSample]:
        """采集一轮所有 has_gpu=True 服务器的 GPU 指标。

        流程:
          1. 读 servers 表,过滤 has_gpu=True。无则返回空列表(不报错)。
          2. asyncio.gather 并发各机 _collect_one(return_exceptions=True)。
          3. 汇总所有机的 GpuSample → append_gpu_metrics 一次性写库。
          4. 为每台机产出一条 metric="gpu_any_running" 的汇总 MetricSample,
             交框架写通用 latest_snapshot / metric_history。

        Returns:
            汇总 MetricSample 列表(每台 GPU 机一条)。
        """
        servers = await self.gpu_repo.get_all_servers()
        gpu_servers = [s for s in servers if s.has_gpu]
        if not gpu_servers:
            logger.debug("gpu: no servers with has_gpu=True; nothing to collect")
            return []

        now = datetime.now(UTC)
        results = await asyncio.gather(
            *(self._collect_one(s) for s in gpu_servers),
            return_exceptions=True,
        )

        all_samples: list[GpuSample] = []
        metric_samples: list[MetricSample] = []
        for server, result in zip(gpu_servers, results, strict=True):
            if isinstance(result, BaseException):
                # _collect_one 已尽量内部消化异常;gather 仍兜底捕获意外异常。
                logger.warning(
                    "gpu: unexpected error collecting %s: %s",
                    server.name,
                    result,
                )
                server_samples = [_status_sample(server.id, "error", now)]
            else:
                server_samples = result
            all_samples.extend(server_samples)
            metric_samples.append(self._summarize(server, server_samples, now))

        # 写专用表(批量,append-only)。写库异常向上抛 → 框架降级为 error。
        await self.gpu_repo.append_gpu_metrics(all_samples)

        return metric_samples

    async def _collect_one(self, server: ServerRow) -> list[GpuSample]:
        """单台 SSH 采集,返回该机所有卡的 GpuSample(异常已分类,不抛)。

        异常分类(ARCH-002 / TASK-013):
          - asyncssh.Error / OSError / TimeoutError → status='unreachable'
          - nvidia-smi exit_status ≠ 0              → status='error'
          - 输出无可识别行 / 解析失败               → status='error'
        """
        now = datetime.now(UTC)

        # TODO(MS-003/TASK-016): 若 VM 处于 deallocated/stopped 状态,跳过 SSH 采集,
        # 直接产出 status='unreachable'(value_text='vm_not_running'),避免连接超时堆积。
        # if not await self._is_vm_running(server.id):
        #     return [_status_sample(server.id, "unreachable", now)]

        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self.ssh_runner.run(
                    server, NVIDIA_SMI_CMD, float(self.timeout_seconds)
                )
        except (TimeoutError, asyncssh.Error, OSError) as exc:
            # 连接/认证/网络故障或超时:该机不可达。
            logger.warning(
                "gpu: %s unreachable (%s)", server.name, type(exc).__name__
            )
            return [_status_sample(server.id, "unreachable", now)]

        if result.exit_status != 0:
            # nvidia-smi 返回非零(无 GPU / 驱动异常 / 命令缺失)。
            logger.warning(
                "gpu: %s nvidia-smi exited %d", server.name, result.exit_status
            )
            return [_status_sample(server.id, "error", now)]

        samples = _parse_nvidia_smi_csv(server.id, result.stdout, now)
        if not samples:
            # 退出码 0 但无可识别行:仍视为 error(无法确认 GPU)。
            logger.warning("gpu: %s produced no parseable GPU rows", server.name)
            return [_status_sample(server.id, "error", now)]
        return samples

    @staticmethod
    def _summarize(
        server: ServerRow, samples: list[GpuSample], now: datetime
    ) -> MetricSample:
        """把单台机的多卡 GpuSample 汇总为一条通用 MetricSample。

        metric='gpu_any_running':value_num=1.0(有任一卡 ok)/0.0;
        value_text='{ok}/{total} gpus ok';status='ok'(有 ok 卡)否则该机的
        主导失败态(unreachable 优先,否则 error)。
        """
        total = len(samples)
        ok_count = sum(1 for s in samples if s.status == "ok")
        if ok_count > 0:
            status: str = "ok"
        elif any(s.status == "unreachable" for s in samples):
            status = "unreachable"
        else:
            status = "error"
        return MetricSample(
            target_id=server.id,
            metric="gpu_any_running",
            value_num=1.0 if ok_count > 0 else 0.0,
            value_text=f"{ok_count}/{total} gpus ok",
            status=status,  # type: ignore[arg-type]
            collected_at=now,
        )
