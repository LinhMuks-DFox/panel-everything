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
