"""TASK-017: 前端 GPU 历史趋势迷你图测试 (ARCH-002).

覆盖三层：

SSR 模板渲染 (_vm_card.html via GET /，注入 azure_dashboard)：
  - test_trend_details_present_and_collapsed
        : GpuCard 内有 <details class="gpu-trend">，默认折叠（无 open 属性），
          且携带 data-server-id / data-gpu-index 与 <canvas class="trend-canvas">。
  - test_trend_multiple_cards_independent
        : 两张 GPU 卡各自渲染独立的 details/canvas，data-* 互不相同。
  - test_trend_absent_when_gpu_unreachable
        : util_pct=None 的不可达 GPU 不渲染趋势块（趋势属于有数据 GPU）。

静态资产检查 (panel.js / panel.css)：
  - test_js_appended_iife_uses_gpu_trend_prefix
        : panel.js 末尾 IIFE 用 gpuTrend 前缀，且未触碰 aiUsage/azure/tailscale 段。
  - test_js_toggle_lazy_loads_correct_api_url
        : JS 构造的 history URL 形如 /api/v1/gpu/{id}/{idx}/history?granularity=5m&limit=144。
  - test_js_no_external_chart_lib / test_js_no_animation_in_canvas
        : 无外部图表库、无 requestAnimationFrame 动画循环。
  - test_js_eink_monochrome_degradation
        : e-ink 分支用单色黑线 (#000) + prefers-color-scheme: no-preference。
  - test_css_gpu_trend_section_exists / _no_box_shadow / _no_animation
        : panel.css 有 GPU 趋势段，且无 box-shadow / animation / @keyframes / transition。

JS 运行时行为 (通过 node 子进程执行真实 drawMiniChart)：
  - test_draw_mini_chart_empty_array_no_crash
        : 空数组输入返回 false（不绘制）且不抛异常。
  - test_draw_mini_chart_plots_nonempty
        : 非空数据返回 true 且发生 stroke() 调用。
  - test_draw_mini_chart_eink_uses_black_stroke
        : e-ink 模式下 strokeStyle 为黑色（颜色不传递信息）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from panel.main import create_app

_ROOT = Path(__file__).parent.parent
_STATIC_DIR = _ROOT / "src" / "panel" / "web" / "static"
_JS_PATH = _STATIC_DIR / "js" / "panel.js"
_CSS_PATH = _STATIC_DIR / "css" / "panel.css"
_TEMPLATES_DIR = _ROOT / "src" / "panel" / "web" / "templates"
_VM_CARD_PATH = _TEMPLATES_DIR / "partials" / "_vm_card.html"


# --------------------------------------------------------------------------- #
# Shared SSR helpers (mirror tests/test_frontend_vm_gpu.py conventions)
# --------------------------------------------------------------------------- #


async def _get(app, path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


def _make_gpu(
    *,
    server_id: int = 1,
    gpu_index: int = 0,
    gpu_name: str | None = "NVIDIA A100",
    util_pct: float | None = 80.0,
    mem_used_mib: float | None = 40960.0,
    mem_total_mib: float | None = 81920.0,
    mem_pct: float | None = 50.0,
    temp_c: float | None = 70.0,
    power_w: float | None = 300.0,
    is_stale: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name=gpu_name,
        util_pct=util_pct,
        mem_used_mib=mem_used_mib,
        mem_total_mib=mem_total_mib,
        mem_pct=mem_pct,
        temp_c=temp_c,
        power_w=power_w,
        is_stale=is_stale,
    )


def _make_vm(*, server_id: int = 1, gpus: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=server_id,
        name="gpu-vm-01",
        power_state="Running",
        is_stale=False,
        is_running=True,
        azure_resource_group="lab-rg",
        azure_vm_name="gpu-vm-01",
        gpus=gpus or [],
    )


def _make_dashboard(vms: list | None = None) -> SimpleNamespace:
    from panel.domain.models import CollectorStatusOut

    cs = {
        "azure_vm": CollectorStatusOut(
            status="up", last_ran_at=datetime.now(UTC), error=None
        ),
        "gpu": CollectorStatusOut(
            status="up", last_ran_at=datetime.now(UTC), error=None
        ),
    }
    return SimpleNamespace(
        fetched_at=datetime.now(UTC), collector_status=cs, vms=vms or []
    )


async def _render(vms, monkeypatch) -> str:
    """GET / with build_azure_dashboard patched to return our dashboard."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(vms=vms)
    app = create_app()
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app.state.repo = mock_repo
    app.state.gpu_repo = MagicMock()
    resp = await _get(app, "/")
    assert resp.status_code == 200
    return resp.text


