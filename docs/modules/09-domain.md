# 模块文档：domain（领域 / 响应模型）

> 维护者参考。读完本文即可理解并扩展 `domain` 模块，无需通读全部源码。
> 关联：ARCH-001（响应白名单契约）、ARCH-002（Azure/GPU 模型）、ARCH-003（Tailscale 模型）、ARCH-004（AI 额度摄取与展示模型）；实现卡 TASK-005（`PublicModel` 白名单基线，与 config 同卡交付）。

---

## 1. 模块概述与职责

`domain` 是 Panel Everything 的**领域 / 响应模型层**，单文件实现：`src/panel/domain/models.py`。它解决两件事：

1. **对外 JSON 的安全白名单**：提供基类 `PublicModel`（`models.py:34`）。所有由 API 序列化返回给浏览器/客户端的响应模型**必须**继承它。它通过 `ConfigDict(extra="forbid")` 拒绝未声明字段，并在模块 docstring 里定义了一套**凭证命名禁忌**（`*secret*`/`*token*`/`*key*`/`*password*`/`private_*`/`ssh_key_path`），从源头上保证凭证字段绝不会被声明进对外模型，因此也绝不会被序列化出去。
2. **跨模块的数据契约（DTO）**：把各采集器/仓库产出的原始行（dataclass / SQL row）映射成结构稳定、字段语义明确的 Pydantic 模型，供 API 端点的 `response_model`、SSR 模板渲染、以及入站请求体校验使用。模型按 ARCH 分区组织（ARCH-002 Azure/GPU、ARCH-003 Tailscale、ARCH-004 AI 额度）。

它是凭证三层防御中的**响应层**（另两层在 `config` 模块：配置层「凭证只存路径」+ 日志层 `scrub`）。本模块**几乎无项目内依赖**（仅 `pydantic` + 标准库 `datetime`/`typing`），是纯被依赖方——API 层、Web 层、仓库层都 import 它，它不 import 任何业务模块，因此可独立演进、无循环依赖风险。

### 入站 vs 出站：两种基类，刻意区分

| 方向 | 基类 | 配置 | 理由 |
|------|------|------|------|
| **出站**（响应给客户端） | `PublicModel` | `extra="forbid"` + 凭证命名禁忌 | 白名单：只暴露安全字段，拒绝额外字段 |
| **入站**（客户端/Reporter 推送、写库请求体） | 裸 `BaseModel` | 默认（不 forbid） | 写入方向允许携带凭证字段（如 `ServerIn.ssh_key_path`）与灵活字段，由消费方决定如何落库 |

这条「出站继承 `PublicModel`、入站用 `BaseModel`」是本模块最核心的约定，理解它就理解了为什么 `ServerIn` 不继承 `PublicModel` 而 `ServerOut` 继承。

---

## 2. 文件与关键符号清单

模块根目录：`src/panel/domain/`

