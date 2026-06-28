"""Reporter 单元测试 (TASK-031 / TASK-032).

纯标准库 unittest（也可由 pytest 收集）。覆盖：
  * Codex 本地 jsonl → metrics（真实 primary/secondary schema + 文档 requests 回退）
  * Claude Code 近 5h token 累计（含窗口边界）
  * ChatGPT 手填 json → metrics
  * payload 构造与 POST body 结构符合 TASK-030 契约
  * post_payload 成功/失败（mock httpx，不发网络）
  * main() 端到端退出码

运行：
    .venv/bin/pytest tools/reporter/test_reporter.py -q
    python3 -m unittest tools.reporter.test_reporter -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# 让 `from sources import ...` / `import reporter` 在任意 cwd 下可用。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import reporter  # noqa: E402
from sources.chatgpt import ChatGptSource  # noqa: E402
from sources.claude_code import ClaudeCodeSource, _mask_token  # noqa: E402
from sources.codex import CodexSource  # noqa: E402

# 已知 TASK-030 契约的合法 metric 名（用于结构断言）。
KNOWN_METRICS = {
    "used_requests",
    "limit_requests",
    "used_percent",
    "resets_at",
    "window_seconds",
    "extra",
    "used_tokens",
    "limit_tokens",
    "secondary_used_percent",
    "secondary_resets_at",
}


def _metric(payload: dict, name: str):
    for m in payload["metrics"]:
        if m["metric"] == name:
            return m
    return None


def _assert_contract(testcase: unittest.TestCase, payload: dict, provider: str) -> None:
    """断言 payload 符合 AiUsagePayload 契约（TASK-030 / ARCH-004）。"""
    testcase.assertIn("reporter_version", payload)
    testcase.assertEqual(payload["provider"], provider)
    testcase.assertIn(payload["status"], ("ok", "error"))
    # reported_at 必须可被解析为 ISO8601。
    datetime.fromisoformat(payload["reported_at"].replace("Z", "+00:00"))
    testcase.assertIsInstance(payload["metrics"], list)
    testcase.assertGreater(len(payload["metrics"]), 0)
    for m in payload["metrics"]:
        testcase.assertIn("metric", m)
        testcase.assertIn("value_num", m)
        testcase.assertIn("value_text", m)
        # 数值型走 value_num，文本型走 value_text（不强制每条都有，但二者结构存在）
        testcase.assertIn(m["metric"], KNOWN_METRICS)


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
class TestCodexSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.codex_dir = self.tmp.name
        self.sessions = os.path.join(self.codex_dir, "sessions", "2026", "06", "28")
        os.makedirs(self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_session(self, lines: list[dict], name="rollout-x.jsonl"):
        path = os.path.join(self.sessions, name)
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return path

    def test_real_schema_primary_secondary(self):
        """真实 codex schema：primary/secondary 桶 + used_percent + epoch resets_at。"""
        self._write_session(
            [
                {"type": "session_meta", "payload": {}},
                {
                    "timestamp": "2026-06-28T04:29:51.655Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 100}},
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {
                                "used_percent": 46.0,
                                "window_minutes": 300,
                                "resets_at": 1781686515,
                            },
                            "secondary": {
                                "used_percent": 17.0,
                                "window_minutes": 10080,
                                "resets_at": 1782106461,
                            },
                            "plan_type": "team",
                        },
                    },
                },
            ]
        )
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertIsNotNone(payload)
        _assert_contract(self, payload, "codex")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 46.0)
        # window_minutes 300 → 18000s
        self.assertEqual(_metric(payload, "window_seconds")["value_num"], 18000.0)
        # epoch → ISO8601
        resets = _metric(payload, "resets_at")["value_text"]
        self.assertTrue(resets.startswith("2026-06-17T"))
        # secondary 桶
        self.assertEqual(_metric(payload, "secondary_used_percent")["value_num"], 17.0)
        self.assertIsNotNone(_metric(payload, "secondary_resets_at"))
        # extra 含来源信息
        extra = json.loads(_metric(payload, "extra")["value_text"])
        self.assertEqual(extra["data_source"], "local_jsonl")

    def test_takes_latest_line(self):
        """取最后一条含 rate_limits 的行（最新）。"""
        self._write_session(
            [
                {
                    "type": "event_msg",
                    "payload": {"rate_limits": {"primary": {"used_percent": 10.0, "window_minutes": 300}}},
                },
                {
                    "type": "event_msg",
                    "payload": {"rate_limits": {"primary": {"used_percent": 88.0, "window_minutes": 300}}},
                },
            ]
        )
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 88.0)

    def test_doc_schema_requests_fallback(self):
        """文档假设 schema：requests.used/limit/reset_at，used_percent=used/limit*100。"""
        self._write_session(
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "rate_limits": {
                            "requests": {"used": 42, "limit": 500, "reset_at": "2026-06-28T15:00:00Z"}
                        }
                    },
                }
            ]
        )
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertIsNotNone(payload)
        self.assertEqual(_metric(payload, "used_requests")["value_num"], 42.0)
        self.assertEqual(_metric(payload, "limit_requests")["value_num"], 500.0)
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 8.4)  # 1 位小数
        self.assertEqual(_metric(payload, "resets_at")["value_text"], "2026-06-28T15:00:00Z")
        self.assertEqual(payload["status"], "ok")

    def test_zero_limit_sets_error(self):
        """limit=0 → used_percent 缺失，status=error。"""
        self._write_session(
            [{"type": "event_msg", "payload": {"rate_limits": {"requests": {"used": 5, "limit": 0}}}}]
        )
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertIsNone(_metric(payload, "used_percent"))
        self.assertEqual(payload["status"], "error")

    def test_missing_dir(self):
        payload = CodexSource({"CODEX_DIR": "/nonexistent/path/xyz"}).collect()
        self.assertIsNone(payload)

    def test_no_rate_limits(self):
        self._write_session([{"type": "event_msg", "payload": {"foo": "bar"}}])
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertIsNone(payload)

    def test_malformed_json_line(self):
        """非法 JSON 行被跳过，仍能从合法行解析。"""
        path = os.path.join(self.sessions, "rollout-x.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json }\n")
            f.write(
                json.dumps(
                    {"type": "event_msg", "payload": {"rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 300}}}}
                )
                + "\n"
            )
        payload = CodexSource({"CODEX_DIR": self.codex_dir}).collect()
        self.assertIsNotNone(payload)
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 5.0)


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
class TestClaudeCodeSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = os.path.join(self.tmp.name, "projects")
        self.proj1 = os.path.join(self.projects, "-Users-mux-proj1")
        os.makedirs(self.proj1)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, lines: list[dict], name="session.jsonl"):
        path = os.path.join(self.proj1, name)
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return path

    @staticmethod
    def _line(ts: datetime, inp: int, out: int, cc: int = 0):
        return {
            "type": "assistant",
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": 999,  # 不计入
                }
            },
        }

    def test_window_calc_excludes_old(self):
        """now-6h 的记录不计，now-2h 的记入。"""
        now = datetime.now(timezone.utc)
        self._write(
            [
                self._line(now - timedelta(hours=6), 1000, 1000, 1000),  # 窗外
                self._line(now - timedelta(hours=2), 100, 200, 50),       # 窗内
            ]
        )
        payload = ClaudeCodeSource({"CLAUDE_PROJECTS_DIR": self.projects}).collect()
        self.assertIsNotNone(payload)
        _assert_contract(self, payload, "claude_code")
        # 仅 100+200+50 = 350（cache_read 999 不计）
        self.assertEqual(_metric(payload, "used_tokens")["value_num"], 350.0)

    def test_message_usage_nested(self):
        """usage 嵌在 message.usage（真实 schema）。"""
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=10), 10, 20, 5)])
        payload = ClaudeCodeSource({"CLAUDE_PROJECTS_DIR": self.projects}).collect()
        self.assertEqual(_metric(payload, "used_tokens")["value_num"], 35.0)

    def test_no_limit_percent_none(self):
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=5), 100, 50)])
        payload = ClaudeCodeSource({"CLAUDE_PROJECTS_DIR": self.projects}).collect()
        self.assertIsNotNone(_metric(payload, "used_tokens"))
        self.assertIsNone(_metric(payload, "used_percent")["value_num"])
        self.assertIsNone(_metric(payload, "limit_tokens")["value_num"])

    def test_with_limit_percent_calc(self):
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=5), 30000, 20000)])  # 50000
        payload = ClaudeCodeSource(
            {"CLAUDE_PROJECTS_DIR": self.projects, "CLAUDE_LIMIT_TOKENS": "100000"}
        ).collect()
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 50.0)
        self.assertEqual(_metric(payload, "limit_tokens")["value_num"], 100000.0)

    def test_missing_dir(self):
        payload = ClaudeCodeSource({"CLAUDE_PROJECTS_DIR": "/nonexistent/xyz"}).collect()
        self.assertIsNone(payload)

    def test_oauth_skipped_without_token(self):
        """无 session token → _collect_oauth 不被调用。"""
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=1), 10, 10)])
        src = ClaudeCodeSource({"CLAUDE_PROJECTS_DIR": self.projects})
        with mock.patch.object(src, "_collect_oauth") as m:
            src.collect()
            m.assert_not_called()

    def test_oauth_fail_silent_returns_jsonl(self):
        """OAuth 网络错误 → 仍返回 jsonl 数据，不抛。"""
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=1), 100, 100)])
        src = ClaudeCodeSource(
            {
                "CLAUDE_PROJECTS_DIR": self.projects,
                "CLAUDE_SESSION_TOKEN": "sk-secret-abc123456789",
                "CLAUDE_ORG_ID": "org-1",
            }
        )
        with mock.patch.object(src, "_collect_oauth", return_value=None):
            payload = src.collect()
        self.assertIsNotNone(payload)
        self.assertEqual(_metric(payload, "used_tokens")["value_num"], 200.0)

    def test_oauth_merge_takes_max(self):
        """OAuth 返回更大 used_tokens → 合并取最大。"""
        now = datetime.now(timezone.utc)
        self._write([self._line(now - timedelta(minutes=1), 100, 100)])  # 200
        src = ClaudeCodeSource(
            {
                "CLAUDE_PROJECTS_DIR": self.projects,
                "CLAUDE_SESSION_TOKEN": "sk-secret-abc123456789",
                "CLAUDE_ORG_ID": "org-1",
                "CLAUDE_LIMIT_TOKENS": "1000",
            }
        )
        with mock.patch.object(src, "_collect_oauth", return_value={"used_tokens": 500}):
            payload = src.collect()
        self.assertEqual(_metric(payload, "used_tokens")["value_num"], 500.0)
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 50.0)  # 重算

    def test_token_masking(self):
        self.assertEqual(_mask_token("sk-12345678abc"), "sk-12345****")
        self.assertEqual(_mask_token("short"), "****")
        self.assertNotIn("abc", _mask_token("sk-12345678abc"))


# --------------------------------------------------------------------------- #
# ChatGPT
# --------------------------------------------------------------------------- #
class TestChatGptSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "chatgpt.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_manual_ok(self):
        self._write(
            {
                "used_messages": 30,
                "limit_messages": 80,
                "resets_at": "2026-06-28T20:00:00Z",
                "window_seconds": 10800,
                "updated_manually_at": "2026-06-28T09:00:00Z",
            }
        )
        payload = ChatGptSource({"CHATGPT_JSON": self.path}).collect()
        self.assertIsNotNone(payload)
        _assert_contract(self, payload, "chatgpt")
        self.assertEqual(_metric(payload, "used_requests")["value_num"], 30.0)
        self.assertEqual(_metric(payload, "limit_requests")["value_num"], 80.0)
        self.assertEqual(_metric(payload, "used_percent")["value_num"], 37.5)
        self.assertEqual(_metric(payload, "window_seconds")["value_num"], 10800.0)
        extra = json.loads(_metric(payload, "extra")["value_text"])
        self.assertEqual(extra["data_source"], "manual")
        self.assertEqual(extra["updated_manually_at"], "2026-06-28T09:00:00Z")

    def test_missing_file(self):
        payload = ChatGptSource({"CHATGPT_JSON": "/nonexistent/chatgpt.json"}).collect()
        self.assertIsNone(payload)

    def test_malformed_json(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        payload = ChatGptSource({"CHATGPT_JSON": self.path}).collect()
        self.assertIsNone(payload)

    def test_zero_limit_error(self):
        self._write({"used_messages": 10, "limit_messages": 0})
        payload = ChatGptSource({"CHATGPT_JSON": self.path}).collect()
        self.assertIsNone(_metric(payload, "used_percent"))
        self.assertEqual(payload["status"], "error")


# --------------------------------------------------------------------------- #
# reporter 主流程 + post_payload
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


class _FakeHttpx:
    """最小 httpx 替身，记录最后一次 post 的参数。"""

    class RequestError(Exception):
        pass

    def __init__(self, response=None, raise_exc=None):
        self._response = response or _FakeResponse()
        self._raise = raise_exc
        self.last_call = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.last_call = {"url": url, "json": json, "headers": headers, "timeout": timeout}
        if self._raise:
            raise self._raise
        return self._response


class TestPostPayload(unittest.TestCase):
    def _payload(self):
        return {
            "reporter_version": "1.0",
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "provider": "codex",
            "status": "ok",
            "metrics": [{"metric": "used_percent", "value_num": 10.0, "value_text": None}],
        }

    def test_post_ok_and_body_contract(self):
        fake = _FakeHttpx(_FakeResponse(200))
        with mock.patch.dict(sys.modules, {"httpx": fake}):
            ok = reporter.post_payload("http://panel:8000", "tok123", self._payload())
        self.assertTrue(ok)
        # URL 拼接正确
        self.assertEqual(fake.last_call["url"], "http://panel:8000/api/ingest/ai-usage")
        # Bearer 头存在
        self.assertEqual(fake.last_call["headers"]["Authorization"], "Bearer tok123")
        # body 符合 TASK-030 契约
        body = fake.last_call["json"]
        self.assertEqual(body["provider"], "codex")
        self.assertIn(body["status"], ("ok", "error"))
        self.assertIn("reported_at", body)
        self.assertIn("reporter_version", body)
        self.assertIsInstance(body["metrics"], list)

    def test_post_no_token_no_auth_header(self):
        fake = _FakeHttpx(_FakeResponse(200))
        with mock.patch.dict(sys.modules, {"httpx": fake}):
            reporter.post_payload("http://panel:8000/", "", self._payload())
        self.assertNotIn("Authorization", fake.last_call["headers"])
        # 尾部斜杠被正确处理
        self.assertEqual(fake.last_call["url"], "http://panel:8000/api/ingest/ai-usage")

    def test_post_non_200_returns_false(self):
        fake = _FakeHttpx(_FakeResponse(400, '{"ok":false}'))
        with mock.patch.dict(sys.modules, {"httpx": fake}):
            ok = reporter.post_payload("http://panel:8000", "", self._payload())
        self.assertFalse(ok)

    def test_post_network_error_returns_false(self):
        fake = _FakeHttpx(raise_exc=ConnectionError("boom"))
        with mock.patch.dict(sys.modules, {"httpx": fake}):
            ok = reporter.post_payload("http://panel:8000", "", self._payload())
        self.assertFalse(ok)


class TestMainEndToEnd(unittest.TestCase):
    def test_main_missing_panel_url(self):
        with mock.patch.object(reporter, "load_env", return_value={}):
            self.assertEqual(reporter.main(), 2)

    def test_main_success_exit_0(self):
        cfg = {"PANEL_URL": "http://panel:8000"}

        class _Src:
            name = "codex"

            def collect(self):
                return {
                    "reporter_version": "1.0",
                    "reported_at": datetime.now(timezone.utc).isoformat(),
                    "provider": "codex",
                    "status": "ok",
                    "metrics": [{"metric": "used_percent", "value_num": 5.0, "value_text": None}],
                }

        with mock.patch.object(reporter, "load_env", return_value=cfg), mock.patch.object(
            reporter, "build_sources", return_value=[_Src()]
        ), mock.patch.object(reporter, "post_payload", return_value=True) as mp:
            rc = reporter.main()
        self.assertEqual(rc, 0)
        mp.assert_called_once()

    def test_main_all_fail_exit_1(self):
        cfg = {"PANEL_URL": "http://panel:8000"}

        class _Src:
            name = "codex"

            def collect(self):
                return None  # 无数据

        with mock.patch.object(reporter, "load_env", return_value=cfg), mock.patch.object(
            reporter, "build_sources", return_value=[_Src()]
        ):
            rc = reporter.main()
        self.assertEqual(rc, 1)

    def test_main_two_sources_both_posted(self):
        cfg = {"PANEL_URL": "http://panel:8000"}

        def _mk(name):
            class _S:
                pass

            s = _S()
            s.name = name
            s.collect = lambda: {
                "reporter_version": "1.0",
                "reported_at": datetime.now(timezone.utc).isoformat(),
                "provider": name,
                "status": "ok",
                "metrics": [{"metric": "used_percent", "value_num": 1.0, "value_text": None}],
            }
            return s

        with mock.patch.object(reporter, "load_env", return_value=cfg), mock.patch.object(
            reporter, "build_sources", return_value=[_mk("codex"), _mk("claude_code")]
        ), mock.patch.object(reporter, "post_payload", return_value=True) as mp:
            rc = reporter.main()
        self.assertEqual(rc, 0)
        self.assertEqual(mp.call_count, 2)


if __name__ == "__main__":
    unittest.main()
