# 模块文档：config（配置与凭证）

> 维护者参考。读完本文即可理解并扩展 `config` 模块，无需通读全部源码。
> 关联：ARCH-001（基线契约）、TASK-005（本模块实现卡）。

---

## 1. 模块概述与职责

`config` 是 Panel Everything 的**配置与凭证安全基线层**。它解决三件事，全部围绕「凭证绝不外泄」这一 ARCH-001 红线：

1. **集中配置加载**：用 `pydantic-settings` 把所有运行参数从环境变量（统一 `PANEL_` 前缀）/ `.env` 文件加载成一个类型校验过的 `Settings` 对象，通过 `get_settings()` 单例分发给全应用。
2. **凭证按路径引用**：凭证（Azure client secret、SSH 私钥等）**只在配置里存文件路径，永不存明文**。运行时由 `read_secret()` 从只读挂载目录（`/secrets`）或绝对路径懒加载实际值，短暂持有后丢弃。
3. **日志脱敏**：`scrub()` + `_ScrubFilter` + `setup_logging()` 在 root logger 上安装过滤器，对每条日志的 token / secret / key / password / Bearer / PEM 私钥块 / 长 hex/base64 串自动打码为 `***`，防止凭证经异常栈、debug 日志或 `collector_run.error` 泄露。

该模块是所有其他模块的上游依赖：采集器、API、main 装配都从这里取配置和凭证。它本身**不依赖项目内任何其他模块**（仅依赖 `pydantic-settings` / 标准库），因此可独立测试与演进。

凭证安全是一个三层防御体系，`config` 负责其中两层（配置/凭证 + 日志脱敏），第三层（响应白名单 `PublicModel`）落在 `domain/models.py`，但 TASK-005 把三者作为一个交付整体，本文在第 4、9 节交代其协同关系。

---

## 2. 文件与关键符号清单

模块根目录：`src/panel/config/`

| 文件 | 符号 | 职责 |
|------|------|------|
| `__init__.py` | re-export | 对外统一导出 `Settings` / `get_settings` / `read_secret` / `scrub` / `setup_logging`（`__all__`），见 `src/panel/config/__init__.py:8`。 |
| `settings.py` | `Settings` | `BaseSettings` 子类，集中声明全部配置字段（`src/panel/config/settings.py:23`）。 |
| `settings.py` | `Settings.azure_configured` | 计算属性：四项 Azure SP 字段是否齐全（`settings.py:59`）。 |
| `settings.py` | `read_secret(name_or_path, settings=None)` | 从 `secrets_dir`/绝对路径读取凭证文件内容并 `strip()`（`settings.py:91`）。 |
| `settings.py` | `get_settings()` | `@lru_cache` 单例工厂，返回缓存的 `Settings`（`settings.py:115`）。 |
| `scrub.py` | `scrub(text)` | 纯函数：把字符串中的敏感子串替换为 `***`（`src/panel/config/scrub.py:64`）。 |
| `scrub.py` | `_ScrubFilter` | `logging.Filter` 子类，对每条 `LogRecord` 应用 `scrub()`（`scrub.py:102`）。 |
| `scrub.py` | `setup_logging(level="info")` | 配置 root logger（handler/formatter）、安装 `_ScrubFilter`、压低第三方 logger 级别（`scrub.py:119`）。 |
| `scrub.py` | `_KV_PATTERNS` / `_PEM_PATTERN` / `_HEX_PATTERN` / `_B64_PATTERN` | 模块加载期编译的脱敏正则集合（`scrub.py:31`–`scrub.py:59`）。 |

---

## 3. 关键数据结构 / 契约

### 3.1 `Settings`（pydantic-settings）

`model_config`（`settings.py:34`）：

```python
SettingsConfigDict(env_prefix="PANEL_", env_file=".env", extra="ignore")
```

- `env_prefix="PANEL_"`：环境变量名 = `PANEL_` + 字段名大写。例：`db_path` ← `PANEL_DB_PATH`。
- `env_file=".env"`：进程工作目录下的 `.env` 也会被加载（env 变量优先级高于 `.env`）。
- `extra="ignore"`：未声明的 `PANEL_*` 变量被静默忽略，不报错（与 `PublicModel` 的 `extra="forbid"` 形成对照——配置宽松、响应严格）。

**全部字段（含默认值与语义）：**

