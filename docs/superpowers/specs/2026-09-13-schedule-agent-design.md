# Design: 定时任务触发智能体 + 飞书推送 + 用户个人定时任务

- **Date**: 2026-09-13
- **Owner**: deer-flow
- **Status**: Draft (awaiting user review)
- **Scope**: DeerFlow 2.0 — adding scheduled agent runs that push results back to Feishu, with per-user subscription semantics and a Web "Schedules" management page.

## 1. Goals

DeerFlow 2.0 users should be able to:

1. **Set a personal schedule** in two ways:
   - In a Feishu conversation, ask the agent "summarise X every day at 9am" — the agent parses the request, confirms, and registers the schedule.
   - On the Web UI, create one from `/schedules` with cron / one-shot / natural-language input.
2. **Subscribe / reuse** an existing schedule. The schedule creator is the default owner; other users can subscribe and receive their own runs.
3. **Resilient execution**. Agent and push failures retry up to N times with backoff; final failures surface a clear alert.
4. **Centralised management** via Web UI `/schedules`: list, create, pause, resume, delete, inspect run history.

## 2. Scope

### In scope (MVP)
- Trigger models: cron expression, one-shot future timestamp, natural language (parsed in-thread by the existing lead agent).
- Push: Feishu only. Each run pushes to the schedule's `target_json` (and, when the schedule was created from an inbound message, replies in the same Feishu thread).
- Agent tools: `schedule_create / list / get / update / pause / resume / delete / subscribe / unsubscribe / runs`.
- Web UI: `/schedules` page (list, detail, create, history).
- Persistence: SQLAlchemy + Alembic on the existing engine (SQLite by default, optional Postgres).

### Out of scope (deferred)
- Push to Slack / Telegram / DingTalk / WeCom.
- Multi-Gateway leader election beyond the MVP advisory-lock mitigation.
- Cross-user ACL beyond owner / subscriber.
- Web-side natural-language → cron preview (UI can ship with a placeholder; the in-thread agent flow already covers it).

## 3. Decisions (recap of clarification)

| Axis | Decision |
|---|---|
| Trigger kinds | cron + one-shot + natural language (all three) |
| Push target | both "original conversation" and "optional extra target" — stored on the schedule |
| Channels | Feishu only (MVP) |
| NL parsing | reuse lead agent, in-thread (no extra model) |
| Re-entrancy | Queue per (schedule, subscriber), bounded; reject when full |
| Failure handling | max 3 attempts, backoff `[60s, 300s, 900s]`, then alert |
| Owner model | task + subscribers; owner can share, others can subscribe / unsubscribe |
| Manual entry | Web UI `/schedules` page (REST API is implicit) |
| Persistence | same engine as the rest of the app (SQLite / Postgres via SQLAlchemy) |

## 4. Architecture

```
                ┌───────────────────────────┐
                │      Web UI /schedules     │
                └─────────────┬─────────────┘
                              │ REST
                              ▼
  ┌─────────────────────────────────────────────────────┐
  │                  Gateway (FastAPI)                   │
  │   /api/schedules/*  /api/schedules/{id}/runs/*       │
  │ ┌──────────────┐  ┌───────────────────────────────┐  │
  │ │ScheduleRouter│  │ ScheduleService                │  │
  │ └──────┬───────┘  └──────────────┬─────────────────┘  │
  │        │                         │                    │
  │        │              ┌──────────▼─────────────┐      │
  │        │              │ SchedulerEngine         │      │
  │        │              │  APScheduler(AsyncIO)   │      │
  │        │              │  + SQLAlchemyJobStore    │      │
  │        │              └──────────┬──────────────┘      │
  │        │                         │ fire                │
  │        │              ┌──────────▼──────────────┐      │
  │        │              │ ScheduleExecutor        │      │
  │        │              │  per-task QueueWorker   │      │
  │        │              │  + retry / backoff      │      │
  │        │              └──────────┬──────────────┘      │
  │        │                         │ RunManager.create  │
  │        │                         ▼                    │
  │        │                    RunManager → StreamBridge │
  │        │                         │                    │
  │        │                  OutboundMessage             │
  │        │                         │                    │
  │        │                  MessageBus                  │
  │        │                         │                    │
  │        │              FeishuChannel._on_outbound      │
  │        ▼                                                │
  │  Persistence:                                         │
  │    schedules  schedule_runs  schedule_subscriptions    │
  │    + apscheduler jobs (SQLAlchemyJobStore)            │
  └─────────────────────────────────────────────────────┘
                              ▲
                              │ tool calls (in-thread)
  ┌───────────────────────────┴──────────────────────────┐
  │             DeerFlow Lead Agent                      │
  │  - "每天 9 点汇总 X" → schedule_create               │
  │  - schedule_subscribe / schedule_list / ...          │
  └──────────────────────────────────────────────────────┘
```

