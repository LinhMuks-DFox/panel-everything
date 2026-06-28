---
id: TASK-005
title: "配置与凭证管理 + response model 白名单 + 日志脱敏"
status: done
priority: P0
architecture: ARCH-001
dependencies: [TASK-001]
estimated_effort: S
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现配置与凭证管理基线:pydantic-settings 集中加载(env / 只读挂载文件)、secrets 挂载约定、对外响应白名单基类、统一日志脱敏。确保任何凭证(SSH 私钥路径之外的明文、Azure secret、token/key)不进 DB、不进 API 响应、不进日志。后续模块卡的凭证字段全部遵循本卡约定。

## 技术规格

### 配置(config/settings.py)

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PANEL_", env_file=".env",
                                      extra="ignore")
    db_path: str = "/data/panel.db"
    port: int = 8080
    log_level: str = "info"
    stale_threshold_seconds: int = 180        # collector 视为 stale 的阈值
    secrets_dir: str = "/secrets"             # 只读挂载凭证目录
    # 模块配置项(azure/tailscale 等)由各模块卡以同样方式追加,沿用 PANEL_ 前缀

@lru_cache
def get_settings() -> Settings: ...
```

- 全部环境变量统一 `PANEL_` 前缀。
- 凭证类配置只接收**路径引用**(如 `PANEL_AZURE_SECRET_FILE=/secrets/azure_secret`、`ssh_key_path`),代码运行时读文件,**不把明文写进 Settings 的可序列化输出**(敏感字段用读文件 helper,而非直接存值;若必须存值,标注并禁止纳入任何对外模型/日志)。
- 提供 `read_secret(name_or_path) -> str` helper:从 `secrets_dir` 或绝对路径读取并 strip。

### 响应白名单(domain/models.py)

```python
class PublicModel(BaseModel):
    """所有对外 JSON 响应模型的基类。
    约定:子类只显式声明可公开字段;凭证/密钥/token/secret/private path 一律不得出现。
    model_config = ConfigDict(extra="forbid")  # 防止意外塞入额外字段
    """
```

- 文档化禁列字段名模式:`*secret*`、`*token*`、`*key*`(凭证语义)、`*password*`、`ssh_key_path`、`private_*`。
- 模块卡定义对外模型时继承 `PublicModel`,且数据库行 → 响应模型的转换显式映射(不得 `**row`)。

### 日志脱敏(config/logging.py 或 config/scrub.py)

```python
SENSITIVE_PATTERNS = [...]   # 匹配 key/secret/token/password/Bearer/私钥块等

def scrub(text: str) -> str:
    """把敏感子串替换为 ***。用于 collector_run.error、异常日志、任何外发文本。"""

def setup_logging(level: str) -> None:
    """配置 root logger;安装一个 Filter/Formatter,对每条 log message 应用 scrub。
    设置 uvicorn/apscheduler logger 级别。"""
```

- `scrub` 覆盖:`token=xxx` / `Bearer xxx` / `api[_-]?key=xxx` / `secret=xxx` / `password=xxx` / `-----BEGIN ... PRIVATE KEY-----` 块 / 长十六进制或 base64 串(谨慎,避免误伤)。
- TASK-003 的 `record_collector_run` 与 scheduler 的 error 字段改用此共享 `scrub`(替换 TASK-003 的临时实现)。
- `setup_logging` 在 `main.py` 启动早期调用(`create_app` 或 `__main__`)。

## 实现指引

1. `settings.py`:`Settings(BaseSettings)` + `get_settings` lru_cache;`read_secret` helper(目录 + 绝对路径两种)。
2. `scrub.py`:正则集合 + `scrub()`;单元测试覆盖各敏感模式与"正常文本不被误删"。
3. `logging.py`:`setup_logging` 安装 logging Filter 应用 scrub;在 `main` 早期调用。
4. `models.py`:`PublicModel` 基类(`extra="forbid"`)+ docstring 列禁字段模式。
5. 回填 TASK-003:scheduler 的 error 脱敏改调共享 `scrub`(若 TASK-003 已交付,本卡负责替换其临时实现并保证测试仍绿)。
6. compose / .env.example:补充 `PANEL_STALE_THRESHOLD_SECONDS`、`PANEL_SECRETS_DIR`,并在 compose 注释中说明 `./secrets:/secrets:ro` 挂载约定。

## 测试要求

- [ ] `get_settings` 从 env(`PANEL_` 前缀)正确加载并被 lru_cache 缓存
- [ ] `read_secret` 能从 secrets_dir 与绝对路径读取并 strip 尾换行
- [ ] `scrub` 对 token/secret/key/password/Bearer/私钥块全部打码
- [ ] `scrub` 不破坏普通文本(无敏感模式时原样返回)
- [ ] `setup_logging` 后,含敏感串的 log 经格式化输出已脱敏(捕获日志断言)
- [ ] `PublicModel` 子类传入未声明字段被拒(`extra="forbid"`)
- [ ] collector_run.error 落库内容已脱敏(与 TASK-003 联测)

## 完成标准

- [ ] `Settings` + `get_settings` + `read_secret` 就绪,统一 `PANEL_` 前缀
- [ ] 凭证只走 env/只读挂载路径,DB 与响应不含明文(契约文档化)
- [ ] `PublicModel` 白名单基类就绪,禁列字段模式文档化
- [ ] `scrub` + `setup_logging` 就绪并接入 scheduler error 与 root logger
- [ ] ruff + pytest 全绿
