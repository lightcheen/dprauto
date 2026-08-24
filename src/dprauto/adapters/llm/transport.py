"""Killable HTTP transport for LLM requests with an absolute wall-clock limit."""

from __future__ import annotations

import multiprocessing
import time
import urllib.error
import urllib.request
from multiprocessing.connection import Connection
from typing import Mapping


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CLEANUP_GRACE_SECONDS = 0.25


class HTTPStatusError(Exception):
    """HTTP response with a non-success status returned by the isolated worker."""

    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"HTTP {code}: {body}")
        self.code = code
        self.body = body


class HTTPTransportError(OSError):
    """Transport error reconstructed from the isolated worker."""


def urlopen_with_hard_deadline(
    url: str,
    *,
    data: bytes,
    headers: Mapping[str, str],
    timeout_seconds: int,
) -> bytes:
    """Execute one request in a process that can be terminated at the deadline.

    ``urllib`` timeouts bound individual blocking socket operations, not necessarily
    the total request duration.  A peer that keeps making partial progress can
    therefore exceed both the configured request timeout and the Agent deadline.
    The parent owns the absolute deadline and forcibly reclaims the worker.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_urlopen_worker,
        args=(sender, url, data, dict(headers), timeout_seconds),
        name="dprauto-llm-http",
        daemon=True,
    )
    started = time.monotonic()
    try:
        process.start()
    except Exception:
        receiver.close()
        sender.close()
        process.close()
        raise
    sender.close()
    try:
        remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
        if not receiver.poll(remaining):
            raise TimeoutError(
                f"LLM HTTP hard wall-clock timeout after {timeout_seconds}s"
            )
        try:
            outcome = receiver.recv()
        except EOFError as exc:
            raise HTTPTransportError(
                f"LLM HTTP worker exited without a response (exit code {process.exitcode})"
            ) from exc
    finally:
        receiver.close()
        _terminate(process)

    kind = outcome[0]
    if kind == "ok":
        return outcome[1]
    if kind == "http-error":
        raise HTTPStatusError(outcome[1], outcome[2])
    if kind == "timeout":
        raise TimeoutError(outcome[1])
    if kind == "error":
        raise HTTPTransportError(outcome[1])
    raise HTTPTransportError("LLM HTTP worker returned an unknown outcome")


def _urlopen_worker(
    sender: Connection,
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
) -> None:
    try:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                sender.send(
                    (
                        "error",
                        f"LLM HTTP response exceeded {_MAX_RESPONSE_BYTES} bytes",
                    )
                )
                return
            sender.send(("ok", body))
        except urllib.error.HTTPError as exc:
            body = exc.read(64 * 1024).decode("utf-8", errors="replace")[-4_000:]
            sender.send(("http-error", exc.code, body))
        except (OSError, ValueError, TypeError) as exc:
            kind = "timeout" if _is_timeout(exc) else "error"
            sender.send((kind, f"{type(exc).__name__}: {exc}"))
    except (BrokenPipeError, EOFError, OSError):
        # The parent reached its absolute deadline and reclaimed the request.
        pass
    finally:
        sender.close()


def _terminate(process: multiprocessing.Process) -> None:
    process.join(_CLEANUP_GRACE_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_CLEANUP_GRACE_SECONDS)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(_CLEANUP_GRACE_SECONDS)
    if process.is_alive():
        raise HTTPTransportError("failed to terminate timed-out LLM HTTP worker")
    process.close()


def _is_timeout(exc: BaseException) -> bool:
    current: BaseException | object | None = exc
    visited: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            current = reason
            continue
        break
    message = str(exc).casefold()
    return "timed out" in message or "timeout" in message