## 5. Data model

All tables live in `deerflow.persistence.models.schedule` and are created via Alembic migration.

### 5.1 `schedules`
| Field | Type | Notes |
|---|---|---|
| `id` | str (ULID) | PK |
| `owner_user_id` | str | creator (DeerFlow user id) |
| `title` | str | user-facing name |
| `kind` | enum | `cron` / `one_shot` |
| `cron_expr` | str? | required when `kind=cron` |
| `run_at` | datetime? | required when `kind=one_shot` |
| `cron_tz` | str | IANA TZ, default `Asia/Shanghai` |
| `prompt` | str | instruction sent to the agent on fire |
| `thread_id` | str? | optional bound thread (reuses context); None ⇒ new thread per run |
| `target_json` | str | serialised target (see 5.4) |
| `status` | enum | `active` / `paused` / `deleted` |
| `apscheduler_job_id` | str? | APScheduler's internal id for recovery |
| `next_fire_at` | datetime? | denormalised for UI |
| `last_fire_at` | datetime? | denormalised for UI |
| `created_at` / `updated_at` | datetime | |
| `source` | str | `im` / `web` / `api` |

### 5.2 `schedule_subscriptions`
| Field | Type | Notes |
|---|---|---|
| `schedule_id` | str (FK) | PK (composite) |
| `user_id` | str | PK (composite); the subscriber |
| `target_json` | str | subscriber can override push target |
| `enabled` | bool | |
| `created_at` | datetime | |

### 5.3 `schedule_runs`
| Field | Type | Notes |
|---|---|---|
| `id` | str (ULID) | PK |
| `schedule_id` | str (FK) | |
| `subscriber_user_id` | str? | when fired for a subscriber, who it was for |
| `run_id` | str? | the langgraph run id (None for pre-run failures) |
| `status` | enum | `queued` / `running` / `succeeded` / `failed` / `canceled` |
| `attempt` | int | 1..N |
| `error_summary` | str? | short reason for failure |
| `started_at` / `finished_at` | datetime | |
| `next_retry_at` | datetime? | |

### 5.4 `target_json` shape
```json
{
  "channel": "feishu",
  "connection_id": "<ulid or null>",
  "chat_id": "oc_xxx",
  "thread_ts": "om_xxx",
  "owner_user_id": "u_xxx"
}
```

## 6. REST API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/schedules` | create |
| GET  | `/api/schedules` | list; filters: `owner=me`, `subscribed=me`, `status` |
| GET  | `/api/schedules/{id}` | detail |
| PATCH | `/api/schedules/{id}` | partial update; owner-only |
| DELETE | `/api/schedules/{id}` | soft delete; owner-only |
| POST | `/api/schedules/{id}/pause` | owner-only |
| POST | `/api/schedules/{id}/resume` | owner-only |
| POST | `/api/schedules/{id}/subscribe` | current user subscribes |
| DELETE | `/api/schedules/{id}/subscribe` | current user unsubscribes |
| GET  | `/api/schedules/{id}/runs` | run history, paginated |
| GET  | `/api/schedules/{id}/runs/{rid}` | one run detail |

Auth: `get_current_user()`. Mutations on a schedule require `owner_user_id == current_user`. Subscription endpoints only require a logged-in user. `target_json.connection_id` semantics:
- If non-null, it must belong to `owner_user_id` (enforced through `ChannelConnectionRepository`).
- If null, the owner's default connection for the target channel is used (looked up via `ChannelConnectionsConfig` runtime).

Request body example:
```json
POST /api/schedules
{
  "title": "每日销售日报",
  "kind": "cron",
  "cron_expr": "0 9 * * *",
  "cron_tz": "Asia/Shanghai",
  "prompt": "汇总昨日销售并推送到此对话",
  "thread_id": null,
  "target": { "channel": "feishu", "connection_id": "...", "chat_id": "oc_..." },
  "source": "im"
}
```

## 7. Agent tools

Module: `deerflow.tools.builtins.schedule_tool.py`. Registered alongside `ask_clarification` — only enabled when a tool context indicates "may schedule" (i.e. user-facing chat sessions).

