---
id: TASK-017
title: "前端 GPU 趋势迷你图（默认折叠）"
status: todo
priority: P2
architecture: ARCH-002
dependencies: [TASK-016, TASK-015]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-003 增强，本期暂不实现。)** 在 GpuCard 下方增加可展开的 GPU 历史趋势迷你图（默认折叠，用户点击展开），调用 TASK-016 提供的 `/api/v1/gpu/{server_id}/{gpu_index}/history` API，使用纯 SVG 或 Canvas 绘制简单折线图（无外部图表库，e-ink 兼容）。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/web/templates/partials/_vm_card.html` | GpuCard 中追加折叠趋势图区块 |
| `src/panel/web/static/js/panel.js` | 追加迷你图绘制函数 |
| `src/panel/web/static/css/panel.css` | 迷你图容器样式 |

### HTML 结构

```html
<!-- GpuCard 内追加 -->
<details class="gpu-trend" data-server-id="{{ gpu.server_id }}"
                            data-gpu-index="{{ gpu.gpu_index }}">
  <summary class="gpu-trend-toggle">历史趋势 ▶</summary>
  <div class="gpu-trend-chart">
    <!-- JS 按需注入 SVG 折线图 -->
    <canvas class="trend-canvas" width="280" height="60"
            aria-label="GPU {{ gpu.gpu_index }} 利用率趋势图"></canvas>
  </div>
</details>
```

`<details>/<summary>` 原生 HTML 折叠，无需 JS 控制展开，JS 仅在 `toggle` 事件时懒加载数据。

### JS 迷你图绘制

```javascript
// panel.js 追加
document.querySelectorAll('.gpu-trend').forEach(details => {
  details.addEventListener('toggle', async () => {
    if (!details.open) return;
    const { serverId, gpuIndex } = details.dataset;
    const url = `/api/v1/gpu/${serverId}/${gpuIndex}/history?granularity=5m&limit=144`;
    const data = await fetch(url).then(r => r.json());
    drawMiniChart(details.querySelector('canvas'), data, 'avg_util_pct');
  });
});

function drawMiniChart(canvas, points, field) {
  // 纯 Canvas 2D 折线图
  // 无外部依赖；线宽 1.5px；颜色随阈值变 (>90% 红, >70% 橙, 其他绿)
  // e-ink 降级：若 prefers-color-scheme: no-preference，改用单色黑线
}
```

### e-ink 兼容约束

- Canvas 绘制使用单色线（不依赖颜色传递信息）；同时用线条粗细区分阈值区间
- 无动画（`requestAnimationFrame` 绘制一次即止）
- 图高度固定 60px，宽度自适应容器（`canvas.width = container.offsetWidth`）

### 数据 API 契约

来自 TASK-016：`GET /api/v1/gpu/{server_id}/{gpu_index}/history?granularity=5m&limit=144`

响应：`[{ bucket_start, avg_util_pct, avg_mem_pct, ... }]`，最多 144 个点（= 12 小时 × 12 个 5min 桶）。

## 实现指引

1. `_vm_card.html` 中在 `gpu-meta-row` 之后追加 `<details class="gpu-trend">` 块。
2. `panel.js` 追加事件监听 + `drawMiniChart` 函数（Canvas 2D API，无外部库）。
3. `panel.css` 追加 `.gpu-trend`、`.gpu-trend-chart`、`.trend-canvas` 样式；确保无 box-shadow、无 animation。
4. 懒加载：只有用户点击展开时才发起 API 请求，避免初始渲染开销。
5. 加载态：`<details>` open 时先显示「加载中…」文字，数据返回后替换为 canvas。
6. 无数据处理：API 返回空数组时显示「暂无历史数据」文字。

## 测试要求

- [ ] `<details>/<summary>` 折叠展开在无 JS 环境（纯 HTML）可访问
- [ ] toggle 事件触发后调用正确 API URL
- [ ] drawMiniChart 输入空数组不崩溃
- [ ] Canvas 绘制无 animation，e-ink 模式下颜色退化为黑色单线
- [ ] 多张卡同时展开时互不干扰（各自独立 canvas）

## 完成标准

- [ ] GpuCard 趋势区块默认折叠，展开后加载并绘制 5min 粒度折线图
- [ ] 纯 Canvas 实现，无外部图表库依赖
- [ ] e-ink 灰度单色降级路径实现
- [ ] 懒加载（展开时才请求 API）
- [ ] 无 box-shadow、无 animation
