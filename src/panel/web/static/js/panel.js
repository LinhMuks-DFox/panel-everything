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