| Tool | Purpose |
|---|---|
| `schedule_create(title, kind, cron_expr?, run_at?, prompt, target?, source='im')` | create; if `target` is missing, the tool binds the current inbound target |
| `schedule_list(scope='mine'\|'subscribed', limit=20)` | list current user's |
| `schedule_get(id)` | detail |
| `schedule_update(id, fields...)` | edit |
| `schedule_pause(id)` / `schedule_resume(id)` | on/off |
| `schedule_delete(id)` | soft delete |
| `schedule_subscribe(id)` / `schedule_unsubscribe(id)` | subscribe |
| `schedule_runs(id, limit=10)` | history |

Flow when the user expresses an intent in natural language:
1. Agent calls `ask_clarification` to confirm cron, TZ, and push target (one round-trip).
2. Agent calls `schedule_create` with the resolved fields.
3. Agent's final reply confirms ("已设好，明早 9 点见 ✅") with a deep-link to the Web UI schedule page.

## 8. Web UI

- Route: `/schedules`
- List view: filters (mine / subscribed / paused), search by title, columns: title, kind, next fire, last result, status.
- Detail drawer: cron preview (with `next 5 fire times` computed), target preview, recent 10 runs (status + error summary + link to langgraph run).
- Create/edit form: cron expression field (with a small "自然语言" box that reuses the agent flow to produce a cron); one-shot field for date+time; target form (channel=feishu locked in MVP, chat_id, thread_ts optional).
- Buttons: pause / resume / delete.

## 9. Scheduling & execution

### 9.1 Lifecycle
- `SchedulerEngine` is started in `gateway/app.py` lifespan, in parallel with `ChannelService`.
- On start: read `scheduling.*` config, build `AsyncIOScheduler` with `SQLAlchemyJobStore`, load all `status=active` schedules, `add_job` for each (rebuild `apscheduler_job_id` if missing), start `ScheduleExecutor` workers.
- On stop: `scheduler.shutdown(wait=False)` + drain in-flight runs.

### 9.2 Create
1. Validate `cron_expr` (croniter) / `run_at > now` / `target` shape.
2. Insert into `schedules` (status=active).
3. `scheduler.add_job(...)`, store `apscheduler_job_id`.
4. Persist `next_fire_at`.

