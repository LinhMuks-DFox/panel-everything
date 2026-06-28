/**
 * Panel Everything — panel.js
 * TASK-004 / ARCH-001
 *
 * Progressive enhancement only. This file is NOT loaded on e-ink devices
 * (the server omits the <script> tag when is_eink=true).
 *
 * Features:
 *   1. Polling: fetch the dashboard page every N seconds (default 45s),
 *      replacing the DOM content.
 *   2. Page Visibility: pause polling when the tab is hidden, resume and
 *      immediately refresh when it becomes visible again.
 *      This ensures CPU≈0 when the panel is not being viewed (REQ-001).
 *
 * No framework, no build step, no external dependencies.
 */

(function () {
  "use strict";

  // ── Configuration ───────────────────────────────────────────────────────
  var grid = document.getElementById("panel-grid");
  var pollInterval =
    (grid && parseInt(grid.dataset.pollInterval, 10)) || 45;
  pollInterval = Math.max(10, Math.min(300, pollInterval)); // clamp 10s–300s

  var pollMs = pollInterval * 1000;
  var timerId = null;
  var isPolling = false;

  // ── Live clock ──────────────────────────────────────────────────────────
  /**
   * Update the header timestamp every second without a server round-trip.
   * Purely cosmetic — the SSR timestamp is the authoritative "data freshness"
   * marker; this just keeps the clock ticking between polls.
   */
  var timeEl = document.getElementById("site-time");

  function tickClock() {
    if (!timeEl) return;
    var now = new Date();
    var y = now.getUTCFullYear();
    var mo = String(now.getUTCMonth() + 1).padStart(2, "0");
    var d = String(now.getUTCDate()).padStart(2, "0");
    var h = String(now.getUTCHours()).padStart(2, "0");
    var mi = String(now.getUTCMinutes()).padStart(2, "0");
    var s = String(now.getUTCSeconds()).padStart(2, "0");
    timeEl.textContent = y + "-" + mo + "-" + d + " " + h + ":" + mi + ":" + s + " UTC";
  }

  setInterval(tickClock, 1000);

  // ── Polling ─────────────────────────────────────────────────────────────
  /**
   * Fetch the root page and replace only the #panel-grid contents.
   * Falling back gracefully: any fetch/parse error is silently ignored
   * so the current (possibly stale) DOM remains visible.
   */
  function refreshDashboard() {
    fetch(window.location.href, {
      method: "GET",
      headers: { "Accept": "text/html" },
      // Short timeout to avoid blocking the loop for too long
      signal: AbortSignal.timeout ? AbortSignal.timeout(10000) : undefined,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (html) {
        // Parse the fetched HTML and extract #panel-grid
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, "text/html");
        var newGrid = doc.getElementById("panel-grid");
        var currentGrid = document.getElementById("panel-grid");

        if (newGrid && currentGrid) {
          currentGrid.innerHTML = newGrid.innerHTML;
        }
      })
      .catch(function () {
        // Network error or timeout — leave current DOM intact
      });
  }

  function startPolling() {
    if (isPolling) return;
    isPolling = true;
    timerId = setInterval(refreshDashboard, pollMs);
  }

  function stopPolling() {
    if (!isPolling) return;
    isPolling = false;
    if (timerId !== null) {
      clearInterval(timerId);
      timerId = null;
    }
  }

  // ── Page Visibility API ─────────────────────────────────────────────────
  /**
   * When the user switches away (hidden), stop polling entirely.
   * When they return (visible), refresh immediately then restart the timer.
   * This is the primary mechanism that keeps CPU≈0 when unattended (REQ-001).
   */
  function handleVisibilityChange() {
    if (document.hidden) {
      stopPolling();
    } else {
      // Immediately refresh so the user sees current data on return
      refreshDashboard();
      startPolling();
    }
  }

  document.addEventListener("visibilitychange", handleVisibilityChange);

  // ── Bootstrap ───────────────────────────────────────────────────────────
  // Only start polling if the Visibility API reports we are visible right now.
  // (If the tab was opened in the background, we wait until it becomes visible.)
  if (!document.hidden) {
    startPolling();
  }
})();