# --------------------------------------------------------------------------- #
# 1. SSR 模板渲染
# --------------------------------------------------------------------------- #


async def test_trend_details_present_and_collapsed(monkeypatch: pytest.MonkeyPatch):
    """趋势块默认折叠：<details class="gpu-trend"> 无 open 属性，带 data-* + canvas。"""
    gpu = _make_gpu(server_id=7, gpu_index=3, util_pct=55.0)
    body = await _render([_make_vm(server_id=7, gpus=[gpu])], monkeypatch)

    assert 'class="gpu-trend"' in body
    assert 'data-server-id="7"' in body
    assert 'data-gpu-index="3"' in body
    assert "trend-canvas" in body
    assert "历史趋势" in body

    # 默认折叠：渲染出的 <details ...> 起始标签不得带 open 属性。
    for m in re.finditer(r"<details\b[^>]*class=\"gpu-trend\"[^>]*>", body):
        assert " open" not in m.group(0), "趋势 details 不应默认展开"


async def test_trend_multiple_cards_independent(monkeypatch: pytest.MonkeyPatch):
    """两张 GPU 卡各自独立的 details / canvas，data-gpu-index 互不相同。"""
    gpus = [
        _make_gpu(server_id=1, gpu_index=0, util_pct=20.0),
        _make_gpu(server_id=1, gpu_index=1, util_pct=95.0),
    ]
    body = await _render([_make_vm(server_id=1, gpus=gpus)], monkeypatch)

    # 两个独立的 gpu-trend details
    assert len(re.findall(r'class="gpu-trend"', body)) == 2
    assert len(re.findall(r"trend-canvas", body)) == 2
    assert 'data-gpu-index="0"' in body
    assert 'data-gpu-index="1"' in body


async def test_trend_absent_when_gpu_unreachable(monkeypatch: pytest.MonkeyPatch):
    """util_pct=None 的不可达 GPU 不渲染趋势块（趋势只属于有数据 GPU）。"""
    gpu = _make_gpu(util_pct=None, mem_pct=None)
    body = await _render([_make_vm(gpus=[gpu])], monkeypatch)
    assert "不可达" in body
    assert 'class="gpu-trend"' not in body


def test_vm_card_partial_has_gpu_trend_block():
    """_vm_card.html 源文件含 gpu-trend details + trend-canvas + 懒加载 summary。"""
    content = _VM_CARD_PATH.read_text(encoding="utf-8")
    assert 'class="gpu-trend"' in content
    assert "gpu-trend-toggle" in content
    assert "trend-canvas" in content
    assert "data-server-id" in content
    assert "data-gpu-index" in content


# --------------------------------------------------------------------------- #
# 2. panel.js 静态资产检查
# --------------------------------------------------------------------------- #


def _read_js() -> str:
    return _JS_PATH.read_text(encoding="utf-8")


def _gpu_trend_js_segment() -> str:
    """末尾追加的 TASK-017 段（从 TASK-017 标记到文件结尾）。"""
    js = _read_js()
    idx = js.find("TASK-017")
    assert idx != -1, "panel.js 未找到 TASK-017 段标记"
    return js[idx:]


def _gpu_trend_js_code() -> str:
    """TASK-017 段去注释后的纯代码（避免注释里的英文字样被误判）。"""
    seg = _gpu_trend_js_segment()
    seg = re.sub(r"/\*.*?\*/", "", seg, flags=re.DOTALL)  # 块注释
    seg = re.sub(r"//[^\n]*", "", seg)  # 行注释
    return seg