| 字段 | 类型 | 默认 | 环境变量 | 语义 |
|------|------|------|----------|------|
| `db_path` | `str` | `/data/panel.db` | `PANEL_DB_PATH` | SQLite 数据库文件路径（容器内）。 |
| `port` | `int` | `8080` | `PANEL_PORT` | uvicorn 监听端口。 |
| `log_level` | `str` | `info` | `PANEL_LOG_LEVEL` | 传给 `setup_logging()` 的级别字符串。 |
| `stale_threshold_seconds` | `int` | `180` | `PANEL_STALE_THRESHOLD_SECONDS` | 采集数据距今超过此秒数即视为陈旧（前端/SSR 用）。 |
| `secrets_dir` | `str` | `/secrets` | `PANEL_SECRETS_DIR` | 只读挂载的凭证目录；`read_secret` 的相对名在此解析。 |
| `azure_tenant_id` | `str` | `""` | `PANEL_AZURE_TENANT_ID` | Azure SP 租户 ID。 |
| `azure_client_id` | `str` | `""` | `PANEL_AZURE_CLIENT_ID` | Azure SP 应用 ID。 |
| `azure_client_secret_file` | `str` | `""` | `PANEL_AZURE_CLIENT_SECRET_FILE` | **路径**，指向存放 client secret 的文件（如 `/secrets/azure_client_secret`）。**不是 secret 本身。** |
| `azure_subscription_id` | `str` | `""` | `PANEL_AZURE_SUBSCRIPTION_ID` | Azure 订阅 ID。 |
| `tailscale_socket` | `str` | `/var/run/tailscale/tailscaled.sock` | `PANEL_TAILSCALE_SOCKET` | Tailscale localapi unix socket 路径。 |
| `ssh_key_path` | `str` | `""` | `PANEL_SSH_KEY_PATH` | **路径**，指向 SSH 私钥文件（如 `/secrets/id_ed25519`）。**不是私钥内容。** |
| `ingest_token` | `str` | `""` | `PANEL_INGEST_TOKEN` | `POST /api/ingest/*` 的 Bearer token；**空字符串 = 关闭鉴权**（任意请求都接受）。 |
| `history_retention_days` | `int` | `30` | `PANEL_HISTORY_RETENTION_DAYS` | `metric_history` 保留窗口天数，每日 retention job 据此裁剪旧行。 |

**`azure_configured` 属性（`settings.py:59`）：** 仅当 `tenant_id`、`client_id`、`client_secret_file`、`subscription_id` 四者**全部非空**时返回 `True`。`AzureVmCollector` 的 `register()` 用它做开关：缺任一项就记 warning 并跳过注册（collector 优雅禁用，应用照常运行）。

> 命名约定（写在 `Settings` docstring，`settings.py:29`）：凡字段名含 `*secret*`/`*token*`/`*key*`/`*password*`/`private_*`，或以 `_file` 结尾（凭证路径引用），都属敏感字段类目，**绝不可出现在任何对外 API 响应里**。这条约定在 `PublicModel` 一侧通过禁列字段名模式落实。

### 3.2 脱敏正则契约（`scrub.py`）

| 正则 | 覆盖 | 替换策略 |
|------|------|----------|
| `_KV_PATTERNS[0]` | `token=` / `secret=` / `password=` / `api_key=` / `apikey=` / `api-key=` / `key=`（大小写不敏感，`=` 或 `:` 分隔） | 保留键名前缀，仅 value 换成 `***`（`scrub.py:86` 用 `m.group("prefix") + "***"`）。 |
| `_KV_PATTERNS[1]` | `Bearer <token>`（Authorization 头） | 保留 `Bearer `，token 换 `***`。 |
| `_KV_PATTERNS[2]` | `Basic <base64>`（Authorization 头） | 保留 `Basic `，凭证换 `***`。 |
| `_PEM_PATTERN` | `-----BEGIN ... PRIVATE KEY-----` 到 `-----END ... PRIVATE KEY-----` 整块（`re.DOTALL` 跨行） | 整块换 `***`。**最先执行**（`scrub.py:82`）。 |
| `_HEX_PATTERN` | 前置 `= : 空白 引号` 后的 ≥32 位十六进制串 | 整串换 `***`。 |
| `_B64_PATTERN` | 前置 `= : 空白 引号` 后的 ≥32 位 base64 串（可带 `==` 结尾） | 整串换 `***`。Hex 之后执行。 |

- value 终止边界由 `_KV_VALUE = r'[^\s,"\'\]}\)>]+'` 定义（`scrub.py:29`）：value 到空白/逗号/引号/闭括号为止。
- hex/base64 的「前置字符 lookbehind」是**刻意保守**的设计，避免误伤 CSS 颜色码、URL 路径等普通文本（见 `scrub.py:51`–`scrub.py:58` 注释）。

