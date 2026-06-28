---
id: TASK-022
title: "前端 NodeCard/NodeGrid/StaleWarning（e-ink 适配）"
status: todo
priority: P1
architecture: ARCH-003
dependencies: [TASK-004, TASK-021]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 Tailscale 节点面板的前端部分：节点网格（NodeGrid）、单节点卡片（NodeCard）、
数据源异常横幅（StaleWarning）。
完全符合 ARCH-001 前端壳规范：e-ink 适配、三态徽标、Page Visibility 暂停轮询、
Kindle `<meta refresh>` 降级。

## 技术规格

### 文件路径

- `src/panel/web/templates/partials/_node_grid.html` — NodeGrid + StaleWarning
- `src/panel/web/templates/partials/_node_card.html` — 单卡片（由 _node_grid 循环引用）
- `src/panel/web/templates/index.html` — 追加 `{% include "partials/_node_grid.html" %}`
- `src/panel/web/static/css/panel.css` — 追加 Tailscale 节点相关样式
- `src/panel/web/static/js/panel.js` — 追加 Tailscale 轮询逻辑
- `tests/web/test_tailscale_render.py` — SSR 渲染测试

### HTML 结构（_node_grid.html）

```html
<section class="card" data-module="tailscale" id="tailscale-section">
  <h2 class="card-title">
    <span class="status-dot {% if collector_status == 'up' %}status-ok{% elif collector_status == 'never_run' %}status-stale{% else %}status-error{% endif %}"
          aria-label="{{ collector_status }}">●</span>
    Tailscale 网络
    <span class="node-summary">
      {{ nodes_online }}/{{ nodes_total }} 在线
    </span>
  </h2>

  {% if is_stale %}
  <div class="datasource-banner" role="alert">
    数据已超过 {{ stale_seconds }}s 未更新，可能不反映当前状态
  </div>
  {% endif %}

  {% if collector_status not in ('up', 'never_run') %}
  <div class="datasource-banner datasource-banner--error" role="alert">
    Tailscale 数据源异常：{{ collector_error or '采集器未响应' }}
  </div>
  {% endif %}

  <div class="node-grid" id="node-grid">
    {% for node in nodes %}
      {% include "partials/_node_card.html" %}
    {% endfor %}
  </div>
</section>
```

### HTML 结构（_node_card.html）

```html
<article class="node-card" data-node-id="{{ node.id }}"
         data-state="{{ node.online_state }}"
         aria-label="{{ node.hostname }} {{ node.online_state }}">
  <header class="node-card__header">
    <span class="status-dot
      {% if node.online_state == 'ONLINE' %}status-ok
      {% elif node.online_state == 'OFFLINE' %}status-warn
      {% else %}status-error{% endif %}"
      aria-hidden="true">
      {% if node.online_state == 'ONLINE' %}●
      {% elif node.online_state == 'OFFLINE' %}◐
      {% else %}○{% endif %}
    </span>
    <span class="node-card__hostname">{{ node.hostname }}</span>
    {% if node.is_exit_node %}
    <span class="node-card__badge node-card__badge--exit" title="Exit Node">⇢</span>
    {% endif %}
  </header>
  <dl class="node-card__meta">
    <dt>IP</dt>
    <dd class="node-card__ip">{{ node.tailscale_ips[0] if node.tailscale_ips else '—' }}</dd>
    <dt>OS</dt>
    <dd>{{ node.os or '—' }}</dd>
    {% if node.online_state != 'ONLINE' and node.last_seen %}
    <dt>最后在线</dt>
    <dd class="node-card__last-seen">{{ node.last_seen | datetimeformat }}</dd>
    {% endif %}
  </dl>
  {% if node.is_stale %}
  <div class="node-card__stale-mark" aria-label="数据已过时">◌</div>
  {% endif %}
</article>
```

### CSS 规范（panel.css 追加，遵循 ARCH-001 命名约定）

