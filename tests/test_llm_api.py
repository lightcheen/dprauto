import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dprauto.adapters.llm import (
    APIModelSettings,
    FailoverLLMClient,
    OpenAICompatibleLLMClient,
)
from dprauto.errors import LLMTimeoutError
from dprauto.observability.llm_calls import HourlyLLMCallLogger
from dprauto.ports.llm import LLMMessage, LLMRequest


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class SlowStreamingLLMHandler(BaseHTTPRequestHandler):
    response_body = json.dumps(
        {
            "model": "fixture-model",
            "choices": [{"message": {"content": "too late"}}],
            "usage": {},
        }
    ).encode()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        chunk_size = max(1, len(self.response_body) // 5)
        try:
            for offset in range(0, len(self.response_body), chunk_size):
                self.wfile.write(self.response_body[offset : offset + chunk_size])
                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


class ImmediateLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        )
        body = json.dumps(
            {
                "model": request["model"],
                "choices": [{"message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class APILLMClientTests(unittest.TestCase):
    def test_production_transport_returns_completed_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), ImmediateLLMHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = OpenAICompatibleLLMClient(
                APIModelSettings(
                    "secret",
                    "fixture-model",
                    f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                ),
                HourlyLLMCallLogger(root / "logs"),
                timeout_seconds=2,
            )
            try:
                with patch.dict(
                    os.environ,
                    {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                ):
                    response = client.complete(
                        LLMRequest(
                            (LLMMessage("user", "complete in time"),),
                            response_schema={"type": "object"},
                        )
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

            self.assertEqual(response.structured, {"answer": "ok"})
            self.assertEqual(response.input_tokens, 3)
            self.assertEqual(response.output_tokens, 2)

    def test_production_transport_enforces_absolute_wall_clock_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStreamingLLMHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = OpenAICompatibleLLMClient(
                APIModelSettings(
                    "secret",
                    "fixture-model",
                    f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                ),
                HourlyLLMCallLogger(root / "logs"),
                timeout_seconds=1,
            )

            started = time.monotonic()
            try:
                with patch.dict(
                    os.environ,
                    {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                ):
                    with self.assertRaises(LLMTimeoutError) as raised:
                        client.complete(
                            LLMRequest((LLMMessage("user", "enforce the deadline"),))
                        )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)
            elapsed = time.monotonic() - started

            self.assertIn("hard wall-clock timeout after 1s", raised.exception.message)
            self.assertGreaterEqual(elapsed, 0.8)
            self.assertLess(elapsed, 2.0)
            records = [
                json.loads(line)
                for path in (root / "logs").glob("*.log")
                for line in path.read_text().splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertIn("hard wall-clock timeout", records[0]["error"])

    def test_configured_client_logs_questions_and_results_once_per_hour_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "myapi.json"
            secret = "secret-must-never-be-logged"
            config_path.write_text(
                json.dumps(
                    {
                        "api_key": secret,
                        "model": "fixture-model",
                        "base_url": "https://llm.invalid/v1/chat/completions",
                    }
                ),
                encoding="utf-8",
            )
            local_zone = ZoneInfo("Asia/Shanghai")
            clock = MutableClock(datetime(2026, 8, 10, 10, 5, tzinfo=local_zone))
            calls = []

            def opener(request, timeout):
                calls.append((json.loads(request.data), request.headers, timeout))
                number = len(calls)
                return FakeHTTPResponse(
                    {
                        "model": "fixture-model",
                        "choices": [
                            {"message": {"content": json.dumps({"answer": number})}}
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                    }
                )

            client = OpenAICompatibleLLMClient(
                APIModelSettings.from_json(config_path),
                HourlyLLMCallLogger(root / "logs", clock=clock),
                timeout_seconds=9,
                opener=opener,
            )
            request = LLMRequest(
                (LLMMessage("user", "how should this build be repaired?"),),
                response_schema={"type": "object"},
                metadata={"run_id": "hourly-log-test"},
            )
            first = client.complete(request)
            clock.value = datetime(2026, 8, 10, 10, 55, tzinfo=local_zone)
            client.complete(request)
            clock.value = datetime(2026, 8, 10, 11, 1, tzinfo=local_zone)
            client.complete(request)

            logs = sorted((root / "logs").glob("*.log"))
            self.assertEqual([path.name for path in logs], [
                "2026-08-10-10.log",
                "2026-08-10-11.log",
            ])
            self.assertEqual(len(logs[0].read_text().splitlines()), 2)
            self.assertEqual(len(logs[1].read_text().splitlines()), 1)
            combined = "".join(path.read_text() for path in logs)
            self.assertIn("how should this build be repaired?", combined)
            self.assertIn('\\"answer\\": 1', combined)
            self.assertNotIn(secret, combined)
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(first.structured, {"answer": 1})
            self.assertEqual(first.input_tokens, 10)
            self.assertEqual(calls[0][2], 9)
            self.assertEqual(calls[0][0]["max_tokens"], 4096)
            self.assertIn("Bearer", calls[0][1]["Authorization"])

    def test_timeout_retries_same_model_then_switches_to_next_configured_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "myapi.json"
            config_path.write_text(
                json.dumps(
                    {
                        "slow": {
                            "api_key": "first-secret",
                            "model": "slow-model",
                            "base_url": "https://slow.invalid/v1/chat/completions",
                        },
                        "fallback": {
                            "api_key": "second-secret",
                            "model": "fallback-model",
                            "base_url": "https://fallback.invalid/v1/chat/completions",
                        },
                    }
                ),
                encoding="utf-8",
            )
            settings = APIModelSettings.all_from_json(config_path)
            self.assertEqual([item.name for item in settings], ["slow", "fallback"])
            attempts = {"slow-model": 0, "fallback-model": 0}

            def opener(request, timeout):
                model = json.loads(request.data)["model"]
                attempts[model] += 1
                if model == "slow-model":
                    raise TimeoutError("read operation timed out")
                return FakeHTTPResponse(
                    {
                        "model": model,
                        "choices": [{"message": {"content": "recovered"}}],
                        "usage": {},
                    }
                )

            logger = HourlyLLMCallLogger(root / "logs")
            client = FailoverLLMClient(
                tuple(
                    OpenAICompatibleLLMClient(
                        item, logger, timeout_seconds=1, opener=opener
                    )
                    for item in settings
                )
            )
            response = client.complete(
                LLMRequest(
                    (LLMMessage("user", "repair this project"),),
                    metadata={"run_id": "failover-test"},
                )
            )

            self.assertEqual(response.content, "recovered")
            self.assertEqual(attempts, {"slow-model": 2, "fallback-model": 1})
            records = [
                json.loads(line)
                for path in (root / "logs").glob("*.log")
                for line in path.read_text().splitlines()
            ]
            self.assertEqual(len(records), 3)
            self.assertEqual(
                [item["metadata"]["configured_model"] for item in records],
                ["slow-model", "slow-model", "fallback-model"],
            )
            self.assertNotIn("first-secret", "".join(map(json.dumps, records)))
            self.assertNotIn("second-secret", "".join(map(json.dumps, records)))

    def test_plan_fix_failover_stops_after_two_timeouts_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def opener(request, timeout):
                calls.append(json.loads(request.data)["model"])
                raise TimeoutError("read operation timed out")

            logger = HourlyLLMCallLogger(root / "logs")
            client = FailoverLLMClient(
                (
                    OpenAICompatibleLLMClient(
                        APIModelSettings(
                            "secret",
                            "slow-model",
                            "https://slow.invalid/v1/chat/completions",
                        ),
                        logger,
                        timeout_seconds=1,
                        opener=opener,
                    ),
                    OpenAICompatibleLLMClient(
                        APIModelSettings(
                            "secret",
                            "fallback-model",
                            "https://fallback.invalid/v1/chat/completions",
                        ),
                        logger,
                        timeout_seconds=1,
                        opener=opener,
                    ),
                )
            )

            with self.assertRaises(LLMTimeoutError) as raised:
                client.complete(
                    LLMRequest(
                        (LLMMessage("user", "repair this project"),),
                        metadata={"operation": "plan_fix", "run_id": "plan-timeout-test"},
                    )
                )

            self.assertEqual(calls, ["slow-model", "slow-model"])
            self.assertIn("2 plan_fix timeout", raised.exception.message)

    def test_request_deadline_clamps_http_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed_timeouts = []

            def opener(request, timeout):
                observed_timeouts.append(timeout)
                return FakeHTTPResponse(
                    {
                        "model": "fixture-model",
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {},
                    }
                )

            client = OpenAICompatibleLLMClient(
                APIModelSettings(
                    "secret",
                    "fixture-model",
                    "https://llm.invalid/v1/chat/completions",
                ),
                HourlyLLMCallLogger(root / "logs"),
                timeout_seconds=120,
                opener=opener,
            )

            client.complete(
                LLMRequest(
                    (LLMMessage("user", "repair this project"),),
                    deadline_at=datetime.now(timezone.utc) + timedelta(seconds=5),
                )
            )

            self.assertGreaterEqual(observed_timeouts[0], 1)
            self.assertLessEqual(observed_timeouts[0], 5)

    def test_failover_does_not_start_attempt_after_deadline_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = 0

            def opener(request, timeout):
                nonlocal calls
                calls += 1
                raise AssertionError("LLM opener should not be called after deadline")

            client = FailoverLLMClient(
                (
                    OpenAICompatibleLLMClient(
                        APIModelSettings(
                            "secret",
                            "slow-model",
                            "https://slow.invalid/v1/chat/completions",
                        ),
                        HourlyLLMCallLogger(root / "logs"),
                        timeout_seconds=1,
                        opener=opener,
                    ),
                )
            )

            with self.assertRaises(LLMTimeoutError):
                client.complete(
                    LLMRequest(
                        (LLMMessage("user", "repair this project"),),
                        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    )
                )
            self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