---

## 4. 对外接口与调用关系

### 4.1 谁调用 config

| 调用方 | 调用的符号 | 用途 | 引用 |
|--------|-----------|------|------|
| `main.create_app()` | `get_settings()`、`setup_logging()` | 启动早期解析配置、装日志过滤器 | `src/panel/main.py:122`–`main.py:123` |
| `main.lifespan()` | `get_settings()`（兜底） | 取 `db_path` / `history_retention_days` 等装配资源 | `src/panel/main.py:64`、`main.py:101` |
| `main.main()` | `get_settings().port` | uvicorn 监听端口 | `src/panel/main.py:165` |
| `collectors.azure.register()` | `read_secret()`、`settings.azure_configured` | 读取 client secret 构造 credential | `src/panel/collectors/azure/__init__.py:44` |
| `collectors.scheduler` | `scrub()` | 对 `collector_run.error` 脱敏后落库 | `src/panel/collectors/scheduler.py:65`、`scheduler.py:76` |
| `api.ingest` | `Settings.ingest_token` | 可选 Bearer 鉴权 | `src/panel/api/ingest.py:31` |
| 各 collector 工厂 | `Settings`（类型注解） | 接收注入的 settings | `collectors/__init__.py`、`gpu/__init__.py`、`tailscale/__init__.py` |

### 4.2 数据流

**配置流：** 环境/`.env` → `Settings()` →（`get_settings()` 缓存）→ `create_app` 存到 `app.state.settings` → `lifespan` 与请求处理器读取。注意 `main` 优先用 `app.state.settings`（测试注入），缺失才回落 `get_settings()`（`main.py:64`、`main.py:122`、`main.py:132`）。

**凭证流：** 配置里只有路径（`azure_client_secret_file` / `ssh_key_path`）→ 运行时 `read_secret(path, settings)` 读文件 → 值短暂传给 SDK（如 `ClientSecretCredential`）→ 不入 DB、不入响应、不进日志。

**脱敏流：** 任意 `logger.xxx(...)` → root handler 的 `_ScrubFilter.filter()` → `record.getMessage()` 完整插值 → `scrub()` → 写回 `record.msg`、清空 `record.args` 防二次插值（`scrub.py:111`–`scrub.py:113`）。`collector_run.error` 另走显式 `scrub(str(exc))` 后再落库。

### 4.3 凭证三层防御（协同视图）

| 层 | 位置 | 作用 |
|----|------|------|
| 配置层 | `config/settings.py`（本模块） | 凭证只存路径，明文经 `read_secret` 懒加载 |
| 响应层 | `domain/models.py` 的 `PublicModel`（`ConfigDict(extra="forbid")`） | 对外 JSON 白名单，禁列 `*secret*/*token*/*key*/*password*/private_*/ssh_key_path` |
| 日志层 | `config/scrub.py`（本模块） | 日志与 error 摘要脱敏 |

---

## 5. 与其他模块的依赖

**上游（config 依赖谁）：** 仅 `pydantic-settings`、`pydantic`、标准库（`functools` / `pathlib` / `logging` / `re`）。**无项目内依赖**——这是刻意的，保证它能被任何模块安全导入而不产生循环。

**下游（谁依赖 config）：** `main`、`collectors.*`（azure / gpu / tailscale / scheduler / retention）、`api.ingest`。基本是全应用。

依赖方向单向向外，因此修改 config 的字段或正则会波及面较广，改动前先看第 9 节注意事项。

---

## 6. 扩展点

### 6.1 新增一个配置项

1. 在 `Settings`（`settings.py`）按所属分组（Core / Collector / Azure / …）加字段，**务必给默认值**（否则缺该 env 时实例化失败）。
2. 命名遵循 `PANEL_` 前缀约定；凭证类**必须存路径不存明文**，字段名以 `_file` 结尾或含 `secret/token/key/password`。
3. 在 `.env.example` 加注释样例；如需容器挂载，更新 `docker-compose.yml`。
4. 若该项是「某功能的开关组合」，参考 `azure_configured`，加一个 `@property` 做布尔聚合，供注册逻辑判断。
5. 在 `tests/test_config.py` 加默认值断言 + env 覆盖断言。

### 6.2 新增一种凭证（按路径引用）

