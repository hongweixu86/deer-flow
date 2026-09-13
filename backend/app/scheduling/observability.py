"""Structured observability hooks for the scheduling subsystem.

Same shape as :mod:`app.scheduling.audit` but explicitly named
``metric`` rather than ``audit`` so log shippers can split "things
that happened" (audit) from "things we measured" (metrics) if they
want to. Both modules ultimately emit ``logger.info`` lines under
their own logger; the metric line is just labelled with the metric
name + key=value payload.

The class is built with static methods (no instance state) because
the executor already lives on the request path and adding instance
plumbing for the metrics layer would just be noise. A future refactor
can drop in a real counters backend (StatsD / Prometheus) by replacing
the ``logger.info`` body without touching any caller.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_METRIC_PREFIX = "metric:schedule"


class Metrics:
    """Structured-metrics surface used by the executor.

    All methods are static. Each call emits a single
    ``metric:schedule <name> k=v ...`` line so a log-based metric
    pipeline (Loki, ELK, Datadog) can scrape the count.
    """

    @staticmethod
    def fire(schedule_id: str, kind: str, status: str) -> None:
        """One line per fire: ``metric:schedule fire ...``."""
        logger.info("[Metric] %s fire schedule_id=%s kind=%s status=%s", _METRIC_PREFIX, schedule_id, kind, status)

    @staticmethod
    def retry(attempt: int) -> None:
        """One line per scheduled retry."""
        logger.info("[Metric] %s retry attempt=%d", _METRIC_PREFIX, attempt)

    @staticmethod
    def push_failure(reason: str) -> None:
        """One line per failed outbound push."""
        logger.info("[Metric] %s push_failure reason=%s", _METRIC_PREFIX, reason)

    @staticmethod
    def run_duration(kind: str, seconds: float) -> None:
        """One line per completed run with its wall-clock duration."""
        logger.info("[Metric] %s run_duration kind=%s seconds=%.3f", _METRIC_PREFIX, kind, seconds)


__all__ = ["Metrics"]