### 9.3 Fire
APScheduler callback enqueues a per-(schedule, subscriber) work item. The executor:
1. Skips if schedule is paused / deleted → mark `canceled`.
2. For each enabled subscription (including owner):
   - `run_id = RunManager.create_or_reject(...)` with `metadata.source=schedule` and bound `thread_id` (schedule's, or new).
   - Await run completion via `StreamBridge` / run wait API.
   - Persist `schedule_runs` row.
3. Update `last_fire_at` / `next_fire_at`.
4. Push `OutboundMessage` to the subscriber's `target_json`.

### 9.4 Push (no incoming context)
- `OutboundMessage.thread_id` becomes `str | None` (currently required).
- `FeishuChannel._on_outbound` falls back to `CreateMessage` (chat-level) when `thread_id` is None, instead of `ReplyMessage`.
- Format:
  - success: `📅 <title>（尝试 N）\n<agent final text>`
  - retry: prefix `🔄 重试 (N/3)` when `attempt > 1`
  - failure: `❌ <title> 失败：<error_summary>\n查看：<run url>`
  - truncate to `push.max_text_length` (default 4000); append artifact list.

### 9.5 Re-entrancy: Queue with bounded capacity
- Per `(schedule_id, subscriber_user_id)` asyncio queue, `maxsize = executor.per_schedule_queue_size` (default 8).
- When full: drop with a warn log, mark a synthetic `schedule_runs` row `status=failed` with `error_summary=queue_full`, and push a "queue full" alert to the target.
- Optional `persist_queue: true` mode writes queued items into `schedule_runs.status=queued` so they survive restart (worker uses `SELECT ... FOR UPDATE SKIP LOCKED`).

### 9.6 Failure handling
- Retryable: agent exception, tool exception, push 5xx / network.
- Non-retryable: push 4xx, user cancel, guardrail deny.
- `attempt` increments on retry; `next_retry_at = now + backoff_seconds[attempt-1]`.
- `attempt > 3`: `status=failed` + final push alert (alert push itself does not retry).
- Pause / delete cancels `next_retry_at` for that schedule.

## 10. Configuration

Add to `config.example.yaml` and bump `config_version`. All `scheduling.*` fields are **startup-only** (add to `STARTUP_ONLY_FIELDS` and annotate with `description`).

```yaml
scheduling:
  enabled: true
  timezone: Asia/Shanghai
  executor:
    max_concurrent_runs: 8
    per_schedule_queue_size: 8
    persist_queue: false
  retry:
    max_attempts: 3
    backoff_seconds: [60, 300, 900]
  push:
    max_text_length: 4000
    include_run_url: true
  apscheduler:
    coalesce: true
    max_instances: 1
    misfire_grace_seconds: 300
limits:
  max_active_schedules_per_user: 50
```

## 11. Errors & edge cases

| Stage | Error | Behaviour |
|---|---|---|
| Create | bad cron / past run_at / missing target field | 422 |
| Start recovery | APScheduler job missing or stale | re-`add_job`; warn log |
| Fire | schedule paused / deleted | skip; mark canceled |
| Fire | subscriber user / target gone | one run fails; others continue; final alert |
| Run | AgentException / ToolException | retry (9.6) |
| Run | cancel / guardrail deny | cancel; no retry |
| Push | feishu 4xx | fail-fast; alert |
| Push | feishu 5xx | retry (9.6) |
| Push | MessageBus queue full | drop; alert |
| DB | alembic migration fails | startup abort |

## 12. Multi-instance (known gap)

MVP assumes single Gateway. Mitigation on start: try to acquire `pg_try_advisory_lock` (Postgres) or `BEGIN IMMEDIATE` (SQLite) — if it fails, the second Gateway serves HTTP but does **not** start the scheduler. Full leader election (Consul / etcd / existing store) is a follow-up.

## 13. Module list (new / changed)

| Module | Layer | Status |
|---|---|---|
| `deerflow/persistence/models/schedule.py` | harness | new |
| `deerflow/persistence/schedule_repo.py` | harness | new |
| `deerflow/tools/builtins/schedule_tool.py` | harness | new |
| `deerflow/config/scheduling.py` | harness | new (config schema) |
| `app/scheduling/scheduler.py` | app | new |
| `app/scheduling/executor.py` | app | new |
| `app/scheduling/service.py` | app | new |
| `app/gateway/routers/schedules.py` | app | new |
| `app/gateway/app.py` (lifespan) | app | small change |
| `app/channels/message_bus.py` (`OutboundMessage.thread_id` optional) | app | small change |
| `app/channels/feishu.py` (CreateMessage fallback) | app | small change |
| `config.example.yaml` + `STARTUP_ONLY_FIELDS` | both | small change |
| `frontend/.../schedules` | frontend | new |
| Alembic migration | harness | new |
| `backend/CLAUDE.md` / `README*.md` | docs | update |

Strict `app → deerflow` boundary: scheduler, APScheduler dependency, and Feishu fallback all live in `app.*`. Harness only adds model + repo + agent tool.

## 14. Security

- Auth: `get_current_user()` on every REST path.
- Authorization: only `owner_user_id` can mutate / pause / delete.
- Connection ownership: `target.connection_id` must belong to the schedule's `owner_user_id`.
- Prompt injection: `metadata.source="schedule"` is set; the lead-agent system prompt is updated to note that no human is present and irreversible actions should be cautious.
- Resource cap: `max_active_schedules_per_user` (default 50).

## 15. Testing

| Level | Cases |
|---|---|
| Unit | cron validation, target validation, backoff math, retry cap, queue-full drop, leader-lock acquire/release |
| Repo | CRUD + subscription joins + run history |
| Scheduler integration | `freezegun` + fake clock; APScheduler fire → executor → RunManager mock → push mock |
| Feishu | `_on_outbound` with `thread_id=None` uses `CreateMessage`, not `ReplyMessage` |
| IM entry e2e | inbound → agent → `schedule_create` → DB row → recovery → fire → push back |
| Blocking-IO gate | `tests/blocking_io/test_scheduler_lifespan.py` + `test_schedule_recovery.py` anchor `asyncio.to_thread` offload |
| E2E | `tests/e2e/test_schedule_e2e.py`: 1 cron + 1 one-shot via fake clock, verify push and retry |

## 16. Definition of Done

- `/schedules` page creates / pauses / resumes / deletes and shows history.
- Feishu conversation: "每天 9 点汇总 X" → agent confirms → DB row → fake-clock fires → result pushed back to the original thread.
- One schedule with N subscribers: each gets their own run; one bad target doesn't block the others.
- Run-stage failure: 3 attempts with backoff, then a `❌` alert.
- Feishu 4xx: immediate alert, no retry.
- Restart Gateway: all `active` schedules recover; `next_fire_at` recalculates correctly.
- Two Gateway replicas simulated: only the leader starts the scheduler.

## 17. Open questions / follow-ups

1. Multi-Gateway leader election (Consul / etcd / built-in store).
2. Push to Slack / Telegram / DingTalk / WeCom.
3. Cross-schedule dependencies ("run B after A succeeds").
4. Web-side natural-language → cron preview (re-uses the agent flow).
5. Per-user quota and metering (if a SaaS tier is added).