// ── TASK-022: Tailscale node grid polling (ARCH-003) ─────────────────────
//
// Polls GET /api/tailscale/nodes every 45s and applies partial DOM updates
// to the existing node cards (class + symbol + stale mark only — no full rebuild).
//
// Page Visibility: mirrors the ARCH-001 outer loop.
// Graceful degradation: any fetch or JSON-parse error is silently swallowed;
// the SSR-rendered HTML remains visible until the next successful poll.
(function () {
  "use strict";

  var TAILSCALE_POLL_MS = 45000; // 45 s — ARCH-001 default
  var tailscaleTimerId = null;
  var tailscalePolling = false;

  // ── Helpers ──────────────────────────────────────────────────────────────

  /** Map online_state to a CSS status class. */
  function stateToClass(state) {
    if (state === "ONLINE")  return "status-ok";
    if (state === "OFFLINE") return "status-warn";
    return "status-error";
  }

  /** Map online_state to shape symbol (three-layer encoding). */
  function stateToSymbol(state) {
    if (state === "ONLINE")  return "●"; // ●
    if (state === "OFFLINE") return "◐"; // ◐
    return "○";                          // ○
  }

  // ── DOM update ───────────────────────────────────────────────────────────

  /**
   * Apply partial DOM updates — only change class/textContent/data-attribute
   * and the stale mark.  Do NOT rebuild DOM to avoid layout flicker.
   */
  function renderNodeGrid(nodes) {
    var grid = document.getElementById("node-grid");
    if (!grid) return;

    // Build id→node index for O(1) lookup
    var nodeMap = {};
    for (var i = 0; i < nodes.length; i++) {
      nodeMap[String(nodes[i].id)] = nodes[i];
    }

    var cards = grid.querySelectorAll(".node-card[data-node-id]");
    for (var j = 0; j < cards.length; j++) {
      var card = cards[j];
      var nodeId = card.getAttribute("data-node-id");
      var node = nodeMap[nodeId];
      if (!node) continue;

      // Update data-state attribute (used for CSS opacity on LONG_OFFLINE)
      card.setAttribute("data-state", node.online_state);

      // Update status-dot class and symbol
      var dot = card.querySelector(".status-dot");
      if (dot) {
        dot.className = "status-dot " + stateToClass(node.online_state);
        dot.textContent = stateToSymbol(node.online_state);
      }

      // Add / remove stale-mark element
      var staleEl = card.querySelector(".node-card__stale-mark");
      if (node.is_stale && !staleEl) {
        var mark = document.createElement("div");
        mark.className = "node-card__stale-mark";
        mark.setAttribute("aria-label", "数据已过时"); // 数据已过时
        mark.textContent = "◌"; // ◌
        card.appendChild(mark);
      } else if (!node.is_stale && staleEl) {
        staleEl.parentNode.removeChild(staleEl);
      }
    }
  }

  // ── Polling ──────────────────────────────────────────────────────────────

  function refreshTailscaleNodes() {
    fetch("/api/tailscale/nodes", {
      method: "GET",
      headers: { "Accept": "application/json" },
      signal: AbortSignal.timeout ? AbortSignal.timeout(10000) : undefined,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (nodes) {
        renderNodeGrid(nodes);
      })
      .catch(function () {
        // Network / parse error — leave current DOM intact until next poll.
      });
  }

  function startTailscalePoll() {
    if (tailscalePolling) return;
    tailscalePolling = true;
    tailscaleTimerId = setInterval(refreshTailscaleNodes, TAILSCALE_POLL_MS);
  }

  function stopTailscalePoll() {
    if (!tailscalePolling) return;
    tailscalePolling = false;
    if (tailscaleTimerId !== null) {
      clearInterval(tailscaleTimerId);
      tailscaleTimerId = null;
    }
  }

  // ── Page Visibility integration (mirrors outer ARCH-001 loop) ───────────
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopTailscalePoll();
    } else {
      refreshTailscaleNodes(); // immediate refresh on tab return
      startTailscalePoll();
    }
  });

  // ── Bootstrap ────────────────────────────────────────────────────────────
  if (!document.hidden) {
    startTailscalePoll();
  }
})();