def test_js_appended_iife_uses_gpu_trend_prefix():
    """新增段全部以 gpuTrend 前缀命名，且未改动既有 aiUsage/azure/tailscale 段。"""
    seg = _gpu_trend_js_segment()
    # 新段必须定义这些 gpuTrend* 标识符
    for name in (
        "gpuTrendDrawMiniChart",
        "gpuTrendHandleToggle",
        "gpuTrendBindAll",
        "gpuTrendUrl",
        "gpuTrendIsEink",
    ):
        assert name in seg, f"缺少 {name}"

    # 新段不得引用其他模块的私有标识符（避免触碰既有段）。
    for foreign in (
        "aiUsageTick",
        "renderAzureDashboard",
        "renderNodeGrid",
        "refreshAzure",
        "refreshTailscaleNodes",
    ):
        assert foreign not in seg, f"新段不应引用既有段标识符 {foreign}"

    # 既有段标记仍在（顺序：tailscale → azure → ai-usage → gpu-trend）。
    js = _read_js()
    assert js.index("TASK-022") < js.index("TASK-015") < js.index(
        "TASK-033"
    ) < js.index("TASK-017")


def test_js_toggle_lazy_loads_correct_api_url():
    """URL 构造命中 /api/v1/gpu/{id}/{idx}/history?granularity=5m&limit=144。"""
    seg = _gpu_trend_js_segment()
    assert "/api/v1/gpu/" in seg
    assert "/history?granularity=" in seg
    assert '"5m"' in seg  # GPU_TREND_GRANULARITY
    assert "144" in seg  # GPU_TREND_LIMIT
    # 懒加载：toggle 事件 + 仅在 details.open 时拉取
    assert '"toggle"' in seg
    assert "details.open" in seg
    # 仅加载一次的去重标记
    assert "gpuTrendLoaded" in seg


def test_js_no_external_chart_lib():
    """无外部图表库依赖（纯 Canvas 2D）。"""
    code = _gpu_trend_js_code().lower()
    for lib in ("chart.js", "echarts", "import ", "require("):
        assert lib not in code, f"不应依赖外部库 {lib}"
    # d3 用单词边界匹配，避免命中 hex 颜色 (#2e7d32) 之类。
    assert re.search(r"\bd3\b", code) is None, "不应依赖外部库 d3"
    assert "getcontext" in code  # 用原生 canvas 2d


def test_js_no_animation_in_canvas():
    """Canvas 一次性绘制——无 requestAnimationFrame / setInterval 动画循环。"""
    code = _gpu_trend_js_code()
    assert "requestAnimationFrame" not in code
    # 新段不得引入定时器（动画/轮询）。
    assert "setInterval" not in code
    assert "setTimeout" not in code


def test_js_eink_monochrome_degradation():
    """e-ink 降级：检测 prefers-color-scheme: no-preference 并退化为黑色单线。"""
    seg = _gpu_trend_js_segment()
    assert "prefers-color-scheme: no-preference" in seg
    assert "#000" in seg  # 黑色单线
    # 颜色 + 线宽双通道：阈值变化时调整 width（不仅依赖颜色）。
    assert "width" in seg


def test_js_graceful_degradation_on_fetch_error():
    """fetch 错误被吞掉并显示「加载失败」，允许重试（清空 loaded 标记）。"""
    seg = _gpu_trend_js_segment()
    assert ".catch(" in seg
    assert "加载失败" in seg
    assert "加载中" in seg
    assert "暂无历史数据" in seg


# --------------------------------------------------------------------------- #
# 3. panel.css 静态检查
# --------------------------------------------------------------------------- #


def _read_gpu_trend_css() -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    m = re.search(r"── GPU 趋势 ──.*?End GPU 趋势", css, re.DOTALL)
    return m.group(0) if m else ""


