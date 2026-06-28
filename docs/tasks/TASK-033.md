---
id: TASK-033
title: "前端 AI 额度卡片（泛化渲染、stale 标记、手动降级）"
status: todo
priority: P2
architecture: ARCH-004
dependencies: [TASK-004, TASK-030]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-004 后期，本期不实现)**

在面板前端新增 AI 额度模块，包含：

1. 后端 GET API（`/api/ai-usage`）从 `latest_snapshot` 聚合并输出所有 provider 最新快照 + stale 判断
2. Jinja2 partial `_ai_card.html`，每个 provider 渲染一张独立卡片
3. 泛化渲染：`used_percent` 进度条 + 分子/分母 + `resets_at` 倒计时
4. 阈值变色（正常/警告/危险）、stale 横幅、ChatGPT 手动降级徽标
5. e-ink 适配（无动画/box-shadow，降级符号）

---

## 技术规格

### 后端 API（`api/ai_usage.py`）

文件路径：`src/panel/api/ai_usage.py`

```python
router = APIRouter(prefix="/api", tags=["ai-usage"])

@router.get("/ai-usage")
async def get_ai_usage(
    repo: Repository = Depends(get_repo),
) -> AiUsageResponse: ...
```

**查询逻辑**：

1. `repo.get_snapshot("ai_usage")` 取所有 provider 的最新 snapshot 行
2. 从 `ai_provider` 表取 provider 元数据（`display_name`、`source_type`、`window_seconds`）
3. 按 provider 分组，提取关键 metrics：`used_percent`、`used_requests`/`used_tokens`、`limit_requests`/`limit_tokens`、`resets_at`、`window_seconds`
4. **stale 判断**：`(now - collected_at).total_seconds() > window_seconds * 0.5` 则 `stale=True`；或 `status="error"` 则 `stale=True`
5. 返回 `AiUsageResponse`

**响应 schema**（`domain/models.py`）：

```python
class AiProviderStatus(BaseModel):
    provider: str
    display_name: str
    source_type: str            # 'local_jsonl' | 'oauth_api' | 'manual'
    used_percent: float | None
    used_value: float | None    # used_requests 或 used_tokens，取到哪个用哪个
    limit_value: float | None
    metric_unit: str            # 'requests' | 'tokens'
    resets_at: str | None       # ISO8601 UTC
    window_label: str           # '5h rolling'
    stale: bool
    stale_since: str | None     # collected_at ISO8601，stale=True 时填
    collected_at: str | None
    status: str                 # 'ok' | 'error' | 'no_data'

class AiUsageResponse(BaseModel):
    providers: list[AiProviderStatus]
    last_updated: str           # 最新一条 collected_at，无数据则 null
```

**`metric_unit` 推断规则**：snapshot 中若有 `used_requests` 则 `metric_unit="requests"`，若有 `used_tokens` 则 `"tokens"`，两者都没有则 `"unknown"`。

### Jinja2 模板（`web/templates/partials/_ai_card.html`）

模板接受 `ai_providers: list[AiProviderStatus]` 上下文变量。

**HTML 骨架**（每张卡）：

```html
<section class="card" data-module="ai-usage" data-provider="{{ p.provider }}">
  <div class="card-header">
    <span class="status-dot {{ status_class }}">{{ status_symbol }}</span>
    <h2>{{ p.display_name }}</h2>
    {% if p.source_type == 'manual' %}
      <span class="badge badge-manual">手动</span>
    {% endif %}
  </div>

  {% if p.stale %}
  <div class="datasource-banner">
    数据可能过旧（上次更新 {{ stale_age }} 前）
  </div>
  {% endif %}

  {% if p.used_percent is not none %}
  <div class="metric-bar-wrap">
    <div class="metric-bar" role="progressbar"
         aria-valuenow="{{ p.used_percent }}"
         style="--pct: {{ p.used_percent }}%">
    </div>
    <span class="metric-bar-label">
      {{ p.used_percent | round(1) }}%
      {% if p.used_value and p.limit_value %}
        （{{ p.used_value | int }} / {{ p.limit_value | int }} {{ p.metric_unit }}）
      {% endif %}
    </span>
  </div>
  {% else %}
  <p class="metric-unknown">用量未知{% if p.source_type == 'manual' %}（请手动更新）{% endif %}</p>
  {% endif %}

  {% if p.resets_at %}
  <p class="resets-at">
    窗口重置：<time datetime="{{ p.resets_at }}" data-countdown="{{ p.resets_at }}">{{ p.resets_at }}</time>
    <span class="window-label">（{{ p.window_label }}）</span>
  </p>
  {% endif %}

  <p class="collected-at">数据时间：{{ p.collected_at or '—' }}</p>
</section>
```

**`index.html` 注入**：

```html
{% include "partials/_ai_card.html" %}
```

### CSS 规范（追加到 `static/css/panel.css`）

```css
/* AI 额度模块 */
[data-module="ai-usage"] .metric-bar {
  height: 12px;
  background: #e0e0e0;
  border: 1px solid #999;     /* e-ink 无 gradient，用 border */
  border-radius: 0;
  position: relative;
  overflow: hidden;
}
[data-module="ai-usage"] .metric-bar::after {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: var(--pct, 0%);
  background: #333;            /* e-ink 灰度友好 */
  /* 无动画 */
}
/* 阈值变色 — 仅非 e-ink 设备 */
@media (color) {
  [data-provider][data-pct-warn] .metric-bar::after { background: #f0a020; }
  [data-provider][data-pct-error] .metric-bar::after { background: #d32f2f; }
}

.badge-manual {
  font-size: 0.7em;
  border: 1px solid currentColor;
  padding: 0 4px;
  border-radius: 2px;
  opacity: 0.7;
}
```

