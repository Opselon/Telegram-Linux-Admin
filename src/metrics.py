"""Lightweight, dependency-free metrics registry.

The bot exposes Prometheus-style counters/gauges over an optional HTTP
endpoint (see :mod:`src.health`).  Everything here is deliberately small and
thread safe: handlers run inside the asyncio loop, while some bookkeeping
(audit flushes, SSH pool reaping) happens in worker threads.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable, Mapping

Labels = Mapping[str, object] | None


def _normalize(labels: Labels) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """A minimal counter/gauge registry with Prometheus text rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    # -- writers ---------------------------------------------------------
    def inc(self, name: str, value: float = 1.0, **labels: object) -> None:
        """Increments a counter (labels are part of the series identity)."""
        key = (name, _normalize(labels))
        with self._lock:
            self._counters[key] += value

    def set(self, name: str, value: float, **labels: object) -> None:
        """Sets a gauge to an absolute value."""
        key = (name, _normalize(labels))
        with self._lock:
            self._gauges[key] = float(value)

    # -- readers ---------------------------------------------------------
    def get(self, name: str, **labels: object) -> float:
        key = (name, _normalize(labels))
        with self._lock:
            if key in self._gauges:
                return self._gauges[key]
            return self._counters.get(key, 0.0)

    def series(self) -> Iterable[tuple[str, tuple[tuple[str, str], ...], float]]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        for (name, labels), value in counters.items():
            yield name, labels, value
        for (name, labels), value in gauges.items():
            yield name, labels, value

    def render(self) -> str:
        """Renders every series in the Prometheus text exposition format."""
        lines: list[str] = []
        for name, labels, value in sorted(self.series()):
            if not name.startswith("tla_"):
                name = f"tla_{name}"
            if labels:
                rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{rendered}}} {value}")
            else:
                lines.append(f"{name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        """Clears every series. Intended for tests."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


#: Process-wide registry shared by handlers, the SSH manager and the health server.
METRICS = Metrics()