// ── TASK-015: Azure VM + GPU dashboard polling ───────────────────────────
// Polls GET /api/v1/dashboard/azure every 45 s (ARCH-001 default interval).
// Page Visibility is handled by the outer refreshDashboard() loop above via
// full-page HTML refresh; this dedicated fetch updates only the Azure section
// on JSON data changes for a faster, flicker-free update.
//
// Graceful degradation: any error (network/JSON parse) is silently swallowed
// — the SSR-rendered HTML remains until the next successful poll.
(function () {
  "use strict";

  var AZURE_POLL_MS = 45000; // 45 s — ARCH-001 default
  var azureTimerId = null;
  var azurePolling = false;

  // ── Rendering helpers ────────────────────────────────────────────────────

  /** Map power_state + is_stale to a CSS class suffix. */
  function vmStatusClass(vm) {
    if (vm.is_stale) return "stale";
    switch (vm.power_state) {
      case "Running":                        return "ok";
      case "Starting":
      case "Stopping":
      case "Deallocating":
      case "Stopped":
      case "Deallocated":                    return "warn";
      default:                               return "error";
    }
  }

  /** Three-layer shape symbol for the VM status dot. */
  function vmStatusSymbol(vm) {
    switch (vmStatusClass(vm)) {
      case "ok":    return "●"; // ●
      case "warn":  return "◐"; // ◐
      case "stale": return "◌"; // ◌
      default:      return "○"; // ○
    }
  }

  /** Map GPU util% to bar-* CSS class. */
  function utilThresholdClass(pct) {
    if (pct >= 90) return "bar-critical";
    if (pct >= 70) return "bar-warn";
    return "bar-ok";
  }

  /** Map GPU mem% to bar-* CSS class. */
  function memThresholdClass(pct) {
    if (pct === null || pct === undefined) return "";
    if (pct >= 90) return "bar-critical";
    if (pct >= 75) return "bar-warn";
    return "bar-ok";
  }

  function escHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function round1(n) { return Math.round(n * 10) / 10; }

  /** Build the inner HTML for a single .gpu-card. */
  function buildGpuCardHtml(gpu) {
    var staleClass = gpu.is_stale ? " gpu-stale" : "";
    var html = '<div class="gpu-card' + staleClass + '" data-gpu-index="' + gpu.gpu_index + '">';
    html += '<div class="gpu-label">GPU ' + gpu.gpu_index;
    if (gpu.gpu_name) {
      html += ' <span class="gpu-name-small">' + escHtml(gpu.gpu_name) + '</span>';
    }
    html += '</div>';

    if (gpu.util_pct !== null && gpu.util_pct !== undefined) {
      // Util bar
      var utilClass = utilThresholdClass(gpu.util_pct);
      html += '<div class="metric-bar-row">';
      html += '<span class="metric-label">算力</span>';
      html += '<div class="metric-bar"><div class="metric-bar__fill bar-fill ' + utilClass + '"';
      html += ' style="--pct:' + round1(gpu.util_pct) + '%"></div></div>';
      html += '<span class="metric-value">' + round1(gpu.util_pct) + '%</span>';
      html += '</div>';

      // Mem bar
      if (gpu.mem_pct !== null && gpu.mem_pct !== undefined) {
        var memClass = memThresholdClass(gpu.mem_pct);
        var memLabel = (gpu.mem_used_mib !== null && gpu.mem_total_mib !== null)
          ? round1(gpu.mem_used_mib / 1024) + "G / " + round1(gpu.mem_total_mib / 1024) + "G"
          : round1(gpu.mem_pct) + "%";
        html += '<div class="metric-bar-row">';
        html += '<span class="metric-label">显存</span>';
        html += '<div class="metric-bar"><div class="metric-bar__fill bar-fill ' + memClass + '"';
        html += ' style="--pct:' + round1(gpu.mem_pct) + '%"></div></div>';
        html += '<span class="metric-value">' + escHtml(memLabel) + '</span>';
        html += '</div>';
      }

      // Temp / power
      var meta = [];
      if (gpu.temp_c !== null && gpu.temp_c !== undefined) meta.push(Math.round(gpu.temp_c) + "°C");
      if (gpu.power_w !== null && gpu.power_w !== undefined) meta.push(Math.round(gpu.power_w) + "W");
      if (meta.length) {
        html += '<div class="gpu-meta-row">' + meta.map(escHtml).join('<span>') + '</div>';
      }
    } else {
      html += '<div class="gpu-unreachable"><span class="status-dot status-error" aria-hidden="true">○</span> 不可达</div>';
    }

    html += '</div>';
    return html;
  }

  /** Build the inner HTML for a single .vm-card article. */
  function buildVmCardHtml(vm) {
    var cls = vmStatusClass(vm);
    var sym = vmStatusSymbol(vm);
    var html = '<article class="vm-card" data-server-id="' + vm.server_id + '">';

    // Header
    html += '<header class="vm-header">';
    html += '<span class="status-dot status-' + cls + '" aria-label="' + escHtml(vm.power_state) + '">' + sym + '</span>';
    html += '<span class="vm-name">' + escHtml(vm.name) + '</span>';
    html += '<span class="vm-state-label">' + escHtml(vm.power_state) + '</span>';
    if (vm.is_stale) {
      html += '<span class="stale-badge" title="采集时间超出阈值，数据可能不准确">⚠ 陈旧</span>';
    }
    html += '</header>';

    // Meta
    html += '<dl class="vm-meta"><dt>资源组</dt><dd>' + escHtml(vm.azure_resource_group || "—") + '</dd></dl>';

    // GPU list
    if (vm.gpus && vm.gpus.length) {
      html += '<div class="gpu-list">';
      for (var i = 0; i < vm.gpus.length; i++) {
        html += buildGpuCardHtml(vm.gpus[i]);
      }
      html += '</div>';
    }

    html += '</article>';
    return html;
  }

  /**
   * Update [data-module="azure"] with fresh dashboard data.
   * Strategy: update each existing .vm-card in-place by server_id;
   * append new VMs; remove cards for deleted VMs.
   * If the section doesn't exist yet (first render before SSR), do nothing.
   */
  function renderAzureDashboard(data) {
    var section = document.querySelector('[data-module="azure"]');
    if (!section) return;

    var vms = data.vms || [];
    var seenIds = {};

    for (var i = 0; i < vms.length; i++) {
      var vm = vms[i];
      var sid = String(vm.server_id);
      seenIds[sid] = true;

      var existing = section.querySelector('.vm-card[data-server-id="' + sid + '"]');
      if (existing) {
        // Replace entire card HTML to pick up all state changes.
        var tmp = document.createElement("div");
        tmp.innerHTML = buildVmCardHtml(vm);
        section.replaceChild(tmp.firstChild, existing);
      } else {
        var tmp2 = document.createElement("div");
        tmp2.innerHTML = buildVmCardHtml(vm);
        section.appendChild(tmp2.firstChild);
      }
    }

    // Remove cards for VMs no longer in the response.
    var allCards = section.querySelectorAll(".vm-card");
    for (var j = 0; j < allCards.length; j++) {
      if (!seenIds[allCards[j].dataset.serverId]) {
        section.removeChild(allCards[j]);
      }
    }
  }

  // ── Polling ──────────────────────────────────────────────────────────────

  function refreshAzure() {
    fetch("/api/v1/dashboard/azure", {
      method: "GET",
      headers: { "Accept": "application/json" },
      signal: AbortSignal.timeout ? AbortSignal.timeout(10000) : undefined,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        renderAzureDashboard(data);
      })
      .catch(function () {
        // Network / parse error — leave current DOM intact until next poll.
      });
  }

  function startAzurePolling() {
    if (azurePolling) return;
    azurePolling = true;
    azureTimerId = setInterval(refreshAzure, AZURE_POLL_MS);
  }

  function stopAzurePolling() {
    if (!azurePolling) return;
    azurePolling = false;
    if (azureTimerId !== null) {
      clearInterval(azureTimerId);
      azureTimerId = null;
    }
  }

  // ── Page Visibility integration (mirrors outer ARCH-001 loop) ───────────
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopAzurePolling();
    } else {
      refreshAzure();       // immediate refresh on tab return
      startAzurePolling();
    }
  });

  // ── Bootstrap ─────────────────────────────────────────────────────────
  if (!document.hidden) {
    startAzurePolling();
  }
})();

