"""Metrics.

A tiny in-process registry rather than a client library. The DAG collects
counters over a task and emits them once at the end; nothing here talks to a
metrics backend, which keeps the pure-Python layers importable anywhere.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["MetricsRegistry", "Sample", "timer"]


def _key(name: str, labels: Mapping[str, str] | None) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


@dataclass(frozen=True)
class Sample:
    """One emitted metric."""

    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()
    kind: str = "counter"

    @property
    def key(self) -> str:
        return _key(self.name, dict(self.labels))


@dataclass
class MetricsRegistry:
    """Counters, gauges and observation lists, keyed by name and labels."""

    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    observations: dict[str, list[float]] = field(default_factory=dict)

    def increment(self, name: str, value: float = 1.0, **labels: str) -> float:
        if value < 0:
            raise ValueError("counters cannot decrease")
        key = _key(name, labels)
        self.counters[key] = self.counters.get(key, 0.0) + value
        return self.counters[key]

    def gauge(self, name: str, value: float, **labels: str) -> float:
        key = _key(name, labels)
        self.gauges[key] = float(value)
        return self.gauges[key]

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations.setdefault(_key(name, labels), []).append(float(value))

    def percentile(self, name: str, fraction: float, **labels: str) -> float | None:
        """Nearest-rank percentile over recorded observations.

        Returns ``None`` when nothing has been observed under that key.
        Nearest-rank rather than interpolated because these series are short
        and an interpolated p99 over eleven samples is a fiction.
        """
        if not 0.0 < fraction <= 1.0:
            raise ValueError("fraction must be in (0, 1]")
        values = sorted(self.observations.get(_key(name, labels), []))
        if not values:
            return None
        rank = max(1, math.ceil(fraction * len(values)))
        return values[min(rank, len(values)) - 1]

    def snapshot(self) -> list[Sample]:
        """Everything recorded, as a flat sorted list."""
        samples: list[Sample] = []
        for key, value in self.counters.items():
            samples.append(Sample(name=key, value=value, kind="counter"))
        for key, value in self.gauges.items():
            samples.append(Sample(name=key, value=value, kind="gauge"))
        for key, values in self.observations.items():
            samples.append(Sample(name=f"{key}#count", value=len(values), kind="summary"))
            samples.append(Sample(name=f"{key}#sum", value=sum(values), kind="summary"))
        return sorted(samples, key=lambda s: (s.kind, s.name))

    def reset(self) -> None:
        self.counters.clear()
        self.gauges.clear()
        self.observations.clear()


@contextmanager
def timer(registry: MetricsRegistry, name: str, clock: object = None, **labels: str) -> Iterator[None]:
    """Record how long a block took.

    ``clock`` is any zero-argument callable returning a float; the default is
    a monotonic counter, but the DAG passes a fake in tests so the recorded
    duration is deterministic.
    """
    import time as _time

    tick = clock if callable(clock) else _time.monotonic
    started = tick()
    try:
        yield
    finally:
        registry.observe(name, max(0.0, tick() - started), **labels)
