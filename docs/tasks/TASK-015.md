---
id: TASK-015
title: "前端 VmCard + GpuCard + 状态徽标（e-ink 适配）"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-004, TASK-014]
estimated_effort: M
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 Azure VM 与 GPU 监控的前端展示层：VmCard（VM 名称、电源态、颜色/形符三重编码）与 GpuCard（逐卡利用率条、显存条、温度、功耗），以及 Kindle e-ink 灰度降级（禁 box-shadow/动画，实心/空心圆替代颜色）。VmCard 嵌套展示其下属 GPU 卡组。数据源来自 `GET /api/v1/dashboard/azure`，通过 panel.js 轮询刷新。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/web/templates/partials/_vm_card.html` | VmCard + 嵌套 GpuCard Jinja2 partial |
| `src/panel/web/templates/index.html` | `{% include "partials/_vm_card.html" %}` 注入 |
| `src/panel/web/static/css/panel.css` | 追加 VM/GPU 相关 CSS（不新建文件） |
| `src/panel/web/static/js/panel.js` | 追加轮询逻辑刷新 Azure 区块（不新建文件） |
| `src/panel/web/routes.py` | `GET /` SSR 路由，注入 dashboard 数据到模板上下文 |

### VmCard HTML 结构

```html
<!-- _vm_card.html -->
<section class="card" data-module="azure">
  <h2 class="section-title">Azure 云服务器</h2>

  {% if collector_status.azure_vm.status == "down" %}
  <div class="datasource-banner datasource-error">
    数据源异常 — {{ collector_status.azure_vm.error or "采集失败" }}
  </div>
  {% endif %}

  {% for vm in vms %}
  <article class="vm-card" data-server-id="{{ vm.server_id }}">
    <header class="vm-header">
      <span class="status-dot status-{{ vm_status_class(vm) }}"
            aria-label="{{ vm.power_state }}">
        {{ vm_status_symbol(vm) }}
      </span>
      <span class="vm-name">{{ vm.name }}</span>
      <span class="vm-state-label">{{ vm.power_state }}</span>
      {% if vm.is_stale %}
      <span class="stale-badge" title="数据陈旧">⚠ 陈旧</span>
      {% endif %}
    </header>

    <dl class="vm-meta">
      <dt>资源组</dt><dd>{{ vm.azure_resource_group or "—" }}</dd>
    </dl>

    {% if vm.gpus %}
    <div class="gpu-list">
      {% for gpu in vm.gpus %}
      <div class="gpu-card {% if gpu.is_stale %}gpu-stale{% endif %}"
           data-gpu-index="{{ gpu.gpu_index }}">
        <div class="gpu-label">GPU {{ gpu.gpu_index }}
          <span class="gpu-name-small">{{ gpu.gpu_name or "" }}</span>
        </div>

        {% if gpu.util_pct is not none %}
        <div class="metric-bar-row">
          <span class="metric-label">算力</span>
          <div class="metric-bar">
            <div class="metric-bar-fill {{ util_threshold_class(gpu.util_pct) }}"
                 style="width: {{ gpu.util_pct | round(1) }}%"></div>
          </div>
          <span class="metric-value">{{ gpu.util_pct | round(1) }}%</span>
        </div>

        <div class="metric-bar-row">
          <span class="metric-label">显存</span>
          <div class="metric-bar">
            <div class="metric-bar-fill {{ mem_threshold_class(gpu.mem_pct) }}"
                 style="width: {{ gpu.mem_pct | round(1) }}%"></div>
          </div>
          <span class="metric-value">
            {{ (gpu.mem_used_mib / 1024) | round(1) }}G /
            {{ (gpu.mem_total_mib / 1024) | round(1) }}G
          </span>
        </div>

        <div class="gpu-meta-row">
          <span>{{ gpu.temp_c | round(0) }}°C</span>
          <span>{{ gpu.power_w | round(0) }}W</span>
        </div>
        {% else %}
        <div class="gpu-unreachable">
          <span class="status-dot status-error">○</span>
          不可达
        </div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </article>
  {% endfor %}