// ── TASK-033: AI 额度卡片 — resets_at 倒计时 + 阈值变色属性 (ARCH-004) ──────
//
// 1. resets_at 倒计时：找所有 [data-countdown] 元素，每分钟刷新显示
//    「Xh Ym 后重置」；到期显示「已重置」。
// 2. 阈值变色属性：根据进度条 aria-valuenow 在 section 上动态加 data-pct-warn
//    (>70) / data-pct-error (>90)，配合 CSS @media (color) 变色。
// 3. Page Visibility：页面不可见时暂停倒计时（CPU≈0，REQ-001），可见时立即刷新。
//
// 命名以 aiUsage 前缀，避免与上方 azure / tailscale 模块的同名变量冲突。
// Graceful degradation：无任何 AI 卡片时不做任何事。
(function () {
  "use strict";

  var AI_COUNTDOWN_MS = 60000; // 1 min
  var aiCountdownTimerId = null;
  var aiCountdownRunning = false;

  /** 把毫秒差格式化为「Xh Ym 后重置」/「Ym 后重置」/「已重置」。 */
  function aiUsageFormatRemaining(diffMs) {
    if (diffMs <= 0) return "已重置";
    var totalMin = Math.floor(diffMs / 60000);
    var h = Math.floor(totalMin / 60);
    var m = totalMin % 60;
    if (h > 0) return h + "h " + m + "m 后重置";
    return m + "m 后重置";
  }

  /** 刷新所有倒计时元素的显示文本。 */
  function aiUsageUpdateCountdowns() {
    var els = document.querySelectorAll("[data-countdown]");
    for (var i = 0; i < els.length; i++) {
      var raw = els[i].dataset.countdown;
      var t = new Date(raw);
      if (isNaN(t.getTime())) continue; // 无法解析则保留 SSR 文本
      els[i].textContent = aiUsageFormatRemaining(t.getTime() - Date.now());
    }
  }

  /**
   * 依据进度条 aria-valuenow 在所属 AI 卡片 section 上标注 data-pct-warn/error。
   * SSR 已根据后端值预置这两个属性；此处在客户端再校准一次，保证 JS 渲染/
   * 局部刷新后阈值变色仍正确。
   */
  function aiUsageApplyThresholdAttrs() {
    var bars = document.querySelectorAll(
      '[data-module="ai-usage"] .ai-metric-bar[aria-valuenow]'
    );
    for (var i = 0; i < bars.length; i++) {
      var section = bars[i].closest('[data-module="ai-usage"]');
      if (!section) continue;
      var pct = parseFloat(bars[i].getAttribute("aria-valuenow"));
      section.removeAttribute("data-pct-warn");
      section.removeAttribute("data-pct-error");
      if (!isNaN(pct)) {
        if (pct >= 90) {
          section.setAttribute("data-pct-error", "");
        } else if (pct >= 70) {
          section.setAttribute("data-pct-warn", "");
        }
      }
    }
  }

  function aiUsageTick() {
    aiUsageUpdateCountdowns();
    aiUsageApplyThresholdAttrs();
  }

  function startAiCountdown() {
    if (aiCountdownRunning) return;
    aiCountdownRunning = true;
    aiCountdownTimerId = setInterval(aiUsageUpdateCountdowns, AI_COUNTDOWN_MS);
  }

  function stopAiCountdown() {
    if (!aiCountdownRunning) return;
    aiCountdownRunning = false;
    if (aiCountdownTimerId !== null) {
      clearInterval(aiCountdownTimerId);
      aiCountdownTimerId = null;
    }
  }

  // ── Page Visibility integration (mirrors outer ARCH-001 loop) ───────────
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopAiCountdown();
    } else {
      aiUsageTick(); // immediate refresh on tab return
      startAiCountdown();
    }
  });

  // ── Bootstrap ─────────────────────────────────────────────────────────
  aiUsageTick(); // initial render (also re-derives threshold attrs)
  if (!document.hidden) {
    startAiCountdown();
  }
})();

