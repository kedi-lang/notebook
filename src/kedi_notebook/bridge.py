from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Mapping
from threading import Condition, Lock
from typing import Any
from uuid import uuid4

_MAX_OUTPUT_CHARS = 200_000


class BridgeCancelled(RuntimeError):
    pass


class BridgeRun:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.condition = Condition()
        self.pending: deque[dict[str, Any]] = deque()
        self.waiting: set[str] = set()
        self.responses: dict[str, dict[str, Any]] = {}
        self.output_events: dict[str, deque[dict[str, str]]] = {}
        self.output_sizes: dict[str, int] = {}
        self.done = False
        self.cancelled = False
        self.updated_at = time.monotonic()

    def request(self, payload: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        response: Mapping[str, Any] | None = None
        for event in self.request_events(payload, timeout=timeout):
            if event["type"] == "response":
                response = event["response"]
        if response is None:
            raise RuntimeError("Playground bridge request returned no response")
        return response

    def request_events(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        request_id = uuid4().hex
        request = {"id": request_id, **dict(payload)}
        with self.condition:
            self._assert_open()
            self.pending.append(request)
            self.waiting.add(request_id)
            self.output_events[request_id] = deque()
            self.output_sizes[request_id] = 0
            self.updated_at = time.monotonic()
            self.condition.notify_all()
        deadline = time.monotonic() + timeout
        completed = False
        try:
            while True:
                with self.condition:
                    while not self.output_events[request_id] and request_id not in self.responses:
                        self._assert_open()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Timed out waiting for the browser model")
                        self.condition.wait(timeout=min(remaining, 1))
                    if self.output_events[request_id]:
                        event: dict[str, Any] = self.output_events[request_id].popleft()
                    else:
                        event = {
                            "type": "response",
                            "response": self.responses.pop(request_id),
                        }
                        completed = True
                yield event
                if completed:
                    return
        finally:
            with self.condition:
                self.waiting.discard(request_id)
                self.responses.pop(request_id, None)
                self.output_events.pop(request_id, None)
                self.output_sizes.pop(request_id, None)

    def next_request(self, *, timeout: float) -> dict[str, Any] | None:
        with self.condition:
            deadline = time.monotonic() + timeout
            while not self.pending and not self.done and not self.cancelled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 1))
            self.updated_at = time.monotonic()
            return self.pending.popleft() if self.pending else None

    def submit_response(self, request_id: str, response: Mapping[str, Any]) -> None:
        with self.condition:
            if request_id not in self.waiting:
                raise ValueError("Response does not belong to an active bridge request")
            if request_id in self.responses:
                raise ValueError("Duplicate bridge response")
            self.responses[request_id] = dict(response)
            self.updated_at = time.monotonic()
            self.condition.notify_all()

    def submit_output(self, request_id: str, *, stream: str, text: str) -> None:
        with self.condition:
            if request_id not in self.waiting:
                raise ValueError("Output does not belong to an active bridge request")
            if stream not in {"stdout", "stderr"}:
                raise ValueError("Bridge output stream must be stdout or stderr")
            if not text:
                return
            remaining = _MAX_OUTPUT_CHARS - self.output_sizes[request_id]
            if remaining <= 0:
                return
            text = text[:remaining]
            self.output_sizes[request_id] += len(text)
            self.output_events[request_id].append(
                {"type": "output", "stream": stream, "text": text}
            )
            self.updated_at = time.monotonic()
            self.condition.notify_all()

    def finish(self) -> None:
        with self.condition:
            self.done = True
            self.updated_at = time.monotonic()
            self.condition.notify_all()

    def cancel(self) -> None:
        with self.condition:
            self.cancelled = True
            self.updated_at = time.monotonic()
            self.condition.notify_all()

    def _assert_open(self) -> None:
        if self.cancelled:
            raise BridgeCancelled("Playground run was cancelled")
        if self.done:
            raise RuntimeError("Playground bridge run is already complete")


class BridgeManager:
    def __init__(self) -> None:
        self._runs: dict[str, BridgeRun] = {}
        self._lock = Lock()

    def get_or_create(self, run_id: str) -> BridgeRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                run = BridgeRun(run_id)
                self._runs[run_id] = run
            return run

    def cancel(self, run_id: str) -> None:
        self.get_or_create(run_id).cancel()

    def cleanup(self, *, max_age: float = 600) -> None:
        cutoff = time.monotonic() - max_age
        with self._lock:
            stale = [
                run_id
                for run_id, run in self._runs.items()
                if run.updated_at < cutoff and (run.done or run.cancelled)
            ]
            for run_id in stale:
                del self._runs[run_id]


__all__ = ["BridgeCancelled", "BridgeManager", "BridgeRun"]
