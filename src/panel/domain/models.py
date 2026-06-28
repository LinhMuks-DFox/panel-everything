"""Domain / response models: Pydantic white-list base class.

All JSON responses served by the API MUST use a model that inherits from
PublicModel.  PublicModel enforces:

  1. extra="forbid"  — extra fields are rejected, not silently ignored.
  2. Documented naming taboo: subclasses MUST NOT declare fields whose names
     match the following patterns (they carry credential semantics):

       Pattern             Examples
       *secret*            azure_client_secret, secret_value
       *token*             access_token, bearer_token
       *key*               api_key, private_key, ssh_key_path
       *password*          db_password, root_password
       *private_*          private_ip (debatable — prefer explicit allow-listing)
       ssh_key_path        exact name (SSH private-key path)

  If a module DOES need to surface a path reference (e.g. showing the *name*
  of a secret file without its content), create a renamed field with a safe
  alias (e.g. `azure_secret_configured: bool`).

  DB row → response model conversion MUST use explicit field mapping.
  Do NOT use `**row_dict` to construct a PublicModel subclass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PublicModel(BaseModel):
    """Base class for all outward-facing JSON response models.

    Constraints
    -----------
    * extra="forbid": prevents accidental inclusion of undeclared fields.
    * Subclasses declare only the fields that are safe to expose publicly.
    * Credential-named fields (see module docstring) are PROHIBITED in subclasses.

    Example::

        class HealthResponse(PublicModel):
            status: str
            db: str
            time: str
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# ARCH-002: Azure/GPU 领域模型
# --------------------------------------------------------------------------- #


class ServerIn(BaseModel):
    """服务器注册请求体.

    ssh_key_path 存路径引用,不存私钥内容。不继承 PublicModel(写入模型允许含凭证字段)。
    """

    name: str
    azure_resource_group: str | None = None
    azure_vm_name: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str = "azureuser"
    ssh_key_path: str | None = None   # 仅写入 DB,不出现在响应中
    has_gpu: bool = False
    notes: str | None = None