1. 加 `xxx_file` 或 `xxx_key_path` 字段（默认 `""`）。
2. 在消费处用 `read_secret(settings.xxx_file, settings)` 读取；**捕获 `FileNotFoundError`/`ValueError`** 做优雅降级（见 azure `register()` 的 try/except，`azure/__init__.py:45`），且 warning 里**不要回显路径细节**。
3. 宿主放文件到 `./secrets/<name>`，compose 以 `./secrets:/secrets:ro` 只读挂载。

### 6.3 新增一条脱敏规则

1. 在 `scrub.py` 仿照现有写法编译一个新 `re.Pattern`（模块加载期编译，勿在 `scrub()` 内编译）。
2. 在 `scrub()` 函数体（`scrub.py:79`–`scrub.py:92`）按合适顺序 `result = pattern.sub(...)`；注意顺序敏感（PEM 最先，hex 在 base64 之前）。
3. 若只想替换 value、保留键名，用带 `prefix`/`value` 命名组 + `lambda m: m.group("prefix") + _REDACTED`（参考 `_KV_PATTERNS`）。
4. 在 `tests/test_config.py` 的 `test_scrub_redacts_sensitive_patterns` 参数表加正例，并确认 `test_scrub_preserves_ordinary_text` 仍绿（避免误伤）。

### 6.4 调整 ingest 鉴权 / 保留窗口

- 鉴权：改 `ingest_token` 默认或在部署设 `PANEL_INGEST_TOKEN`；逻辑在 `api/ingest.py:_check_auth`，空 token = 放行。
- 保留窗口：改 `history_retention_days` 默认或设 `PANEL_HISTORY_RETENTION_DAYS`；retention job 在 `collectors/retention.py`，由 `main.lifespan` 每日调度。

---

## 7. 配置 / 环境变量

全部见第 3.1 节表格。部署侧补充：

- `.env.example`（仓库根）：列出所有 `PANEL_*` 样例与凭证挂载约定的注释。
- `docker-compose.yml`：`- ./secrets:/secrets:ro` 把宿主 `./secrets` 只读挂入容器 `/secrets`；约定 `./secrets/<name>` ↔ `PANEL_SECRETS_DIR/<name>`。
- 凭证文件示例：`./secrets/azure_client_secret`（对应 `PANEL_AZURE_CLIENT_SECRET_FILE`）、`./secrets/id_ed25519`（对应 `PANEL_SSH_KEY_PATH`）。

---

## 8. 测试位置与覆盖

测试文件：`tests/test_config.py`（同时覆盖 `domain.models.PublicModel`，因为三者同属 TASK-005 交付）。

| 测试 | 验证点 |
|------|--------|
| `clear_settings_cache`（autouse fixture） | 每个用例前后 `get_settings.cache_clear()`，隔离单例缓存 |
| `test_settings_defaults` | 各字段内置默认值 |
| `test_settings_from_env` | `PANEL_` 前缀 env 覆盖（port/log_level/stale） |
| `test_get_settings_returns_singleton` | `get_settings()` 两次返回同一对象（lru_cache） |
| `test_get_settings_cache_clear_reinitialises` | `cache_clear()` 后重读新 env |
| `test_optional_credential_fields_default_empty` | azure/ssh 凭证字段默认空、无报错 |
| `test_read_secret_from_secrets_dir` | 相对名在 `secrets_dir` 解析、strip 尾换行 |
| `test_read_secret_from_absolute_path` | 绝对路径直读、忽略 `secrets_dir` |
| `test_read_secret_strips_whitespace` | 前后空白全 strip |
| `test_read_secret_missing_file_raises` | 缺文件 → `FileNotFoundError` |
| `test_read_secret_empty_name_raises` | 空名 → `ValueError` |
| `test_scrub_redacts_sensitive_patterns`（参数化） | token/secret/password/api_key 各形态/Bearer/Basic/PEM 全打码 |
| `test_scrub_preserves_ordinary_text` / `..._urls_without_credentials` / `..._empty_string` | 普通文本不被误伤 |
| `test_scrub_token_kv_value_replaced` / `..._secret_..` / `..._bearer_..` / `..._pem_..` | 键名保留、value 消失 |
| `test_setup_logging_installs_scrub_filter` | 装过滤器后日志中原始 secret 不出现、键名仍在 |
| `test_setup_logging_idempotent` | 多次调用不崩 |
| `test_public_model_*` | `PublicModel` 接受声明字段、拒绝额外字段（含凭证名） |

跑：`pytest tests/test_config.py -q`。