| 文件 | 符号 | 职责 |
|------|------|------|
| `__init__.py` | 包 docstring | 仅声明「领域层：Pydantic 领域/响应模型」，无 re-export（`src/panel/domain/__init__.py:1`）。导入一律走 `from panel.domain.models import ...`。 |
| `models.py` | 模块 docstring | 写明 `PublicModel` 三条约束 + 凭证命名禁忌表 + 「DB row→响应禁用 `**row_dict`」规则（`models.py:1`–`models.py:24`）。**改本模块前必读。** |
| `models.py` | `PublicModel` | 所有出站响应模型的白名单基类，`ConfigDict(extra="forbid")`（`models.py:34`）。 |
| `models.py` | `ServerIn` | 服务器注册**请求体**（入站，裸 `BaseModel`），含 `ssh_key_path` 路径字段（`models.py:59`）。 |
| `models.py` | `ServerOut` | 服务器信息**响应体**（出站），`from_attributes=True`，**故意不声明 `ssh_key_path`**（`models.py:76`）。 |
| `models.py` | `VmStatusOut` | VM 电源态响应体，dashboard 聚合用（`models.py:98`）。 |
| `models.py` | `GpuMetricOut` | 单张 GPU 卡最新指标响应体（`models.py:112`）。 |
| `models.py` | `CollectorStatusOut` | 单个 collector 最近运行状态（Azure/GPU 域，`status` 为自由 `str`）（`models.py:128`）。 |
| `models.py` | `GpuHistoryPointOut` | GPU 趋势数据点（某时间桶的聚合，TASK-016）（`models.py:139`）。 |
| `models.py` | `DashboardVmOut` | 继承 `VmStatusOut`，内嵌 `gpus: list[GpuMetricOut]`（`models.py:155`）。 |
| `models.py` | `DashboardAzureOut` | Azure/GPU dashboard 聚合顶层响应体（`models.py:161`）。 |
| `models.py` | `NodeResponse` | 单个 Tailscale 节点响应体，**故意不含 `node_key`**（`models.py:174`）。 |
| `models.py` | `CollectorStatusResponse` | Tailscale 采集器运行状态（`status` 为 `Literal`，区别于 `CollectorStatusOut`）（`models.py:192`）。 |
| `models.py` | `RefreshResponse` | 手动触发采集的响应体（`models.py:202`）。 |
| `models.py` | `AiMetricItem` | AI 用量单条指标采样（入站，`AiUsagePayload` 的元素）（`models.py:214`）。 |
| `models.py` | `AiUsagePayload` | `POST /api/ingest/ai-usage` 的入站请求体（`models.py:227`）。 |
| `models.py` | `AiProviderStatus` | 单个 AI provider 最新用量状态（出站，供 `_ai_card.html` 渲染一张卡）（`models.py:249`）。 |
| `models.py` | `AiUsageResponse` | `GET /api/ai-usage` 聚合响应体（出站）（`models.py:274`）。 |

---

## 3. 关键数据结构 / 契约

### 3.1 `PublicModel` 白名单契约（`models.py:34`）

