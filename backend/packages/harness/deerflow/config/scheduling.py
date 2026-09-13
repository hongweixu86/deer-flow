"""Configuration models for scheduled agent runs.

The ``SchedulingConfig`` and ``LimitsConfig`` models are wired into
:class:`deerflow.config.app_config.AppConfig` and registered as
**startup-only** in :mod:`deerflow.config.reload_boundary`. They cover the
runtime knobs for the scheduler (concurrent runs, retry/backoff, push
payload, APScheduler) and the per-user resource caps enforced by the
scheduling API.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExecutorConfig(BaseModel):
    """Limits for the per-process schedule executor."""

    max_concurrent_runs: int = 8
    per_schedule_queue_size: int = 8
    persist_queue: bool = False


class RetryConfig(BaseModel):
    """Retry policy applied to failed schedule runs."""

    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])


class PushConfig(BaseModel):
    """Limits for pushback payloads (Feishu / IM channels)."""

    max_text_length: int = 4000
    include_run_url: bool = True


class APSchedulerConfig(BaseModel):
    """APScheduler job defaults for scheduled agent runs."""

    coalesce: bool = True
    max_instances: int = 1
    misfire_grace_seconds: int = 300


class SchedulingConfig(BaseModel):
    """Top-level scheduler configuration."""

    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    push: PushConfig = Field(default_factory=PushConfig)
    apscheduler: APSchedulerConfig = Field(default_factory=APSchedulerConfig)


class LimitsConfig(BaseModel):
    """Per-user resource caps exposed by the scheduling API."""

    max_active_schedules_per_user: int = 50
