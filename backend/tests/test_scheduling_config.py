"""Tests for scheduling-related configuration models."""

from deerflow.config.scheduling import LimitsConfig, SchedulingConfig


def test_scheduling_config_defaults():
    cfg = SchedulingConfig()
    assert cfg.enabled is True
    assert cfg.timezone == "Asia/Shanghai"
    assert cfg.executor.max_concurrent_runs == 8
    assert cfg.executor.per_schedule_queue_size == 8
    assert cfg.executor.persist_queue is False
    assert cfg.retry.max_attempts == 3
    assert cfg.retry.backoff_seconds == [60, 300, 900]
    assert cfg.push.max_text_length == 4000
    assert cfg.push.include_run_url is True
    assert cfg.apscheduler.coalesce is True
    assert cfg.apscheduler.max_instances == 1
    assert cfg.apscheduler.misfire_grace_seconds == 300


def test_limits_config_defaults():
    assert LimitsConfig().max_active_schedules_per_user == 50