// ── TASK-017: GPU 历史趋势迷你图 (ARCH-002) ───────────────────────────────
//
// 每个 GpuCard 内有一个 <details class="gpu-trend">，默认折叠。用户展开时
// (toggle 事件且 details.open=true) 才懒加载 GET /api/v1/gpu/{id}/{idx}/history
// 并用纯 Canvas 2D 画一条折线图——无外部库、无动画 (一次性绘制即止)。
//
// e-ink 降级：prefers-color-scheme: no-preference 时退化为单色黑线，
// 不依赖颜色传递信息；阈值额外用线宽区分。
//
// 命名一律 gpuTrend 前缀，避免与上方 azure / tailscale / aiUsage 模块冲突。
// 优雅降级：任意 fetch/解析错误都吞掉并显示「加载失败」，SSR DOM 不受影响。
(function () {
  "use strict";

  var GPU_TREND_GRANULARITY = "5m";
  var GPU_TREND_LIMIT = 144; // 12h × 12 个 5min 桶
  var GPU_TREND_FIELD = "avg_util_pct";

  /** 是否处于 e-ink / 灰度降级模式（无颜色偏好）。 */
  function gpuTrendIsEink() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: no-preference)").matches
    );
  }

  /**
   * 根据利用率阈值返回 {color, width}。
   * e-ink 模式：颜色恒为黑，靠线宽区分 (>90 粗 / >70 中 / 其他细)。
   * 彩色模式：>90 红、>70 橙、其他绿；线宽固定 1.5。
   */
  function gpuTrendStrokeFor(maxVal, eink) {
    if (eink) {
      var w = 1;
      if (maxVal >= 90) w = 2.5;
      else if (maxVal >= 70) w = 1.75;
      return { color: "#000", width: w };
    }
    var color = "#2e7d32"; // 绿
    if (maxVal >= 90) color = "#c62828"; // 红
    else if (maxVal >= 70) color = "#ef6c00"; // 橙
    return { color: color, width: 1.5 };
  }

  /**
   * 纯 Canvas 2D 折线图。无外部依赖、无 requestAnimationFrame 循环、无动画。
   * 空数组 / 全空字段 → 不绘制，交由调用方显示「暂无历史数据」。
   *
   * @param {HTMLCanvasElement} canvas
   * @param {Array} points  history API 返回的数据点
   * @param {string} field  取哪个字段（默认 avg_util_pct）
   * @returns {boolean}  true=已绘制；false=无可绘制数据
   */
  function gpuTrendDrawMiniChart(canvas, points, field) {
    if (!canvas || typeof canvas.getContext !== "function") return false;
    var ctx = canvas.getContext("2d");
    if (!ctx) return false;

    var key = field || GPU_TREND_FIELD;

    // 宽度自适应容器；高度固定。
    var parent = canvas.parentNode;
    var w = (parent && parent.offsetWidth) || canvas.width || 280;
    var h = canvas.height || 60;
    canvas.width = w;
    canvas.height = h;

    ctx.clearRect(0, 0, w, h);

    if (!points || !points.length) return false;

    // 抽取有效数值（百分比 0–100），保留索引以等距铺开 x。
    var vals = [];
    for (var i = 0; i < points.length; i++) {
      var v = points[i] ? points[i][key] : null;
      vals.push(typeof v === "number" && isFinite(v) ? v : null);
    }
    var hasAny = false;
    var maxVal = 0;
    for (var j = 0; j < vals.length; j++) {
      if (vals[j] !== null) {
        hasAny = true;
        if (vals[j] > maxVal) maxVal = vals[j];
      }
    }
    if (!hasAny) return false;

    var pad = 2;
    var plotH = h - pad * 2;
    var n = vals.length;
    // 固定 0–100 百分比刻度，避免单点时除零。
    function xAt(idx) {
      return n <= 1 ? w / 2 : pad + (idx / (n - 1)) * (w - pad * 2);
    }
    function yAt(val) {
      var clamped = val < 0 ? 0 : val > 100 ? 100 : val;
      return pad + (1 - clamped / 100) * plotH;
    }

    var eink = gpuTrendIsEink();
    var stroke = gpuTrendStrokeFor(maxVal, eink);

    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.lineWidth = stroke.width;
    ctx.strokeStyle = stroke.color;

    ctx.beginPath();
    var started = false;
    for (var k = 0; k < n; k++) {
      if (vals[k] === null) continue; // 跳过缺口（断线）
      var x = xAt(k);
      var y = yAt(vals[k]);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
    return true;
  }

  /** 构造 history API URL。 */
  function gpuTrendUrl(serverId, gpuIndex) {
    return (
      "/api/v1/gpu/" +
      encodeURIComponent(serverId) +
      "/" +
      encodeURIComponent(gpuIndex) +
      "/history?granularity=" +
      GPU_TREND_GRANULARITY +
      "&limit=" +
      GPU_TREND_LIMIT
    );
  }

  /** 在某个 details 块内设置状态文本（加载中 / 暂无 / 失败）。 */
  function gpuTrendSetStatus(details, text) {
    var status = details.querySelector(".gpu-trend-status");
    if (status) {
      status.textContent = text;
      status.hidden = false;
    }
  }

  function gpuTrendHideStatus(details) {
    var status = details.querySelector(".gpu-trend-status");
    if (status) status.hidden = true;
  }

  /**
   * 处理单个 details 的 toggle：仅在展开 (open) 且尚未加载过时拉取数据。
   * 每张卡各自独立——只操作自身 details 内的 canvas / 状态节点。
   */
  function gpuTrendHandleToggle(details) {
    if (!details.open) return;
    if (details.dataset.gpuTrendLoaded === "1") return; // 只加载一次
    details.dataset.gpuTrendLoaded = "1";

    var serverId = details.dataset.serverId;
    var gpuIndex = details.dataset.gpuIndex;
    var canvas = details.querySelector("canvas.trend-canvas");

    gpuTrendSetStatus(details, "加载中…");

    fetch(gpuTrendUrl(serverId, gpuIndex), {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout ? AbortSignal.timeout(10000) : undefined,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        var drawn = gpuTrendDrawMiniChart(canvas, data, GPU_TREND_FIELD);
        if (drawn) {
          gpuTrendHideStatus(details);
        } else {
          gpuTrendSetStatus(details, "暂无历史数据");
        }
      })
      .catch(function () {
        // 允许下次重新展开时再试
        details.dataset.gpuTrendLoaded = "";
        gpuTrendSetStatus(details, "加载失败");
      });
  }

  /** 给所有 .gpu-trend 绑定一次 toggle 监听（幂等：跳过已绑定的）。 */
  function gpuTrendBindAll() {
    var blocks = document.querySelectorAll(".gpu-trend");
    for (var i = 0; i < blocks.length; i++) {
      var details = blocks[i];
      if (details.dataset.gpuTrendBound === "1") continue;
      details.dataset.gpuTrendBound = "1";
      details.addEventListener(
        "toggle",
        (function (d) {
          return function () {
            gpuTrendHandleToggle(d);
          };
        })(details)
      );
    }
  }

  // 暴露绘制函数供测试与潜在复用（不影响 IIFE 封装的其余内部状态）。
  window.gpuTrendDrawMiniChart = gpuTrendDrawMiniChart;

  // ── Bootstrap ─────────────────────────────────────────────────────────
  gpuTrendBindAll();
})();