</section>
```

### Jinja2 自定义过滤器 / 全局函数

在 `web/routes.py` 或 Jinja2 环境中注册：

```python
def vm_status_class(vm: DashboardVmOut) -> str:
    """返回 CSS class 后缀: ok / warn / error / stale"""
    if vm.is_stale:
        return "stale"
    match vm.power_state:
        case "Running":       return "ok"
        case "Starting" | "Stopping" | "Deallocating": return "warn"
        case "Stopped" | "Deallocated": return "warn"
        case _: return "error"

def vm_status_symbol(vm: DashboardVmOut) -> str:
    """三重编码形符: ●/◐/○/◌"""
    match vm_status_class(vm):
        case "ok":    return "●"
        case "warn":  return "◐"
        case "stale": return "◌"
        case _:       return "○"

def util_threshold_class(pct: float) -> str:
    if pct >= 90: return "bar-critical"
    if pct >= 70: return "bar-warn"
    return "bar-ok"

def mem_threshold_class(pct: float | None) -> str:
    if pct is None: return ""
    if pct >= 90: return "bar-critical"
    if pct >= 75: return "bar-warn"
    return "bar-ok"
```

### CSS（追加至 panel.css）

```css
/* === ARCH-002: VM / GPU Cards === */

.vm-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin-bottom: 0.75rem;
}

.vm-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}
.vm-name { font-weight: 600; }
.vm-state-label { color: var(--text-muted); font-size: 0.85em; }

.stale-badge {
  font-size: 0.75em;
  color: var(--warn);
  margin-left: auto;
}

/* 利用率/显存条 */
.metric-bar-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0.2rem 0;
}
.metric-label { font-size: 0.8em; width: 2.5rem; flex-shrink: 0; }
.metric-bar {
  flex: 1;
  height: 8px;
  background: var(--bar-bg, #e0e0e0);
  border-radius: 4px;
  overflow: hidden;
}
.metric-bar-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.3s ease;
}
.metric-value { font-size: 0.8em; width: 6rem; text-align: right; flex-shrink: 0; }