def test_css_gpu_trend_section_exists():
    assert _read_gpu_trend_css(), "panel.css 缺少 GPU 趋势段标记"


def test_css_gpu_trend_no_box_shadow():
    section = _read_gpu_trend_css()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\bbox-shadow\s*:", no_comments) is None


def test_css_gpu_trend_no_animation():
    section = _read_gpu_trend_css()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\banimation\s*:", no_comments) is None
    assert re.search(r"@keyframes\b", no_comments) is None
    assert re.search(r"\btransition\s*:", no_comments) is None


def test_css_defines_trend_canvas_and_toggle():
    section = _read_gpu_trend_css()
    assert ".gpu-trend" in section
    assert ".trend-canvas" in section
    assert ".gpu-trend-toggle" in section


# --------------------------------------------------------------------------- #
# 4. JS 运行时行为（node 子进程执行真实 gpuTrendDrawMiniChart）
# --------------------------------------------------------------------------- #

_NODE = shutil.which("node")

# 提取 IIFE 内的 gpuTrendDrawMiniChart + 其依赖 (gpuTrendIsEink / gpuTrendStrokeFor)
# 在 node 下运行。我们用一段 harness：注入 window.matchMedia + 一个记录调用的
# 假 canvas/2d-context，然后 eval 真实函数源码并断言行为。
_HARNESS_TEMPLATE = r"""
'use strict';
// ── 假 DOM 环境 ──────────────────────────────────────────────────────────
const EINK = %EINK%;
global.window = {
  matchMedia: function (q) {
    return { matches: EINK && q.indexOf('no-preference') !== -1 };
  },
};

let strokeCalls = 0;
let lastStrokeStyle = null;
let lastLineWidth = null;
const ctx = {
  clearRect() {},
  beginPath() {},
  moveTo() {},
  lineTo() {},
  stroke() { strokeCalls++; },
  set strokeStyle(v) { lastStrokeStyle = v; },
  get strokeStyle() { return lastStrokeStyle; },
  set lineWidth(v) { lastLineWidth = v; },
  get lineWidth() { return lastLineWidth; },
  set lineJoin(v) {},
  set lineCap(v) {},
};
const canvas = {
  width: 280,
  height: 60,
  parentNode: { offsetWidth: 280 },
  getContext() { return ctx; },
};

// ── 被测函数源码（从 panel.js 提取） ──────────────────────────────────────
%FUNCS%

// ── 执行用例 ──────────────────────────────────────────────────────────────
const input = %INPUT%;
let result, threw = false, errMsg = null;
try {
  result = gpuTrendDrawMiniChart(canvas, input, 'avg_util_pct');
} catch (e) {
  threw = true;
  errMsg = String(e);
}
console.log(JSON.stringify({
  result: result === true,
  threw: threw,
  errMsg: errMsg,
  strokeCalls: strokeCalls,
  strokeStyle: lastStrokeStyle,
  lineWidth: lastLineWidth,
}));
"""