---

## 9. 注意事项 / 降级语义 / gotchas

- **lru_cache 单例陷阱**：`get_settings()` 缓存首次结果。**测试或运行时改 env 后必须 `get_settings.cache_clear()`** 才会重读；否则拿到旧值。`test_config.py` 用 autouse fixture 处理。
- **`extra="ignore"` vs `extra="forbid"`**：配置层故意宽松（忽略未知 `PANEL_*`），响应层故意严格（拒绝额外字段）。两者别搞混。
- **`read_secret` 不缓存、每次读盘**：高频路径勿反复调用；凭证拿到后短暂持有即丢（如 azure 只在构造 credential 时用一次）。
- **凭证降级要静默且不回显路径**：读不到凭证文件时应记 warning 并跳过（collector disabled），**warning 文案不要带文件路径**，避免泄露布局（见 `azure/__init__.py:47`）。
- **`ingest_token` 空 = 不鉴权**：默认空字符串意味着 `/api/ingest/*` 接受任意请求（设计假设 tailnet 内网隔离，ARCH-004）。生产若暴露需显式设 `PANEL_INGEST_TOKEN`。鉴权用**精确字符串比较** `Authorization == f"Bearer {token}"`（非常量时间比较——内网单用户场景可接受，对外暴露前应升级）。
- **`known_hosts=None`（关联，不在本模块但常一起踩）**：GPU SSH 采集器用 `known_hosts=None`（等价 `StrictHostKeyChecking=no`），是 ARCH-001 在「内网 Tailscale 隔离」前提下的首期裁定，P3 增强为加载 known_hosts 校验指纹（`collectors/gpu/collector.py:108`）。本模块的 `ssh_key_path` 只提供私钥路径，主机校验策略由采集器决定。
- **`_ScrubFilter` 改写 `record.msg` 并清 `args`**：先 `getMessage()` 完整插值再 `scrub()`，然后置 `record.args=None` 防 formatter 二次 `%` 展开（`scrub.py:111`）。`filter()` 内任何异常都被吞掉（`scrub.py:114`）——脱敏绝不能让应用崩溃，但也意味着脱敏静默失败时不会有显式报错，调试时留意。
- **`setup_logging` 幂等**：仅当 root 无 handler 时才加 handler，且 `_ScrubFilter` 不重复安装（`scrub.py:134`、`scrub.py:148`）。重复调用安全，但**改 level 不会移除旧 handler**，多次以不同 level 调用时第一次的 handler 仍在。
- **`scrub` 是保守而非完备的**：hex/base64 规则带 lookbehind 以减少误伤，因此**没有**前置 `=:空白引号` 的裸 token 不会被打码。它是纵深防御的一层，不能替代「凭证不入日志」的源头纪律。
- **e-ink/树莓派约束（项目级）**：本模块本身无前端，但默认值（如 `stale_threshold_seconds=180`、`history_retention_days=30`）服务于资源受限部署；改大保留窗口会增加 SQLite 体积，retention job 是为此而设。
- **`tailscale_socket` 仅 socket 路径**：默认 `/var/run/tailscale/tailscaled.sock`，容器需另行只读挂载该目录（compose 中默认注释掉，按需开启）。

---

## 10. 关联 REQ / ARCH / TASK

| 类型 | 编号 | 关系 |
|------|------|------|
| ARCH | ARCH-001 | 基线契约：`pydantic-settings` 选型、凭证按路径/只读挂载、响应白名单、日志脱敏规范（§凭证管理规范）；`history_retention_days` 默认 30 的 Addendum。 |
| ARCH | ARCH-004 | ingest 端点鉴权约定：tailnet 内默认不鉴权，可选 `INGEST_TOKEN` Bearer。 |
| TASK | TASK-005 | 本模块实现卡（配置/凭证 + `PublicModel` 白名单 + `scrub`/`setup_logging`）。 |
| TASK | TASK-003 | scheduler 的 `collector_run.error` 脱敏改用本模块共享 `scrub()`。 |
| TASK | TASK-030 | 新增 `ingest_token`，`api/ingest` 可选 Bearer 鉴权。 |
| TASK | TASK-040 | 新增 `history_retention_days`，`collectors/retention.prune_metric_history` 每日裁剪。 |
| TASK | TASK-018 / TASK-012 | Azure SP 四字段 + `azure_configured` + `read_secret` 读 client secret。 |
| TASK | TASK-001 | 本模块依赖的 `main.create_app` / lifespan 装配骨架。 |