/* 阈值变色 */
.bar-ok       { background: var(--color-ok, #4caf50); }
.bar-warn     { background: var(--color-warn, #ff9800); }
.bar-critical { background: var(--color-error, #f44336); }

.gpu-card {
  background: var(--card-inner-bg, #f8f8f8);
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  margin-top: 0.5rem;
}
.gpu-label { font-size: 0.85em; font-weight: 600; margin-bottom: 0.3rem; }
.gpu-name-small { font-weight: normal; color: var(--text-muted); margin-left: 0.3rem; }
.gpu-meta-row { font-size: 0.8em; color: var(--text-muted); margin-top: 0.2rem; }
.gpu-unreachable { font-size: 0.85em; color: var(--text-muted); }

/* e-ink 灰度降级 */
@media (prefers-color-scheme: no-preference), print {
  .vm-card            { border-radius: 0; }
  .metric-bar-fill    { transition: none; }
  .gpu-card           { background: none; border: 1px solid #ccc; }
}

/* Kindle / e-ink: 禁动画, 禁 box-shadow */
@media screen and (max-width: 800px) and (color-index: 2) {
  .metric-bar-fill { transition: none; }
  .vm-card         { box-shadow: none; }
}
```

e-ink CSS 注意：ARCH-001 规定 `box-shadow` 和动画对 e-ink 设备（Kindle 600px 宽，color-index:2 或 no-preference）应完全禁止。上述规则已遵守。

### 状态三重编码总表（VM 维度）

| 状态 | CSS class | 形符 | 说明 |
|------|-----------|------|------|
| Running | `.status-ok` | ● 实心 | 正常运行 |
| Starting/Stopping/Deallocating | `.status-warn` | ◐ 半圆 | 过渡中 |
| Stopped/Deallocated | `.status-warn` | ◐ 半圆 | 已停止 |
| Unknown/Error | `.status-error` | ○ 空心 | 异常 |
| Stale | `.status-stale` | ◌ 虚线圆 | 数据陈旧 |

### JS 轮询（panel.js 追加）

```javascript
// panel.js 追加：Azure dashboard 轮询
const AZURE_POLL_MS = 45_000;  // 45 秒，ARCH-001 默认值

async function refreshAzure() {
  try {
    const resp = await fetch('/api/v1/dashboard/azure');
    if (!resp.ok) return;
    const data = await resp.json();
    renderAzureDashboard(data);
  } catch (e) {
    // 网络失败静默，等待下次轮询
  }
}

function renderAzureDashboard(data) {
  // 用 data.vms 动态更新 [data-server-id] 元素的内容
  // 简单方案：整体替换 section[data-module="azure"] innerHTML
  // 注意：Page Visibility 暂停（ARCH-001 panel.js 已实现 visibilitychange 统一管理）
}
```

Page Visibility 暂停逻辑复用 ARCH-001 已实现的统一管理机制。

### SSR 初始渲染（web/routes.py）

```python
# routes.py 中 GET / 路由
@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    gpu_repo = request.app.state.gpu_repo
    repo = request.app.state.repo
    dashboard = await build_azure_dashboard(gpu_repo, repo)  # 复用 TASK-014 的聚合逻辑
    return templates.TemplateResponse("index.html", {
        "request": request,
        "azure_dashboard": dashboard,
        ...
    })
```

`index.html` 中 `{% include "partials/_vm_card.html" %}` 传入 `azure_dashboard`。

## 实现指引

1. 创建 `src/panel/web/templates/partials/_vm_card.html`，结构如上，使用 Jinja2 模板语法。
2. 在 `web/routes.py` 注册 `vm_status_class`、`vm_status_symbol`、`util_threshold_class`、`mem_threshold_class` 为 Jinja2 全局函数（`templates.env.globals[...]`）。
3. `panel.css` 追加 `=== ARCH-002 ===` 注释块，新增上述 CSS 变量和规则。不修改 ARCH-001 已有的基础变量（`--border`、`--color-ok` 等复用）。
4. `panel.js` 追加 `refreshAzure()` + `renderAzureDashboard()`；注册到 ARCH-001 的统一轮询管理器中（参考 ARCH-001 规定的 Page Visibility 暂停机制）。
5. `index.html` 中在合适位置 `{% include "partials/_vm_card.html" %}`。
6. SSR 路由调用 TASK-014 实现的 `get_azure_dashboard` 核心逻辑（抽取为可复用函数而非仅作为 FastAPI handler）。
7. Kindle 降级：`_vm_card.html` 中 `<meta http-equiv="refresh">` 已由 ARCH-001 base.html 实现；本卡只确保 GPU 进度条在 Kindle（600px 宽，e-ink）上不依赖 box-shadow 和 transition。

## 测试要求

- [ ] 用 `TestClient` 请求 `GET /`，返回 200，响应 HTML 包含 `data-module="azure"`
- [ ] `vm_status_class` 函数：Running→"ok"，Deallocated→"warn"，Unknown→"error"，stale→"stale"
- [ ] `util_threshold_class`：0%→"bar-ok"，70%→"bar-warn"，90%→"bar-critical"
- [ ] `mem_threshold_class`：75%→"bar-warn"，90%→"bar-critical"，None→""
- [ ] 渲染时 GPU util_pct=None 时显示「不可达」区块而非报错
- [ ] HTML 中无 `ssh_key_path` 字段内容泄露（grep 验证）
- [ ] CSS 规则中无 `box-shadow`（grep `panel.css` 对应 ARCH-002 块）
- [ ] CSS 规则中无 `animation`/`@keyframes`（grep 验证）

## 完成标准

- [ ] `_vm_card.html` 实现 VmCard + GpuCard 嵌套，e-ink 降级规则正确
- [ ] `panel.css` 追加 VM/GPU 样式，三断点响应式，无 box-shadow 无 animation
- [ ] `panel.js` 实现 Azure 区块轮询（45s，Page Visibility 暂停）
- [ ] SSR 初始渲染正确传入 dashboard 数据
- [ ] 状态三重编码（色+形符+文字）完整实现
- [ ] e-ink 专用降级路径（灰度实心/空心圆）经代码审查确认