def _extract_js_funcs() -> str:
    """从 panel.js 抽取运行 drawMiniChart 所需的三个函数源码。"""
    seg = _gpu_trend_js_segment()
    names = ["gpuTrendIsEink", "gpuTrendStrokeFor", "gpuTrendDrawMiniChart"]
    out = []
    for name in names:
        m = re.search(
            r"(function " + re.escape(name) + r"\s*\([^)]*\)\s*\{)", seg
        )
        assert m, f"未能在 panel.js 中定位 {name}"
        start = m.start()
        # 花括号配平找函数体结束
        depth = 0
        i = m.end(1) - 1  # 指向开 {
        while i < len(seg):
            ch = seg[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(seg[start : i + 1])
                    break
            i += 1
        else:  # pragma: no cover - 防御
            raise AssertionError(f"{name} 花括号未配平")
    return "\n".join(out)


def _run_harness(input_points, eink: bool) -> dict:
    harness = (
        _HARNESS_TEMPLATE.replace("%EINK%", "true" if eink else "false")
        .replace("%FUNCS%", _extract_js_funcs())
        .replace("%INPUT%", json.dumps(input_points))
    )
    proc = subprocess.run(  # noqa: S603 — 本地构造脚本,_NODE 为 which 解析的绝对路径,无外部输入
        [_NODE, "-e", harness],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_empty_array_no_crash():
    """空数组输入：不抛异常、返回 false（不绘制）、不调用 stroke。"""
    out = _run_harness([], eink=False)
    assert out["threw"] is False
    assert out["result"] is False
    assert out["strokeCalls"] == 0


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_all_null_no_crash():
    """字段全为 null：不崩溃、返回 false。"""
    pts = [{"avg_util_pct": None}, {"avg_util_pct": None}]
    out = _run_harness(pts, eink=False)
    assert out["threw"] is False
    assert out["result"] is False


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_plots_nonempty():
    """非空数据：返回 true 且发生至少一次 stroke()。"""
    pts = [{"avg_util_pct": 10.0}, {"avg_util_pct": 80.0}, {"avg_util_pct": 95.0}]
    out = _run_harness(pts, eink=False)
    assert out["threw"] is False
    assert out["result"] is True
    assert out["strokeCalls"] >= 1


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_single_point_no_crash():
    """单点数据（n=1，xAt 除零边界）：不崩溃。"""
    out = _run_harness([{"avg_util_pct": 50.0}], eink=False)
    assert out["threw"] is False
    assert out["result"] is True


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_eink_uses_black_stroke():
    """e-ink 模式：strokeStyle 退化为黑色（#000），不依赖颜色传递阈值信息。"""
    pts = [{"avg_util_pct": 95.0}, {"avg_util_pct": 92.0}]
    out = _run_harness(pts, eink=True)
    assert out["result"] is True
    assert out["strokeStyle"] == "#000"


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_draw_mini_chart_colour_mode_uses_threshold_colour():
    """彩色模式：高利用率(≥90) 用红色 #c62828，与 e-ink 黑线区分。"""
    pts = [{"avg_util_pct": 95.0}]
    out = _run_harness(pts, eink=False)
    assert out["result"] is True
    assert out["strokeStyle"] == "#c62828"


# --------------------------------------------------------------------------- #
# 5. 轮询重渲染回归（MS-005 评审 HIGH #1 / #18）
#
# Azure JSON 轮询用 buildGpuCardHtml 整卡重建 .vm-card，且整页 refreshDashboard
# 用 innerHTML 替换 #panel-grid——两条路径都会引入新的 .gpu-trend。回归点：
#   (a) buildGpuCardHtml 必须输出与 SSR 等价的 .gpu-trend/<canvas> 趋势块，
#       否则首次 45s 轮询后趋势块从 DOM 永久消失。
#   (b) window.gpuTrendBindAll 必须被暴露，且两条轮询路径在 DOM 变更后各调用
#       一次，否则新插入的 .gpu-trend 拿不到 toggle 监听（展开无反应）。
# --------------------------------------------------------------------------- #


def _azure_js_segment() -> str:
    """TASK-015 Azure 轮询段（含 buildGpuCardHtml / renderAzureDashboard）。"""
    js = _read_js()
    start = js.find("TASK-015")
    end = js.find("TASK-033")  # Azure 段止于 AI 段开始
    assert start != -1 and end != -1 and start < end
    return js[start:end]


def test_build_gpu_card_emits_trend_block_source():
    """buildGpuCardHtml 源码含 .gpu-trend / trend-canvas / data-server-id（修复 a）。

    旧实现完全没有趋势块标记，本断言会变红——锁定 Azure 轮询重建不再丢失趋势。
    """
    seg = _azure_js_segment()
    # 定位 buildGpuCardHtml 函数体
    idx = seg.find("function buildGpuCardHtml")
    assert idx != -1
    body = seg[idx:]
    assert 'class="gpu-trend' in body
    assert "trend-canvas" in body
    assert "data-server-id" in body
    assert "gpu.gpu_index" in body
    assert "gpu.server_id" in body


def test_poll_paths_rebind_gpu_trend_source():
    """window.gpuTrendBindAll 被暴露，且两条轮询路径在 DOM 变更后调用它（修复 b）。"""
    js = _read_js()
    # 暴露 rebind 函数
    assert "window.gpuTrendBindAll = gpuTrendBindAll;" in js
    # renderAzureDashboard 末尾调用一次
    azure_seg = _azure_js_segment()
    ra_idx = azure_seg.find("function renderAzureDashboard")
    assert ra_idx != -1
    assert "window.gpuTrendBindAll()" in azure_seg[ra_idx:]
    # 外层 refreshDashboard innerHTML 替换后调用一次
    outer = js[: js.find("TASK-022")]
    rd_idx = outer.find("function refreshDashboard")
    assert rd_idx != -1
    assert "currentGrid.innerHTML = newGrid.innerHTML;" in outer[rd_idx:]
    assert "window.gpuTrendBindAll()" in outer[rd_idx:]


# ── 运行时：在 node 下真实执行 buildGpuCardHtml，断言趋势块被拼出 ───────────

_BUILD_HARNESS_TEMPLATE = r"""
'use strict';
%FUNCS%
const gpu = %GPU%;
const html = buildGpuCardHtml(gpu);
console.log(JSON.stringify({ html: html }));
"""


def _extract_named_funcs(segment: str, names: list[str]) -> str:
    """按名从 segment 抽取若干 function 定义源码（花括号配平）。"""
    out = []
    for name in names:
        m = re.search(r"(function " + re.escape(name) + r"\s*\([^)]*\)\s*\{)", segment)
        assert m, f"未能在 panel.js 中定位 {name}"
        start = m.start()
        depth = 0
        i = m.end(1) - 1
        while i < len(segment):
            ch = segment[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(segment[start : i + 1])
                    break
            i += 1
        else:  # pragma: no cover - 防御
            raise AssertionError(f"{name} 花括号未配平")
    return "\n".join(out)


def _run_build_gpu_card(gpu: dict) -> str:
    """在 node 下执行真实 buildGpuCardHtml(gpu)，返回拼出的 HTML 字符串。"""
    seg = _azure_js_segment()
    funcs = _extract_named_funcs(
        seg,
        [
            "escHtml",
            "round1",
            "utilThresholdClass",
            "memThresholdClass",
            "buildGpuCardHtml",
        ],
    )
    harness = _BUILD_HARNESS_TEMPLATE.replace("%FUNCS%", funcs).replace(
        "%GPU%", json.dumps(gpu)
    )
    proc = subprocess.run(  # noqa: S603 — 本地构造脚本,_NODE 为 which 解析的绝对路径
        [_NODE, "-e", harness],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])["html"]


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_runtime_build_gpu_card_includes_trend_block():
    """运行时：buildGpuCardHtml 对有数据 GPU 输出含正确 data-* 的趋势块。"""
    gpu = {
        "server_id": 7,
        "gpu_index": 3,
        "gpu_name": "NVIDIA A100",
        "util_pct": 55.0,
        "mem_pct": 50.0,
        "mem_used_mib": 40960.0,
        "mem_total_mib": 81920.0,
        "temp_c": 70.0,
        "power_w": 300.0,
        "is_stale": False,
    }
    html = _run_build_gpu_card(gpu)
    assert 'class="gpu-trend"' in html
    assert "trend-canvas" in html
    assert 'data-server-id="7"' in html
    assert 'data-gpu-index="3"' in html
    assert "历史趋势" in html


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_runtime_build_gpu_card_unreachable_has_no_trend():
    """运行时：util_pct=None 的不可达 GPU 不输出趋势块（与 SSR 一致）。"""
    gpu = {
        "server_id": 1,
        "gpu_index": 0,
        "gpu_name": None,
        "util_pct": None,
        "mem_pct": None,
        "is_stale": False,
    }
    html = _run_build_gpu_card(gpu)
    assert "不可达" in html
    assert "gpu-trend" not in html