```css
/* === Tailscale 节点网格 === */
.node-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 0.5rem;
}

.node-card {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  /* 禁止 box-shadow（e-ink 兼容） */
}

.node-card__header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-weight: 600;
  font-size: 0.9rem;
  overflow: hidden;
}

.node-card__hostname {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-card__meta {
  display: grid;
  grid-template-columns: auto 1fr;
  column-gap: 0.4rem;
  font-size: 0.75rem;
  margin: 0.3rem 0 0;
  color: var(--text-secondary);
}

.node-card__badge--exit {
  font-size: 0.7rem;
  border: 1px solid currentColor;
  padding: 0 2px;
  border-radius: 2px;
}

.node-card__stale-mark {
  font-size: 0.7rem;
  color: var(--text-secondary);
  text-align: right;
}

.node-summary {
  font-size: 0.8rem;
  font-weight: normal;
  margin-left: auto;
  color: var(--text-secondary);
}

/* LONG_OFFLINE 节点视觉降调 */
.node-card[data-state="LONG_OFFLINE"] {
  opacity: 0.55;
}

/* 三断点响应式 */
@media (max-width: 599px) {
  .node-grid {
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  }
}

@media (min-width: 600px) and (max-width: 1023px) {
  .node-grid {
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  }
}

@media (min-width: 1024px) {
  .node-grid {
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  }
}
```

### JS 轮询（panel.js 追加）

```javascript
// Tailscale 节点轮询（45s 默认，Page Visibility 暂停）
const TAILSCALE_POLL_INTERVAL = 45_000;
let tailscalePollTimer = null;

async function fetchTailscaleNodes() {
  try {
    const resp = await fetch('/api/tailscale/nodes');
    if (!resp.ok) return;
    const nodes = await resp.json();
    renderNodeGrid(nodes);
  } catch (e) {
    console.warn('Tailscale fetch failed', e);
  }
}

function renderNodeGrid(nodes) {
  const grid = document.getElementById('node-grid');
  if (!grid) return;
  // 仅更新 data-state 属性与 status-dot class，不重建 DOM
  for (const node of nodes) {
    const card = grid.querySelector(`[data-node-id="${node.id}"]`);
    if (!card) continue;
    card.dataset.state = node.online_state;
    const dot = card.querySelector('.status-dot');
    if (dot) {
      dot.className = 'status-dot ' + stateToClass(node.online_state);
      dot.textContent = stateToSymbol(node.online_state);
    }
    const staleEl = card.querySelector('.node-card__stale-mark');
    if (node.is_stale && !staleEl) {
      // 追加 stale 标记
      const mark = document.createElement('div');
      mark.className = 'node-card__stale-mark';
      mark.setAttribute('aria-label', '数据已过时');
      mark.textContent = '◌';
      card.appendChild(mark);
    } else if (!node.is_stale && staleEl) {
      staleEl.remove();
    }
  }
}

function stateToClass(state) {
  if (state === 'ONLINE') return 'status-ok';
  if (state === 'OFFLINE') return 'status-warn';
  return 'status-error';
}

function stateToSymbol(state) {
  if (state === 'ONLINE') return '●';
  if (state === 'OFFLINE') return '◐';
  return '○';
}

function startTailscalePoll() {
  if (tailscalePollTimer) clearInterval(tailscalePollTimer);
  tailscalePollTimer = setInterval(fetchTailscaleNodes, TAILSCALE_POLL_INTERVAL);
}

function stopTailscalePoll() {
  if (tailscalePollTimer) { clearInterval(tailscalePollTimer); tailscalePollTimer = null; }
}

// Page Visibility API：页面隐藏时暂停轮询
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopTailscalePoll(); }
  else { fetchTailscaleNodes(); startTailscalePoll(); }
});

// 初始启动
fetchTailscaleNodes();
startTailscalePoll();
```

### Kindle 降级

`base.html` 的 `<head>` 中检测 Kindle UA 或在 template 上下文传 `kindle=True`，条件性注入：

```html
{% if kindle %}
<meta http-equiv="refresh" content="60">
{% endif %}
```

Kindle 不执行 JS，依赖 meta refresh 每 60s 重载整页 SSR。

### Jinja2 过滤器

在 `web/routes.py` 或 `main.py` 中注册自定义过滤器：