class ServerOut(PublicModel):
    """服务器信息响应体.

    ssh_key_path 故意缺失 — 白名单不回传凭证路径。
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    name: str
    azure_resource_group: str | None
    azure_vm_name: str | None
    ssh_host: str | None
    ssh_port: int
    ssh_user: str
    # ssh_key_path — 故意不列,符合 ARCH-001 凭证白名单规范
    has_gpu: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class VmStatusOut(PublicModel):
    """VM 电源态响应体(用于 dashboard 聚合)."""

    server_id: int
    name: str
    azure_vm_name: str | None
    azure_resource_group: str | None
    power_state: str
    power_state_raw: str | None
    is_running: bool
    collected_at: datetime
    is_stale: bool


class GpuMetricOut(PublicModel):
    """单张 GPU 卡最新指标响应体."""

    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    mem_pct: float | None
    temp_c: float | None
    power_w: float | None
    collected_at: datetime
    is_stale: bool


class CollectorStatusOut(PublicModel):
    """单个 collector 最近运行状态."""

    status: str            # "up" / "down" / "error" / "unknown"
    last_ran_at: datetime | None
    error: str | None


# === ARCH-002 GPU 趋势 ===


class GpuHistoryPointOut(PublicModel):
    """单张 GPU 卡某时间桶的趋势数据点 (TASK-016).

    供 GET /api/v1/gpu/{server_id}/{gpu_index}/history 返回，按 bucket_start
    升序排列。raw 粒度下 bucket_start 即原始采集时刻，avg_* 为单点原值、
    max_* 同值、sample_count=1；5m/1h 粒度下为降采样聚合值。
    """

    bucket_start: datetime
    avg_util_pct: float | None
    avg_mem_pct: float | None
    max_temp_c: float | None
    max_power_w: float | None
    sample_count: int


class DashboardVmOut(VmStatusOut):
    """Dashboard 中单台 VM 条目,内嵌该机各 GPU 卡最新指标."""

    gpus: list[GpuMetricOut] = []


class DashboardAzureOut(PublicModel):
    """Azure/GPU dashboard 聚合响应体."""

    fetched_at: datetime
    collector_status: dict[str, CollectorStatusOut]
    vms: list[DashboardVmOut]


# --------------------------------------------------------------------------- #
# ARCH-003: Tailscale 响应模型
# --------------------------------------------------------------------------- #


class NodeResponse(PublicModel):
    """单个 Tailscale 节点响应体.

    node_key 字段故意缺失 — 白名单不回传内部密钥字段。
    """

    id: int
    hostname: str
    dns_name: str | None
    tailscale_ips: list[str]
    os: str | None
    online_state: Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]
    is_exit_node: bool
    last_seen: datetime | None    # UTC; None when online
    is_stale: bool
    updated_at: datetime


class CollectorStatusResponse(PublicModel):
    """Tailscale 采集器最近运行状态响应体."""

    status: Literal["up", "down", "error", "never_run"]
    ran_at: datetime | None
    sample_count: int
    duration_ms: int
    error: str | None             # 脱敏后; None 表示无错误


class RefreshResponse(PublicModel):
    """手动触发采集的响应体."""

    triggered: bool
    message: str


# === ARCH-004 AI 额度摄取 ===
# 工作站 Reporter 推送 AI 用量数据的请求模型。请求体不继承 PublicModel
# （PublicModel 用于对外响应白名单；摄取为入站方向，允许灵活字段）。


class AiMetricItem(BaseModel):
    """单条 AI 用量指标采样（请求体内的元素）。

    metric 取值示例：'used_requests' | 'limit_requests' | 'used_percent'
    | 'resets_at' | 'window_seconds' | 'extra'。
    数值型走 value_num，文本/枚举型走 value_text，二者按需填写。
    """

    metric: str
    value_num: float | None = None
    value_text: str | None = None


class AiUsagePayload(BaseModel):
    """POST /api/ingest/ai-usage 的请求体（工作站 Reporter 推送）。

    provider 用 str（非 Literal）：未知 provider 不在 Pydantic 层 422 拒绝，而是
    交由端点查 ai_provider 表，未命中时返回 400 {"ok": False, "error": ...}，
    与 TASK-030 测试要求一致（已知 provider 名仍由 ai_provider 表约束）。
    """

    reporter_version: str
    reported_at: datetime
    provider: str
    metrics: list[AiMetricItem]
    status: Literal["ok", "error"] = "ok"


# === ARCH-004 AI 额度展示 ===
# 前端 AI 额度卡片(TASK-033)的对外响应模型。继承 PublicModel(白名单 +
# extra="forbid")。API 层(api/ai_usage.py)负责把 latest_snapshot 的原始
# 指标(used_requests/used_tokens/used_percent/resets_at/...)聚合并统一成
# used_value + metric_unit,模板只消费这些已算好的字段。


class AiProviderStatus(PublicModel):
    """单个 AI provider 的最新用量状态(供 _ai_card.html 渲染一张卡)。

    used_value / limit_value 由 API 层从 used_requests/used_tokens 与
    limit_requests/limit_tokens 统一而来;metric_unit 标明单位。
    stale 为读时派生:数据超过 window_seconds*0.5 或上报 status='error'。
    no_data:provider 配置存在但从未收到上报。
    """

    provider: str
    display_name: str
    source_type: str               # 'local_jsonl' | 'oauth_api' | 'manual'
    used_percent: float | None
    used_value: float | None       # used_requests 或 used_tokens(取到哪个用哪个)
    limit_value: float | None
    metric_unit: str               # 'requests' | 'tokens' | 'unknown'
    resets_at: str | None          # ISO8601 UTC
    window_label: str              # 如 '5h 窗口'
    # 次级(周)限额:Codex 上报 secondary_used_percent / secondary_resets_at。
    # 无次级窗口的 provider 两者均为 None,卡片侧据此条件渲染。
    secondary_used_percent: float | None = None
    secondary_resets_at: str | None = None
    stale: bool
    stale_since: str | None        # collected_at ISO8601,stale=True 时填
    stale_age_label: str | None    # 如 '2h 15m',stale=True 时填(后端预算)
    collected_at: str | None       # ISO8601 UTC,no_data 时为 None
    status: str                    # 'ok' | 'error' | 'no_data'


class AiUsageResponse(PublicModel):
    """GET /api/ai-usage 聚合响应体。"""

    providers: list[AiProviderStatus]
    last_updated: str | None       # 最新一条 collected_at;无数据则 None