**JavaScript（`panel.js` 追加）**：

- `resets_at` 倒计时：`DOMContentLoaded` 后找所有 `[data-countdown]` 元素，每分钟更新显示「X 小时 Y 分钟后重置」
- `data-pct-warn/error` 属性由 JS 在加载时根据 `aria-valuenow` 动态设到 `section` 上（>70 加 warn，>90 加 error）
- Page Visibility API：页面不可见时暂停倒计时（复用 TASK-004 的 Visibility 暂停机制）

### e-ink 适配要点

| 要求 | 实现 |
|------|------|
| 无 box-shadow | 用 `border` 替代 |
| 无 CSS animation/transition | 进度条静态渲染 |
| 无颜色依赖（灰度） | 进度条用深灰，阈值变色用 `@media (color)` 包裹 |
| 状态符号 | 正常 `●`、警告 `◐`、危险 `●`（红色被忽略但符号仍有意义）、stale `○`、手动 `◌` |
| Kindle meta refresh | base 模板已有 `<meta http-equiv="refresh" content="60">`（TASK-004 实现），本卡不新增 |

### 路由注册

`src/panel/main.py`：

```python
from panel.api.ai_usage import router as ai_usage_router
app.include_router(ai_usage_router)
```

`web/routes.py`（SSR 路由）：

```python
ai_providers = await get_ai_usage_data(repo)   # 复用 api 层查询逻辑
return templates.TemplateResponse("index.html", {
    ...,
    "ai_providers": ai_providers,
})
```

---

## 实现指引

1. **开发顺序**：先写 `api/ai_usage.py` 并用 curl 验证数据正确，再写模板

2. **stale_age 计算（Jinja2 filter 或 Python 预处理）**
   - 建议在后端算好 `stale_age_label: str`（如 `"2h 15m"`）直接传模板，避免 Jinja2 内复杂计算

3. **`metric_unit` + `used_value`/`limit_value` 统一**
   - API 层负责把 `used_requests`/`used_tokens` 统一成 `used_value + metric_unit`，模板只消费统一字段，不做判断

4. **无数据（`status="no_data"`）渲染**
   - provider 配置存在但从未收到上报时，显示空卡：`◌ <display_name>` + 「尚未收到数据，请确认 Reporter 已部署」

5. **ChatGPT 手动更新流程**
   - 本卡不实现手动输入 UI（属于操作型，与面板被动消费原则相悖）
   - 用户通过在工作站手工编辑 `~/.panel_reporter/chatgpt.json` → Reporter 下次 cron 上报
   - 卡片只展示当前值 + `◌` + 「手动更新」徽标即可

6. **`resets_at` 前端倒计时**（`panel.js`，约 20 行）：
   ```javascript
   function updateCountdowns() {
     document.querySelectorAll('[data-countdown]').forEach(el => {
       const t = new Date(el.dataset.countdown);
       const diff = t - Date.now();
       if (diff <= 0) { el.textContent = '已重置'; return; }
       const h = Math.floor(diff / 3600000);
       const m = Math.floor((diff % 3600000) / 60000);
       el.textContent = `${h}h ${m}m 后重置`;
     });
   }
   updateCountdowns();
   setInterval(updateCountdowns, 60000);
   ```

---

## 测试要求

- [ ] `test_get_ai_usage_api_ok`：插入 mock snapshot 后 GET `/api/ai-usage` 返回正确 `used_percent` + `stale=false`
- [ ] `test_get_ai_usage_stale`：`collected_at` 设为 `now - 3h`（5h 窗口 50% 阈值超出）→ `stale=true`，`stale_since` 非空
- [ ] `test_get_ai_usage_no_data`：未上报 codex 时，codex provider `status="no_data"`
- [ ] `test_get_ai_usage_metric_unit`：有 `used_requests` 记录 → `metric_unit="requests"`；有 `used_tokens` → `"tokens"`
- [ ] `test_ai_card_render_ok`：Jinja2 模板 render，`used_percent=80` → 含 `status-warn` CSS 类（或 `data-pct-warn`）
- [ ] `test_ai_card_manual_badge`：`source_type="manual"` → 模板含 `.badge-manual` + `◌`
- [ ] `test_ai_card_stale_banner`：`stale=true` → 模板含 `.datasource-banner`
- [ ] `test_ai_card_no_data`：`status="no_data"` → 模板含 「尚未收到数据」提示

---

## 完成标准

- [ ] `src/panel/api/ai_usage.py` 实现 GET `/api/ai-usage`，stale 判断逻辑正确
- [ ] `web/templates/partials/_ai_card.html` 渲染 used_percent 进度条、stale 横幅、手动降级徽标
- [ ] `index.html` 注入 `_ai_card.html`
- [ ] `panel.css` 进度条样式：e-ink 无动画、阈值变色用 `@media (color)` 包裹
- [ ] `panel.js` 倒计时逻辑：DOMContentLoaded 触发，每分钟更新，Page Visibility 暂停
- [ ] 所有测试通过
- [ ] Kindle 浏览器手动访问验证：进度条可见（灰度），状态符号正确，无白屏/报错