```python
def datetimeformat(value, fmt="%Y-%m-%d %H:%M UTC"):
    if value is None:
        return "—"
    return value.strftime(fmt)

app.jinja_env.filters["datetimeformat"] = datetimeformat
```

### SSR 渲染路由（web/routes.py 追加片段）

```python
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, repo: Repository = Depends(get_repo)):
    nodes = await repo.get_all_nodes()
    last_run = await repo.get_last_run("tailscale")
    # ... 组装 context
    context = {
        "nodes": nodes,
        "nodes_online": sum(1 for n in nodes if n.online_state == "ONLINE"),
        "nodes_total": len(nodes),
        "collector_status": last_run.status if last_run else "never_run",
        "collector_error": last_run.error if last_run else None,
        "is_stale": is_stale,
        "stale_seconds": STALE_THRESHOLD_SECONDS,
        "kindle": is_kindle(request),
    }
    return templates.TemplateResponse("index.html", {"request": request, **context})
```

## 实现指引

1. 先在 `index.html` 的 `{% block content %}` 内对应位置加 `{% include "partials/_node_grid.html" %}`，使 Tailscale 节段出现在总览页。
2. 实现 `_node_grid.html` 和 `_node_card.html`，确保 Jinja2 循环变量名与 `routes.py` 传入的 context key 一致（`nodes`、`node`）。
3. 在 `panel.css` 追加节点网格相关样式，复用 `.card`、`.status-dot`、`.datasource-banner` 等全局 class，不引入新命名体系。
4. 在 `panel.js` 末尾追加 Tailscale 轮询代码，避免与已有 VM/GPU 轮询冲突（各自独立 timer）。
5. `renderNodeGrid` 函数采用**局部 DOM 更新**（只改 class/textContent/data-attribute），不整体重建节点列表，避免闪烁。
6. `is_exit_node` 显示为小徽标 `⇢`（e-ink 可见），不使用 emoji 以外的彩色图标。
7. LONG_OFFLINE 节点用 `opacity: 0.55` 视觉降调，保持存在但不抢眼；不做动画（e-ink 禁止）。
8. `datetimeformat` 过滤器处理 `None`（返回 `—`）和 aware datetime（格式化为 UTC 字符串）。

## 测试要求

- [ ] SSR 渲染测试（`test_tailscale_render.py`）：向 `index` 路由注入 mock nodes（含 ONLINE/OFFLINE/LONG_OFFLINE 各一），验证响应 HTML 含对应 `status-ok`、`status-warn`、`status-error` class。
- [ ] StaleWarning 测试：传入 `is_stale=True`，验证 HTML 含 `datasource-banner`；`is_stale=False` 时不含。
- [ ] 空节点列表测试：`nodes=[]` 时页面不报 500，`node-grid` 容器存在但为空。
- [ ] e-ink 约束验证（人工检查/代码审查）：`panel.css` 中 Tailscale 相关样式不含 `box-shadow`、`animation`、`transition`。
- [ ] JS 测试（可选，jsdom）：`renderNodeGrid` 接受不同 state 的节点数组，验证 `status-dot` class 和符号正确更新。

## 完成标准

- [ ] 面板首页可正确渲染 Tailscale 节点网格，三种在线态均显示对应符号（●/◐/○）和 CSS class。
- [ ] ONLINE/OFFLINE/LONG_OFFLINE 三态徽标颜色 + 形符 + 文字均正确编码（满足 ARCH-001 三重编码规范）。
- [ ] StaleWarning 横幅在 `is_stale=True` 或 `collector_status not in ('up','never_run')` 时正确显示。
- [ ] JS 轮询在页面可见时每 45s 请求 `/api/tailscale/nodes`，页面隐藏时停止（Page Visibility API）。
- [ ] Kindle UA 或 `kindle=True` 时 `<meta refresh>` 60s 注入，JS 轮询不阻塞渲染（无 JS 降级可用）。
- [ ] 三断点（<600px / 600-1024px / >1024px）布局均正常，无水平溢出。
- [ ] `ruff check` 零 error，SSR 测试全绿。
