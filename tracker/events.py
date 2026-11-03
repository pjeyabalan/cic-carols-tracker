from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Dict, Generator, Optional


class TrackerEventBus:
    """Thread-safe Server-Sent Events (SSE) pub/sub broker for real-time tracker telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[str]] = set()
        self._seq = 0

    def publish(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
            envelope = json.dumps(
                {
                    "seq": seq,
                    "event": event_type,
                    "timestamp": time.time(),
                    "data": payload or {},
                }
            )
            dead: list[queue.Queue[str]] = []
            for subscriber in self._subscribers:
                try:
                    subscriber.put_nowait(envelope)
                except queue.Full:
                    dead.append(subscriber)
            for item in dead:
                self._subscribers.discard(item)
            return seq

    def subscribe(self, heartbeat_seconds: float = 20.0) -> Generator[str, None, None]:
        sub_queue: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(sub_queue)
        try:
            while True:
                try:
                    msg = sub_queue.get(timeout=heartbeat_seconds)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # Keepalive comment prevents reverse proxies (Render / Cloudflare) from closing idle streams
                    yield ": heartbeat\n\n"
        finally:
            with self._lock:
                self._subscribers.discard(sub_queue)


event_bus = TrackerEventBus()