```python
class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

约束（写在模块 docstring，`models.py:1`–`models.py:24`）：

1. **`extra="forbid"`**：构造时传入未声明字段会抛 `ValidationError`，而不是被静默忽略（与 config 的 `Settings`「`extra="ignore"`」形成刻意对照——配置宽松、响应严格）。
2. **凭证命名禁忌**：子类**不得**声明匹配下表模式的字段名（它们带凭证语义）：

   | 模式 | 示例 |
   |------|------|
   | `*secret*` | `azure_client_secret`、`secret_value` |
   | `*token*` | `access_token`、`bearer_token` |
   | `*key*` | `api_key`、`private_key`、`ssh_key_path` |
   | `*password*` | `db_password`、`root_password` |
   | `private_*` | `private_ip`（有争议，建议显式 allow-list） |
   | `ssh_key_path` | 精确名（SSH 私钥路径） |

   > **重要：这条命名禁忌是「文档约定 + 人/评审纪律」，不是运行时强制。** `PublicModel` 只在运行时强制 `extra="forbid"`；它**不会**因为你声明了一个名为 `api_key` 的字段而报错。防线靠：① 不声明这类字段；② 显式字段映射（见 3.2）；③ 测试断言（见第 8 节）。要暴露「凭证文件的名字而非内容」时，改名为安全别名（如 `azure_secret_configured: bool`），见 `models.py:18`–`models.py:20`。
3. **禁止 `**row_dict` 构造**：DB row → 响应模型**必须显式逐字段映射**（`models.py:22`–`models.py:23`）。原因：`**row_dict` 会把 row 里可能存在的凭证列（如 `ssh_key_path`）一并塞入构造参数；虽然 `extra="forbid"` 会因此报错（若模型没声明该字段），但依赖报错兜底是脆弱的——显式映射才是契约。`ServerOut` 的转换函数 `_row_to_out`（`src/panel/api/azure.py:110`）即逐字段列出，自然漏掉 `ssh_key_path`。

### 3.2 出站模型一览（继承 `PublicModel`）

| 模型 | 关键字段 | 凭证保护策略 | 备注 |
|------|----------|--------------|------|
| `ServerOut` | `id/name/azure_*/ssh_host/ssh_port/ssh_user/has_gpu/notes/created_at/updated_at` | **故意省略 `ssh_key_path`**（`models.py:91` 注释标注） | 额外 `from_attributes=True`，可由 dataclass 属性读取；但实际转换仍走显式映射 `_row_to_out` |
| `VmStatusOut` | `server_id/name/azure_vm_name/azure_resource_group/power_state/power_state_raw/is_running/collected_at/is_stale` | 无凭证字段 | `is_stale` 由 API 层据 `collected_at` 派生 |
| `GpuMetricOut` | `server_id/gpu_index/gpu_name/util_pct/mem_*/temp_c/power_w/collected_at/is_stale` | 无凭证字段 | 数值字段全 `float | None`（采集失败/缺测时为 None） |
| `CollectorStatusOut` | `status:str`、`last_ran_at:datetime|None`、`error:str|None` | `error` 已由 scheduler 层脱敏后落库（`azure.py:78`） | Azure/GPU 域；`status` 是自由 str（`"up"/"down"/"error"/"unknown"`） |
| `GpuHistoryPointOut` | `bucket_start/avg_util_pct/avg_mem_pct/max_temp_c/max_power_w/sample_count` | 无凭证字段 | raw 粒度：bucket=原始时刻、avg/max 为单点值、`sample_count=1`；5m/1h 粒度：降采样聚合值（`models.py:139`–`models.py:152`） |
| `DashboardVmOut` | 继承 `VmStatusOut` + `gpus: list[GpuMetricOut] = []` | 继承 | 一台 VM 内嵌其各卡指标 |
| `DashboardAzureOut` | `fetched_at`、`collector_status: dict[str, CollectorStatusOut]`、`vms: list[DashboardVmOut]` | 嵌套模型均为出站 | dashboard 顶层响应体 |
| `NodeResponse` | `id/hostname/dns_name/tailscale_ips/os/online_state/is_exit_node/last_seen/is_stale/updated_at` | **故意省略 `node_key`**（`models.py:177` 注释） | `online_state` 为 `Literal["ONLINE","OFFLINE","LONG_OFFLINE"]`；`last_seen` 在线时为 None |
| `CollectorStatusResponse` | `status:Literal["up","down","error","never_run"]`、`ran_at/sample_count/duration_ms/error` | `error` 脱敏后；None=无错误 | Tailscale 域；`status` 为 `Literal`（与 `CollectorStatusOut` 的自由 str 不同——**两个相似但不可互换的类型**） |
| `RefreshResponse` | `triggered:bool`、`message:str` | 无 | 手动触发采集结果 |
| `AiProviderStatus` | `provider/display_name/source_type/used_percent/used_value/limit_value/metric_unit/resets_at/window_label/stale/stale_since/stale_age_label/collected_at/status` | 无凭证字段 | API 层统一聚合后的展示模型（见 4.2） |
| `AiUsageResponse` | `providers: list[AiProviderStatus]`、`last_updated: str|None` | 嵌套出站 | `GET /api/ai-usage` 顶层 |

`AiProviderStatus` 字段语义要点（`models.py:249`–`models.py:271`）：
- `used_value`/`limit_value`：API 层从 `used_requests/used_tokens` 与 `limit_requests/limit_tokens` 中**取到哪个用哪个**统一而来；`metric_unit`（`'requests'|'tokens'|'unknown'`）标明单位。
- `stale`：**读时派生**——数据超过 `window_seconds*0.5` 或上报 `status='error'` 即为 stale。`stale_since`/`stale_age_label` 仅在 `stale=True` 时填（后端预算好的人类标签，如 `'2h 15m'`）。
- `status`：`'ok'|'error'|'no_data'`；`no_data` 表示 provider 配置存在但从未收到上报，此时 `collected_at` 为 None。
- 所有时间字段（`resets_at/stale_since/collected_at`）是 **ISO8601 字符串**而非 `datetime`——因为模板直接消费、且这些值来自聚合预算，不需要再被 Pydantic 重新校验为 datetime。

### 3.3 入站模型一览（裸 `BaseModel`，可含凭证 / 灵活字段）

| 模型 | 字段 | 为什么不继承 `PublicModel` |
|------|------|---------------------------|
| `ServerIn`（`models.py:59`） | `name`、`azure_resource_group?`、`azure_vm_name?`、`ssh_host?`、`ssh_port=22`、`ssh_user="azureuser"`、`ssh_key_path?`、`has_gpu=False`、`notes?` | 写入模型**允许含凭证字段**：`ssh_key_path` 存的是**路径引用**（非私钥内容），仅写入 DB，绝不出现在响应中（`models.py:62`、`models.py:71`） |
| `AiMetricItem`（`models.py:214`） | `metric:str`、`value_num:float|None`、`value_text:float|None` | 入站元素；数值走 `value_num`、文本/枚举走 `value_text`，二者按需填写。`metric` 取值示例：`used_requests`/`limit_requests`/`used_percent`/`resets_at`/`window_seconds`/`extra` |
| `AiUsagePayload`（`models.py:227`） | `reporter_version:str`、`reported_at:datetime`、`provider:str`、`metrics:list[AiMetricItem]`、`status:Literal["ok","error"]="ok"` | 入站请求体。`provider` 用 `str` 而非 `Literal`：**未知 provider 不在 Pydantic 层 422 拒绝**，而是交给端点查 `ai_provider` 表，未命中时返回 `400 {"ok": False, "error": ...}`（与 TASK-030 测试一致，见 `api/ingest.py:54`–`api/ingest.py:59`） |

---

## 4. 对外接口与调用关系

本模块不主动调用任何模块；它被四类消费方使用。

### 4.1 谁导入了哪些模型

| 消费方 | 导入的模型 | 用途 | 引用 |
|--------|-----------|------|------|
| `api/azure.py` | `CollectorStatusOut`、`DashboardAzureOut`、`DashboardVmOut`、`GpuHistoryPointOut`、`GpuMetricOut`、`ServerIn`、`ServerOut` | servers CRUD + dashboard + GPU history 端点的请求/`response_model` | `src/panel/api/azure.py:26` |
| `api/tailscale/routes.py` | `CollectorStatusResponse`、`NodeResponse`、`RefreshResponse` | Tailscale 节点/状态/刷新端点 | `src/panel/api/tailscale/routes.py:22` |
| `api/ai_usage.py` | `AiProviderStatus`、`AiUsageResponse` | `GET /api/ai-usage` 聚合 | `src/panel/api/ai_usage.py:24` |
| `api/ingest.py` | `AiUsagePayload` | `POST /api/ingest/ai-usage` 请求体校验 | `src/panel/api/ingest.py:20` |
| `web/routes.py` | `ServerIn` | SSR `/servers` 表单提交构造 `ServerIn` 后写库（`web/routes.py:456`） | `src/panel/web/routes.py:25` |
| `db/gpu_repository.py` | `ServerIn` | `insert_server(data: ServerIn)` 写库参数类型（`gpu_repository.py:171`） | `src/panel/db/gpu_repository.py:24` |
| `tests/*` | 多数模型 | 构造/断言（含凭证白名单断言） | 见第 8 节 |

### 4.2 数据流（DB row / 采集结果 → 出站模型）

```
采集器/仓库产出原始 row(dataclass: ServerRow / GpuMetricRow / GpuBucketRow /
  CollectorRunRow / SnapshotRow / TailscaleNodeRow)
        │  显式逐字段映射(禁用 **row_dict)
        ▼
domain 出站模型(PublicModel 子类)
        │  FastAPI response_model 序列化  /  Jinja 模板属性访问
        ▼
浏览器 JSON  /  SSR HTML
```

关键映射函数（都在 `api/azure.py`，是「显式映射」纪律的范本）：
- `_row_to_out(row) -> ServerOut`（`azure.py:110`）：逐字段列出，自然漏掉 `ssh_key_path`。
- `_build_collector_status(...) -> dict[str, CollectorStatusOut]`（`azure.py:60`）：从未运行的 collector → `status="unknown"`；`error` 直接透传（已由 scheduler 脱敏）。
- `_build_gpu_outs(...) -> list[GpuMetricOut]`（`azure.py:83`）：在此计算 `is_stale = now - collected_at > GPU_STALE_SECONDS`。`is_stale` 不是 DB 列，是**响应层派生**。
- `_raw_to_history_point` / `_bucket_to_history_point -> GpuHistoryPointOut`（`azure.py:329`、`azure.py:345`）：raw 粒度与降采样桶分别映射成同一趋势点模型。

AI 额度的聚合（`api/ai_usage.py`）：`get_ai_usage_data(repo)`（`ai_usage.py:176`）从 `latest_snapshot`(collector=`'ai_usage'`) 读各 provider 原始指标，与 `ai_provider` 表静态元数据合并，在 `_snapshot_to_status(...)`（`ai_usage.py:72`）里把 `used_requests/used_tokens/...` 统一成 `used_value+metric_unit`、算好 `stale`/标签，产出 `AiProviderStatus`。模板只消费这些**已算好的字段**，不做计算。该函数同时供 HTTP 端点与 SSR `index()` 复用（避免 HTTP 往返）。

### 4.3 入站校验流

```
ServerIn:  HTML 表单 / JSON body → FastAPI 用 ServerIn 校验 → gpu_repository.insert_server(data)
AiUsagePayload: Reporter POST → FastAPI 用 AiUsagePayload 校验(provider 不在此 422)
   → ingest 端点查 ai_provider 表(未知→400) → 展开成 MetricSample 落 latest_snapshot/metric_history
```

---

## 5. 与其他模块的依赖

**上游（domain 依赖谁）：** 仅 `pydantic`（`BaseModel`/`ConfigDict`）、标准库 `datetime`、`typing.Literal`。**无项目内依赖**——刻意为之，确保任何模块可安全导入而不产生循环。

**下游（谁依赖 domain）：**
- `api/azure.py`、`api/tailscale/routes.py`、`api/ai_usage.py`、`api/ingest.py`（response_model / 请求体）。
- `web/routes.py`（`ServerIn` 表单写库；模板把 `DashboardVmOut`/`AiProviderStatus`/`NodeResponse` 当 view-model 做属性访问，见 `web/routes.py:39`、`web/routes.py:133`）。
- `db/gpu_repository.py`（`insert_server` 参数类型）。
- `tests/`（构造与断言）。

**相邻协同（非 import 依赖，但必须一起理解）：**
- `config` 模块：凭证三层防御的另两层（配置层「凭证只存路径」+ 日志层 `scrub`）。`PublicModel` 是第三层。三者由 TASK-005 作为整体交付。
- 各仓库的 `*Row` dataclass（`db/repository.py`、`db/gpu_repository.py`）：是出站模型的**数据来源**，但不是 domain 模型本身。映射在 API 层完成，不在 domain 层。

---

## 6. 扩展点

### 6.1 新增一个出站响应模型（最常见）

1. 在 `models.py` 对应 ARCH 分区（用 `# --- ARCH-00x ---` 分隔注释）下新建 `class XxxOut(PublicModel):`。
2. **只声明安全字段**；逐字核对第 3.1 节凭证命名禁忌表，凡命中 `*secret*/*token*/*key*/*password*/private_*/ssh_key_path` 的字段一律不要声明。需暴露「凭证文件名/是否配置」时改安全别名（如 `xxx_configured: bool`）。
3. 嵌套的子模型也必须是 `PublicModel` 子类（如 `DashboardAzureOut` 内嵌 `CollectorStatusOut`/`DashboardVmOut`）。
4. 在 API 端点用 `response_model=XxxOut` 声明；写**显式逐字段映射函数**（仿 `_row_to_out`/`_build_gpu_outs`），**绝不 `XxxOut(**row_dict)`**。
5. 派生字段（如 `is_stale`、`used_value`、各 `*_label`）在 API 层算好再塞进模型——domain 模型只承载数据形状，不含业务计算。
6. 在 `tests/` 加：① 字段集合断言（响应 JSON 的 keys == 期望集合）；② 若涉敏感来源，加「绝不出现凭证字段名」的断言（仿 `tests/test_dashboard_azure.py:483` 的 ssh_key_path 断言）。

### 6.2 新增一个入站请求体模型

1. 新建 `class XxxIn(BaseModel):`（**裸 `BaseModel`，不要继承 `PublicModel`**）。入站可含凭证路径字段与灵活字段。
2. 凭证字段存**路径引用**而非明文（仿 `ServerIn.ssh_key_path`），并在消费端（仓库/端点）决定如何落库，确保它不被映射进任何出站模型。
3. 若某字段「Pydantic 层不应硬拒、而要交业务层校验」（如 `AiUsagePayload.provider` 用 `str` 不用 `Literal`），用宽松类型并在端点查表/返回 400，且在 docstring 写清原因 + 关联 TASK 测试要求。

### 6.3 给已有出站模型加字段

1. 直接在类里加字段。注意 `extra="forbid"` 是对**额外**字段的约束，加**新声明**字段不受其影响。
2. 同步更新对应的映射函数（API 层），否则构造时缺参会报错。
3. 同步前端模板/JSON 消费方，并更新对应 test 的字段集合断言。

### 6.4 区分 `CollectorStatusOut` 与 `CollectorStatusResponse`（避免误用）

两者都表示采集器状态，但**不可互换**：Azure/GPU 域用 `CollectorStatusOut`（`status` 为自由 `str`，字段 `last_ran_at/error`）；Tailscale 域用 `CollectorStatusResponse`（`status` 为 `Literal["up","down","error","never_run"]`，字段 `ran_at/sample_count/duration_ms/error`）。新增端点时按所属域选对类型，不要图省事复用错的那个。

### 6.5 与「新增采集器/迁移/前端卡」的边界

domain 模块只负责**模型形状**。新增一个采集器或 DB 表通常会牵出一个新出站模型（按 6.1 加）+ 一个新映射函数（在 API 层）+ 可能一个新入站模型（按 6.2 加）。DB 迁移、采集逻辑、前端卡片的扩展步骤分别在 db / collectors / web 模块文档里——本模块只在它们需要 DTO 时被触及。

---

## 7. 配置 / 环境变量

本模块**不读取任何配置/环境变量**，无 `PANEL_*` 依赖。它是纯数据模型，行为不随环境变化。

唯一的间接关联：出站模型里的 `is_stale` / `stale` 等派生字段，其阈值（如 `GPU_STALE_SECONDS`、`window_seconds*0.5`）由 API 层依据 `config.Settings`（`stale_threshold_seconds` 等）计算，**不在本模块**。domain 只接收算好的布尔值。

---

## 8. 测试位置与覆盖

| 测试文件 | 覆盖本模块的什么 |
|----------|------------------|
| `tests/test_config.py:269`–`tests/test_config.py:286` | **`PublicModel` 核心契约**：`_SampleResponse(PublicModel)` 用声明字段可构造（`test_public_model_*`）；传额外字段（含凭证名）抛 `ValidationError`（`extra="forbid"`）。与 config 同卡（TASK-005），故并入 `test_config.py`。 |
| `tests/test_servers_api.py:99`、`:206` | POST/GET 响应字段集合 == `ServerOut`（**不含 `ssh_key_path`**）。 |
| `tests/test_dashboard_azure.py:451` | `DashboardVmOut` 含全部规定字段（含内嵌 `gpus`）。 |
| `tests/test_dashboard_azure.py:483` | **凭证白名单回归**：`DashboardAzureOut` 响应里绝不能出现 `ssh_key_path`（即使 `ServerIn` 写库时带了）。 |
| `tests/test_gpu_downsample.py`、`tests/test_gpu_trend.py` | `GpuHistoryPointOut`（raw/5m/1h 各粒度的趋势点形状）、`CollectorStatusOut`（模板渲染）。 |
| `tests/test_frontend_vm_gpu.py:115` | 用 `CollectorStatusOut` 拼最小 `DashboardAzureOut`-like 命名空间渲染模板。 |
| `tests/test_ai_card.py:257` | `AiProviderStatus`-like view-model 模板渲染（属性访问）。 |
| `tests/test_azure_collector.py`、`test_gpu_collector.py`、`test_azure_public_ip.py`、`test_gpu_schema.py`、`test_gpu_dynamic_host.py`、`test_servers_api.py` | 大量用 `ServerIn(...)` 构造测试数据（默认值 `ssh_port=22`/`ssh_user="azureuser"`/`has_gpu=False` 等）。 |

跑相关测试：`pytest tests/test_config.py tests/test_servers_api.py tests/test_dashboard_azure.py -q`。

> 注：本模块**没有**独立的 `tests/test_domain.py`。`PublicModel` 行为在 `test_config.py`，各具体模型的形状/白名单在各 API 测试里随端点验证。新增模型时，优先在对应端点的测试文件加断言。

---

## 9. 注意事项 / 降级语义 / gotchas

- **凭证命名禁忌不是运行时强制**：`PublicModel` 运行时只 `extra="forbid"`，**不会**因为你声明了 `api_key`/`ssh_key_path` 这类名字而报错。禁忌靠人/评审/测试守。要害字段（`ServerOut` 漏 `ssh_key_path`、`NodeResponse` 漏 `node_key`）是**手工不声明 + 测试断言**双保险，改这两个类时务必保留对应回归断言（`tests/test_dashboard_azure.py:483`）。
- **绝不用 `**row_dict` 构造出站模型**（`models.py:22`）：DB row 可能含凭证列，`**` 展开会把它们塞进构造参数。即便 `extra="forbid"` 能兜底报错，也不要依赖；坚持显式逐字段映射（范本 `azure.py:110`）。
- **`CollectorStatusOut` ≠ `CollectorStatusResponse`**：名字相近、含义相近，但字段集与 `status` 类型不同（自由 str vs `Literal`），分属 Azure/GPU 与 Tailscale 两域，不可互换。导入时看清。
- **`ServerOut` 的 `from_attributes=True` 名实不一致**：模型声明了 `from_attributes=True`（`models.py:82`），看似能 `model_validate(dataclass)` 直读属性；但实际代码走的是**显式构造** `_row_to_out`（`azure.py:120`），并未用 `model_validate`。两种路径都能漏掉 `ssh_key_path`，但要清楚当前生效的是显式映射这条。
- **时间字段类型不统一是有意的**：Azure/GPU/Tailscale 域用 `datetime`（FastAPI 序列化为 ISO8601）；AI 额度域（`AiProviderStatus`）用 `str` 的 ISO8601。后者是因为这些值是聚合层预算好的标签/时间串，模板直接消费，不需要 Pydantic 再当 datetime 校验。新增 AI 字段时沿用 `str`，别混入 `datetime`。
- **`AiUsagePayload.provider` 故意是 `str` 不是 `Literal`**（`models.py:230`）：未知 provider 不在校验层 422，而由端点查 `ai_provider` 表返回 `400 {"ok": False, ...}`（TASK-030 测试要求）。若改成 `Literal` 会破坏该契约。
- **派生字段在 API 层算、不在 domain**：`is_stale`/`stale`/`used_value`/`*_label` 等都不是 DB 列，由 API 层据阈值算好再塞入模型。改阈值要去 API/config，改不到 domain。
- **`error` 字段已脱敏**：`CollectorStatusOut.error` / `CollectorStatusResponse.error` 透传的是 scheduler 层已 `scrub()` 过的字符串（`azure.py:78`）。domain 层**不**做脱敏，依赖上游已脱敏——新增承载 error 的字段时确认来源已脱敏。
- **e-ink / 树莓派约束（项目级）**：模型本身无前端，但字段设计服务于轻量 SSR——把计算与标签预算放在后端（如 `window_label`/`stale_age_label`），模板零计算，减少前端负担、利于 e-ink 等弱终端。新增展示模型时延续「后端算好、字段即结论」的风格。
- **`known_hosts=None`（相邻坑，不在本模块）**：GPU SSH 采集器用 `known_hosts=None`（等价 `StrictHostKeyChecking=no`），是 ARCH-001 内网 Tailscale 隔离前提下的首期裁定。本模块的 `ServerIn.ssh_key_path` 只提供私钥**路径**；主机校验策略由采集器决定，与 domain 无关——但维护者常把两者混在一起，记住边界。
- **入站 `setattr` 注入风险**：入站模型用裸 `BaseModel`，默认不 forbid 额外字段，且消费方若用 `setattr`/`**` 把请求体往别处灌，可能引入意外字段。规约是：入站模型显式声明字段、消费端显式取字段（如 ingest 端点逐项展开 `body.metrics`，`ingest.py:61`），不要把整个请求体对象往 row/响应里 `**` 展开。

---

## 10. 关联 REQ / ARCH / TASK

| 类型 | 编号 | 关系 |
|------|------|------|
| ARCH | ARCH-001 | 响应白名单基线契约：`domain/models.py` 提供白名单 Pydantic 基类 `PublicModel`，禁止序列化任何凭证字段（ARCH-001 §数据/响应、§凭证管理规范，`ARCH-001.md:309`、`ARCH-001.md:383`）。凭证三层防御中的「响应层」。 |
| ARCH | ARCH-002 | Azure/GPU 领域模型：`ServerIn/ServerOut/VmStatusOut/GpuMetricOut/CollectorStatusOut/GpuHistoryPointOut/DashboardVmOut/DashboardAzureOut`。 |
| ARCH | ARCH-003 | Tailscale 响应模型：`NodeResponse/CollectorStatusResponse/RefreshResponse`。 |
| ARCH | ARCH-004 | AI 额度摄取（入站 `AiMetricItem/AiUsagePayload`）与展示（出站 `AiProviderStatus/AiUsageResponse`）。 |
| TASK | TASK-005 | 本模块 `PublicModel` 白名单实现卡（与 config 配置/凭证/日志脱敏同卡，P0）。 |
| TASK | TASK-016 | `GpuHistoryPointOut`（GPU 降采样趋势点）。 |
| TASK | TASK-017 | 前端 GPU 趋势迷你图，消费 `GpuHistoryPointOut`。 |
| TASK | TASK-018 | Azure 动态公网 IP + SP 对齐，关联 `ServerIn/ServerOut`（`ssh_host` 等）。 |
| TASK | TASK-024 | 服务器注册表单 `/servers`，SSR 构造 `ServerIn` 写库。 |
| TASK | TASK-030 | AI 用量摄取端点，定义 `AiMetricItem/AiUsagePayload`（`provider` 用 str 的契约）。 |
| TASK | TASK-033 | 前端 AI 额度卡片，消费 `AiProviderStatus/AiUsageResponse`。 |
| REQ | REQ-002 | 服务器注册入口（`ServerIn/ServerOut`）。 |
