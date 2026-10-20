# 数字员工协同：多 Agent Hub-and-Spoke 设计方案

> 目标：在 DeerFlow 之上落地多个"数字员工"（虚拟运行值班员、虚拟系统管理员、虚拟 DBA ……），以 **Hub-and-Spoke** 拓扑 + **Custom Agent** 为组织单元，让值班/告警/工单这类长生命周期、可审计的运维任务在同一个 Gateway 里并发运行，而不需要复制一份网关。

本文是 [lead-agent-vs-subagent.md](./lead-agent-vs-subagent.md) 的姊妹篇 —— 那篇讲清楚"Lead Agent / SDK Agent / Subagent 三层"是什么，本文讲清楚"如何在这三层上搭多个数字员工"。

---

## 1. 范围与已确认的关键决策

| 维度 | 决策 | 备选 |
| --- | --- | --- |
| 拓扑 | **Hub-and-Spoke**（所有数字员工复用同一 Gateway 入口） | 各自一个独立 Gateway / Subagent-only |
| 首批员工 | **运行值班员 (oncall_operator)**，可平滑扩展 DBA / SRE | 一次性铺开多种角色 |
| 触发 | **混合：Webhook + Scheduled** | 纯 IM / 纯定时 |
| 动作 | **只读 + 工单系统**（不允许执行写命令） | 完整 Shell / 受限 Shell |
| 渠道 | **飞书 (Feishu)** | Slack / 钉钉 / Web UI |
| 身份 | **独立系统用户 + 角色**（不是某个真人） | 复用值班同事的 IM 身份 |
| 知识 | **Thread 记忆 + Skills runbook** | 纯知识库 / 全量 RAG |
| 形态 | **Custom Agent**（`users/oncall/agents/oncall_operator/`） | 硬编码新模块 |
| Hub 分派 | **用户手动选 Agent**（前端下拉、IM 端 `assistant_id`） | 关键字路由 / 自动判断 |

下面所有设计都围绕这九条决策展开。

---

## 2. 整体架构

```
                          ┌──────────────────────────┐
   Prometheus Alert ───┐  │   Gateway (8001)         │
   Webhook POST ────────┼─►│  /api/oncall/ingest     │──┐
                        │  │  /api/langgraph/threads  │  │
   Feishu 群消息 ───────┘  │       (RunManager)       │  │
                          │                          │  │
   飞书群 / 个人消息 ────► │  /api/feishu (channels)  │  │
                          │                          │  │
   前端下拉选 Agent ────► │  /api/langgraph/threads  │  │
                          │                          │  │
   定时 cron ──────────► │  scheduler (in-process)  │  │
                          └────────┬─────────────────┘  │
                                   │  assistant_id       │
                                   ▼                     │
                          ┌──────────────────────────┐   │
                          │  lead_agent (Hub)        │   │
                          │  - agent_name 分派        │   │
                          │  - 复用同一份 subagent    │   │
                          │    执行器 / 沙箱         │   │
                          └──┬─────────┬─────────┬───┘   │
                             │         │         │       │
                             ▼         ▼         ▼       │
                       oncall_op.  dba_agent  sre_agent   │
                       (Custom)    (Custom)   (Custom)    │
                          │                       │
                          ▼                       │
                   飞书群 / 邮件 / 工单 ◄──────────┘
                          ▲
                          │
                   run_events 落库（审计）
```

**关键事实**（[lead-agent-vs-subagent.md](./lead-agent-vs-subagent.md) §3）：

- `lead_agent` 是 LangGraph-compatible 入口；每个 Custom Agent **不** 独立进程、不独立网关、不独立端口 —— 都是 `lead_agent` 在 `assistant_id` / `agent_name` 维度上的一个实例化变体。
- Hub 不需要新建模块：Frontend 选 `oncall_operator` ⇒ `assistant_id=oncall_operator` ⇒ `runtime_ctx["agent_name"]="oncall_operator"` ⇒ `load_agent_config("oncall_operator", user_id)` 命中 `users/{user_id}/agents/oncall_operator/SOUL.md`（[worker.py:993](backend/packages/harness/deerflow/runtime/runs/worker.py#L993), [context_compaction.py:121-185](backend/packages/harness/deerflow/runtime/context_compaction.py#L121-L185)）。
- 这意味着 **新增一个数字员工 = 新增一个 `users/{user_id}/agents/{role}/` 目录**，不需要改 Gateway、不需要发版。

---

## 3. 三层模型在本方案里的角色

回顾 [lead-agent-vs-subagent.md](./lead-agent-vs-subagent.md) §3 的三层：

| 层 | 在本方案里的角色 | 数字员工对应物 |
| --- | --- | --- |
| **Lead Agent** (`make_lead_agent`) | **Hub**：按 `agent_name` 分派到具体的 Custom Agent；承载共享中间件（sandbox、subagent runtime、summarization、plan mode、scheduler） | 一个常驻的 `lead_agent` 进程 + `subagent_runtime` 实例 |
| **SDK Agent** (`create_deerflow_agent`) | **Custom Agent 实例化**：每个 `users/{user_id}/agents/{role}/SOUL.md` 编译出一个 `create_deerflow_agent` 图，注入该角色专属的 `tool_groups` / `allowed_subagents` / `skills` | `oncall_operator.py`、`dba_agent.py`（编译产物） |
| **Subagent** (`task` 工具) | **执行手脚**：值班员委派给只读 subagent（`bash` 工具禁用 `write_file`），DBA 委派给 `general-purpose` 跑慢查询分析 | `bash`（只读 wrapper）、`general-purpose`、`network` |

这条 **"Lead 分派 → Custom Agent 编译 → Subagent 执行"** 的链路让"加一个员工"和"加一个 subagent"是两件不同的事：

- 加员工 = 加一个 `users/{user_id}/agents/{role}/`（一行 `make` 不需要）
- 加 subagent = 改 `subagents.custom_agents` 或新建 `subagents/builtins/{name}.py`（需要发版）

---

## 4. Custom Agent 形态（推荐落点）

### 4.1 选 `Custom Agent` 而不是新建模块的三个理由

1. **零发版**：`agents_api: { enabled: true }` 即可走 HTTP API 注册/编辑；或者直接文件落盘到 `users/{user_id}/agents/{role}/SOUL.md`，下次 Gateway 启动自动加载（[persistence/agents/file.py:94](backend/packages/harness/deerflow/persistence/agents/file.py#L94)）。
2. **天然隔离**：每个 Custom Agent 拥有独立 `user_id`（系统用户）、独立 thread namespace、独立 checkpoint lineage。
3. **完整 AgentConfig 字段**：直接用 `AgentConfig` 的所有约束（`tool_groups`、`skills`、`allowed_subagents`、`memory_enabled`、`model_settings`、`github`），不需要再造一遍。
   - 字段定义：[agents_config.py](backend/packages/harness/deerflow/config/agents_config.py)

### 4.2 目录结构

```
backend/.deer-flow/
├── users/
│   └── ops/                              # 系统用户 ops（与人类用户隔离）
│       └── agents/
│           ├── oncall_operator/          # 数字员工 1：虚拟运行值班员
│           │   ├── SOUL.md               # 系统提示（人设 / 角色边界 / 流程）
│           │   ├── config.yaml           # AgentConfig：tool_groups/skills/subagents
│           │   └── tools/                # 角色专属工具（MCP/内置）
│           │       └── ticket.py         # 工单系统客户端
│           ├── dba_agent/                # 数字员工 2（扩展用）
│           │   ├── SOUL.md
│           │   └── config.yaml
│           └── sre_agent/                # 数字员工 3（扩展用）
│               ├── SOUL.md
│               └── config.yaml
└── skills/
    └── public/                           # 角色共享 runbook（已自动加载）
        ├── oncall-runbooks/
        │   ├── SKILL.md
        │   ├── resources/
        │   │   ├── triage_checklist.md
        │   │   ├── escalation_matrix.md
        │   │   └── runbook_template.md
        │   └── scripts/
        │       └── quick_diagnose.sh     # 只读诊断脚本（被 tool_groups 引用）
        └── ticket-system/                # 工单系统使用指南
            └── SKILL.md
```

> **为什么数字员工放在 `users/ops/` 而不是 `users/alice/`？** —— `users/{user_id}/agents/{name}/` 已经按用户隔离（[persistence/agents/model.py:26](backend/packages/harness/deerflow/persistence/agents/model.py#L26) 的 `uq_agents_user_name`）。把数字员工挂在系统用户 `ops` 下，意味着：
>
> - 不会因为某位值班同事的离职/权限变更而影响数字员工
> - 审计日志里 `user_id=ops` 即可一眼分辨"系统行为 vs 真人操作"
> - 后续如果做"按用户配额"也容易切分
>
> 实现时只需在启动时 bootstrap 一个 `ops` 系统用户（参考已有系统用户模式）。

### 4.3 SOUL.md 模板（oncall_operator）

SOUL.md 编码四个东西：身份、边界、流程、升级路径。下面是骨架（实际落地时填具体内容）：

```markdown
---
name: oncall_operator
display_name: 虚拟运行值班员
description: 7×24 接收告警/工单/咨询，先判后行；只读诊断 + 工单升级
model: sonnet
tool_groups: [bash_readonly, jina, mcp:ticket_system]
skills: [oncall-runbooks, ticket-system]
allowed_subagents: [bash-readonly, general-purpose]
memory_enabled: true          # 记住当前值班窗口的上下文
github: null
---

# 身份
你是 **虚拟运行值班员 (OnCall Operator)**，负责接收来自告警系统、飞书群、值班同事的求助。
你代表的是一个长期在线、冷静、只读、绝不擅自动手写的角色 —— 你的第一反应是"读"，不是"改"。

# 工作模式
1. **收单**：把任何来源的输入归一为"事件"（who / what / when / severity）
2. **初判**：基于 SKILL `oncall-runbooks` 的 triage_checklist 走只读诊断
3. **回复**：在飞书群/工单里发"诊断 + 建议 + 风险等级"
4. **升级**：超出权限（[边界]）时立刻建工单 → 升级到对应业务 SRE

# 边界（强制）
- ✅ 允许：所有只读命令（`ls / cat / grep / curl -I` / `kubectl get` / `tail logs`）
- ✅ 允许：调用 `mcp:ticket_system` 创建/查询/关闭工单
- ❌ 禁止：任何写命令（`kubectl apply / rm / sed -i / echo > file / DB UPDATE`）
- ❌ 禁止：执行 `present_files` / `view_image`（这些是给人看的，不是给机器）
- ❌ 禁止：调用 `task` 把活儿转给 `bash`（必须是 `bash-readonly`）

# 工具说明
- `bash_readonly`：受限 Shell，自动剥除任何 `>` / `>>` / `sed -i` / `kubectl apply` 关键字
- `jina` / `mcp:jina`：读外部页面（故障公告/状态页）
- `mcp:ticket_system`：工单系统（创建/查询/评论/关闭）

# 升级矩阵
| 严重度 | 升级目标 | 渠道 |
| --- | --- | --- |
| P0 全站不可用 | 业务 SRE + 值班经理 | 飞书群 + 电话 |
| P1 单服务异常 | 业务 SRE | 飞书群 + 工单 |
| P2 性能劣化 | DBA-Agent | 飞书群 + 工单 |
| P3 咨询类问题 | 自动回复 + 归档 | 飞书群 |
| 越权请求 | 拒绝 + 工单留痕 | 飞书群 + 工单 |

# 风格
- 中文优先；引用 runbook 用相对路径（`./resources/...`）
- 不要重复造 runbook，先 cat 再判断
- 同一事件多次被问，引用上一次诊断而不是重做
```

### 4.4 config.yaml（AgentConfig）

```yaml
# backend/.deer-flow/users/ops/agents/oncall_operator/config.yaml
name: oncall_operator
display_name: 虚拟运行值班员
description: 7×24 接收告警，先判后行
model: sonnet
tool_groups:
  - bash_readonly       # 受限 Shell（写命令被中间件拦下）
  - jina                # 读外部页面
  - mcp:ticket_system   # 工单系统
skills:
  - oncall-runbooks
  - ticket-system
allowed_subagents:
  - bash-readonly
  - general-purpose
memory_enabled: true    # 跨 run 保留上下文（值班窗口粘性）
model_settings:
  max_tokens: 8192
thinking_enabled: true
```

> `tool_groups` 字段是 AgentConfig 强约束（[agents_config.py:224](backend/packages/harness/deerflow/config/agents_config.py#L224)）；`mcp:ticket_system` 这种命名空间前缀的 tool_group 由 Gateway 在装配 agent 时按 `extensions_config.json -> mcp_servers` 解析。

---

## 5. 只读 + 工单系统：工具组的设计

### 5.1 为什么不能直接给值班员 `bash` 工具

参考 [agents/factory.py](backend/packages/harness/deerflow/agents/factory.py) 的 `create_deerflow_agent`：

- `bash` 工具来自 `deerflow.sandbox.tools`，提供 `bash / ls / read_file / write_file / str_replace` 五件套。
- `SubagentConfig.tools` 字段（[subagents/config.py](backend/packages/harness/deerflow/subagents/config.py)）接受任意子集，但没有"运行时再过滤"语义 —— 也就是说**给 `bash` subagent 即等于给了写**。

### 5.2 推荐做法：派生一个 `bash-readonly` subagent

在 `subagents/builtins/` 下新增一个只读变体（不修改 `bash_agent.py` 本身）：

```python
# backend/packages/harness/deerflow/subagents/builtins/bash_readonly.py
BASH_READONLY_CONFIG = SubagentConfig(
    name="bash-readonly",
    description="Read-only shell for triage. NO write/apply/sed -i.",
    system_prompt=(
        "You are a read-only shell operator. "
        "Any command containing '>', '>>', 'sed -i', 'kubectl apply', "
        "'kubectl delete', 'rm ', 'kubectl edit' MUST be refused. "
        "Prefer `kubectl get/describe/logs`, `cat`, `grep`, `head`, `tail`, `jq`."
    ),
    tools=["bash", "ls", "read_file"],   # 没有 write_file / str_replace
    disallowed_tools=["write_file", "str_replace", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=40,
    timeout_seconds=180,
)
```

并把它加入 `BUILTIN_SUBAGENTS` 字典（[subagents/builtins/\_\_init\_\_.py](backend/packages/harness/deerflow/subagents/builtins/__init__.py)）：

```python
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
    "bash-readonly": BASH_READONLY_CONFIG,   # 新增
}
```

> **为什么不靠 `tool_groups` 过滤**？—— `tool_groups` 是 lead agent 这一层选工具组用的；subagent 内部仍然拿全量 sandbox 工具。**真正的安全边界要落到 subagent 自己的 `disallowed_tools`**，这样 subagent 即便被绕过也无法写。

### 5.3 工单系统接入（`mcp:ticket_system`）

工单系统推荐走 MCP：

1. 在 `extensions_config.json` 注册 MCP server：

   ```json
   {
     "mcpServers": {
       "ticket_system": {
         "command": "uvx",
         "args": ["ticket-mcp", "--config", "/etc/oncall/ticket.yaml"],
         "env": { "TICKET_API_KEY": "..." }
       }
     }
   }
   ```

2. 工具命名空间变成 `mcp:ticket_system` → 出现在 AgentConfig 的 `tool_groups: [mcp:ticket_system]`。

3. `extensions_config.json` 的写入语义（重要！）：[extensions_config.json 是 Gateway 在 `PUT/PATCH /api/mcp/config` 时写入的](../AGENTS.md)，生产部署必须挂为可写（compose `:rw`，Helm 启动时从 ConfigMap 复制到 home volume）。

4. 把工单工具的"使用约定"沉淀到 Skill：`skills/public/ticket-system/SKILL.md` —— "创建工单时先 `query` 避免重复"、"评论时引用 run_id"。

---

## 6. 触发层：Webhook + Scheduled 混合

### 6.1 触发矩阵

| 触发源 | 入口 | 用途 | 是否要求"独立线程" |
| --- | --- | --- | --- |
| Prometheus AlertManager webhook | `POST /api/oncall/ingest`（新建） | 告警/事件 | ✅ 每事件一线程 |
| 飞书群消息 | `channels.feishu` 已有 | 用户咨询 | ❌ 复用群 thread |
| 定时巡检 | scheduler 已有 | 周期巡检 | ✅ 每 cron tick 一线程 |
| 前端 UI | 已有 `/api/langgraph/threads` | 值班同事介入 | ❌ 沿用 thread |
| HTTP API 主动调用 | 已有 | 集成触发 | ✅ |

### 6.2 Webhook 入口（**需要新增**）

新增 `POST /api/oncall/ingest` 端点（放在 `app/gateway/routers/oncall.py`）：

```python
@router.post("/api/oncall/ingest")
async def ingest_alert(request: Request) -> Response:
    """Prometheus AlertManager / 第三方告警的统一入口。"""
    body = await request.json()
    # 1. 规范化（AlertManager 通用格式 → 内部 event 格式）
    event = normalize_alert(body)
    # 2. 创建/复用 thread（按 alert fingerprint 去重）
    thread_id = await oncall_thread_service.get_or_create(event.fingerprint, event.severity)
    # 3. 以 oncall_operator 身份投递（assistant_id = oncall_operator）
    await run_agent(
        thread_id=thread_id,
        assistant_id="oncall_operator",
        user_id="ops",
        input=event.render(),
        config={"configurable": {"agent_name": "oncall_operator", "non_interactive": True}},
    )
    return {"thread_id": thread_id, "status": "accepted"}
```

> **关键设计点**：
> - `assistant_id="oncall_operator"` 命中 Hub 分派（[worker.py:993](backend/packages/harness/deerflow/runtime/runs/worker.py#L993)）
> - `configurable.agent_name="oncall_operator"` 保证 checkpoint lineage 正确（[context_compaction.py:1089-1098](backend/packages/harness/deerflow/runtime/runs/worker.py#L1089-L1098)）
> - `non_interactive=True` 让 lead agent 的中间件**主动屏蔽 `ask_clarification` 工具**（参考 [lead-agent-vs-subagent.md](./lead-agent-vs-subagent.md) §6 中关于 `disable_clarification` 的设计）
> - 告警 **去重**（同 fingerprint 复用 thread）避免告警风暴把线程打满
> - 这个端点**不是** Subagent 也不是 SDK Agent 改动 —— 只是 Gateway 上加一个普通 HTTP 路由，对外契约稳定

### 6.3 定时巡检（**已有 scheduler**）

scheduler 已有，只需配置启用（[config.example.yaml:2447](../config.example.yaml)）：

```yaml
# config.yaml
scheduler:
  enabled: true
  multi_instance: false     # 起步用单实例；扩多副本要加 lease/Postgres
  poll_interval_seconds: 5
  max_concurrent_runs: 3
  recursion_limit: 1000
```

定时任务的"具体内容"通过 API 注册到 `scheduled_tasks` 表（参考 [backend/AGENTS.md Scheduled-task note](../AGENTS.md)）：

```sql
INSERT INTO scheduled_tasks (id, name, cron, assistant_id, user_id, input, config, enabled)
VALUES (
  'oncall-5min-heartbeat',
  '每 5 分钟例行巡检',
  '*/5 * * * *',
  'oncall_operator',          -- 走 Hub 分派
  'ops',                      -- 系统用户
  '执行 skills/oncall-runbooks 中 5min_heartbeat 流程',
  '{"configurable": {"non_interactive": true}}',
  true
);
```

> **注意**：scheduled-task 也走 Hub（同一个 `assistant_id="oncall_operator"`），所以同一份 SOUL.md 既服务告警又服务巡检，差别只在 input。

---

## 7. 飞书渠道绑定

### 7.1 复用现有 channels

飞书 channel 已经在 [app/channels/feishu/](backend/app/channels/feishu/) 实现。要让值班员既能"接"又能"回"，配置如下（[config.example.yaml:2644](../config.example.yaml)）：

```yaml
# config.yaml
channels:
  feishu:
    enabled: true
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
    allowed_users: []         # 先放开，跑稳再加白名单

  session:
    assistant_id: oncall_operator      # 飞书渠道默认走值班员
    config:
      recursion_limit: 100
    context:
      thinking_enabled: true
      non_interactive: true            # 飞书消息异步到达，禁用澄清
      subagent_enabled: true           # 允许值班员委派 subagent
```

`session.assistant_id=oncall_operator` 是 **Hub 分派的入口**：所有从飞书过来的消息都先经过 lead agent，再被分派到 oncall_operator（[config.example.yaml:2636](../config.example.yaml) 注释：`custom agents route via lead_agent + agent_name`）。

### 7.2 多员工并存：飞书群 → 不同员工

一个飞书机器人可以服务多个员工 —— 在前端/UI 把"群"和"员工"绑定：

- `oncall_alerts` 群 → 飞书机器人识别为 `assistant_id=oncall_operator`
- `dba_help` 群 → 飞书机器人识别为 `assistant_id=dba_agent`
- `sre_questions` 群 → 飞书机器人识别为 `assistant_id=sre_agent`

实现路径两条：

| 路径 | 适用 | 说明 |
| --- | --- | --- |
| **A：群级别 session override** | 群固定员工 | 在 `channels.feishu` 加 `groups: { oc_xxx: { assistant_id: oncall_operator } }` |
| **B：消息内 @ 触发** | 群多员工 | 让 `OnCallOperator` / `DbaAgent` 各自有飞书 bot 名，员工识别 `@` |

起步建议走 A —— 一个员工配一个群，简单可审计。

### 7.3 Webhook 触发后通知到飞书

Prometheus 告警 → `POST /api/oncall/ingest` → oncall_operator 跑诊断 → 结论发到飞书群。这条链路已经存在：值班员通过 `mcp:ticket_system` 创建工单后，可以用 `tool_groups: [mcp:feishu_bot]` 主动发消息（如果需要）；否则只发工单，飞书群里有人看到工单号自己去查。

---

## 8. 身份与权限模型

### 8.1 系统用户 bootstrap

启动时确保 `ops` 用户存在（`User` 表的 `id="ops"`，`role="system"`，`display_name="数字员工系统账户"`）。实现方式：

- 在 Gateway 启动时检查 `ops` 是否存在；不存在则建（参考 `bootstrap_admin` 之类已有模式）
- **不要**给 `ops` 配 IM 账号（避免被钓鱼/冒名顶替）
- 所有数字员工共享 `ops`，用 `agent_name` 区分具体角色

### 8.2 权限分层

| 层 | 控制点 | 实现 |
| --- | --- | --- |
| 谁能选哪个员工 | 前端下拉 / 飞书群绑定 | UI 显式枚举可用 `assistant_id` |
| 员工能用什么工具 | `tool_groups` + subagent `disallowed_tools` | SOUL.md + AgentConfig + SubagentConfig 三道 |
| 员工能不能写 | **subagent `disallowed_tools` 才是真边界** | `bash-readonly` 没有 `write_file / str_replace` |
| 员工能跑多久 | `max_turns=50 / 100 / 150` | 见 [subagents/config.py](backend/packages/harness/deerflow/subagents/config.py) |
| 员工能不能升级 | SOUL.md 升级矩阵 | 业务规则，不在代码层强约束 |
| 谁能看到员工对话 | thread 可见性 | 沿用 thread ACL；`user_id=ops` 的 thread 默认仅系统用户 + 管理员可见 |

### 8.3 审计可观测

每个数字员工 run 在 `run_events` 表里都有完整轨迹（与人类用户一致），关键事件：

- `run_started` / `run_finished` / `run_failed`
- `tool_call`（参数 + 工具名 + 返回）
- `subagent_task_started` / `subagent_task_finished`
- `checkpoint_compacted`

查询示例：

```sql
-- 过去 24h oncall_operator 的所有失败 run
SELECT * FROM run_events
WHERE assistant_id = 'oncall_operator'
  AND event_type = 'run_failed'
  AND created_at > now() - interval '24 hours';
```

> 数字员工账号的所有行为天然落在审计里 —— 不需要额外的"影子模式"。

---

## 9. 关键实施 Checklist

按依赖顺序排列：

### 阶段 0：脚手架（必做，1-2 天）

- [ ] 启动脚本：bootstrap `ops` 系统用户
- [ ] 创建 `users/ops/agents/oncall_operator/SOUL.md` + `config.yaml`
- [ ] 在 `BUILTIN_SUBAGENTS` 加 `bash-readonly`（第 5.2 节代码）
- [ ] 创建 `skills/public/oncall-runbooks/SKILL.md`（先放占位 runbook）
- [ ] 创建 `skills/public/ticket-system/SKILL.md`

### 阶段 1：Hub 分派验证（1 天）

- [ ] 前端下拉出现 `oncall_operator`
- [ ] 选 `oncall_operator` 发起一次"测试对话"，确认：
  - SOUL.md 加载正确
  - `tool_groups` 中 `mcp:ticket_system` 出现
  - `subagent_enabled=true` 时 `task` 工具出现
  - `bash` subagent 被拒绝（`bash-readonly` 是默认）
- [ ] checkpoint 写入 `metadata.deerflow_agent_name=oncall_operator`

### 阶段 2：飞书渠道接通（1-2 天）

- [ ] `channels.feishu` 配置 `app_id` / `app_secret`
- [ ] `session.assistant_id=oncall_operator`
- [ ] 在飞书群 `@bot` 测试，确认 Hub 分派
- [ ] `non_interactive=true` 生效（值班员不会问"你想让我做啥"）

### 阶段 3：Webhook 入口（2-3 天）

- [ ] 新增 `POST /api/oncall/ingest`
- [ ] 写 AlertManager → 内部 event 的 normalize 函数
- [ ] 告警去重（fingerprint → thread）
- [ ] 单测：`tests/test_oncall_ingest.py`（normalize、fingerprint 复用、Hub 分派）
- [ ] 集成测试：本地起 AlertManager → POST → 验证飞书群收到回复

### 阶段 4：Scheduled 巡检（1 天）

- [ ] `scheduler.enabled=true` + 单实例
- [ ] 注册 `oncall-5min-heartbeat` scheduled task
- [ ] 验证 thread 独立、`non_interactive=true` 生效
- [ ] 验证 `max_concurrent_runs=3` 不被超（`queued` 落库而非丢）

### 阶段 5：工单 MCP（2-3 天）

- [ ] 选工单系统（MCP 服务端优先；自写也行但成本高）
- [ ] 配 `extensions_config.json` 里的 `mcpServers.ticket_system`
- [ ] 验证 `tool_groups: [mcp:ticket_system]` 解析
- [ ] 写测试工单系统 mock：值班员能"建单/查单/评论/关单"

### 阶段 6：审计 & 告警（持续）

- [ ] 跑通 `run_events` 全文查询
- [ ] 在 Grafana 配 "oncall_operator 失败率/平均耗时" 面板
- [ ] 飞书群收到"连续 3 次失败"自动告警 → 人工介入

### 阶段 7：扩到第二个员工（1-2 天，可复制阶段 0+1）

- [ ] 新建 `users/ops/agents/dba_agent/`（不同 SOUL.md、不同 tool_groups）
- [ ] 第二个飞书群 `dba_help` 绑定
- [ ] 共享 `bash-readonly` subagent，扩一个 `dba-query` subagent（看慢查询）

---

## 10. 关键设计权衡

| 选择 | 备选 | 理由 |
| --- | --- | --- |
| **Custom Agent（SOUL.md 落盘）** | 改 `make_lead_agent` 加新分支 | 零发版；AgentConfig 字段够用 |
| **系统用户 `ops`** | 复用某位人类用户 | 审计清晰；不受人事变动影响 |
| **派生 `bash-readonly` subagent** | 只靠 `tool_groups` 过滤 | 真正的安全边界必须在 subagent 自己的 `disallowed_tools` |
| **MCP 接入工单系统** | 直接 HTTP 调用 | MCP 提供"工单系统即工具"的一致体验，可换后端 |
| **新建 `/api/oncall/ingest` 端点** | 走 IM 机器人收告警 | 告警系统是 HTTP-first，IM 反向不稳定；告警走 HTTP，IM 走聊天 |
| **Scheduler 单实例起步** | 多实例 lease | 多实例要 Postgres + heartbeat + advisory lock，单实例在 [`max_concurrent_runs=3`](../config.example.yaml) 下够用 |
| **手动选 Agent** | 关键字路由 | 第一阶段路由出错代价太高；人知道自己在跟谁说话 |

---

## 11. 风险与已知的坑

### 11.1 Subagent 工具逃逸

- 现象：给 `bash-readonly` subagent 的 system_prompt 写"拒绝写命令"，但 subagent 自己用 `bash -c "echo x > /etc/foo"`。
- 缓解：**双层防御** —— ① `disallowed_tools=["write_file","str_replace"]` 让 sandbox 工具集直接没有这两个；② SOUL.md 明确禁止行；③ 后续可加 `BashSandboxMiddleware` 在命令层做正则拦截。
- 测试：`tests/test_bash_readonly_subagent.py` 验证 5 类常见写命令被拒。

### 11.2 告警风暴 → thread 风暴

- 现象：AlertManager 一次性 200 条告警 → 200 个 thread。
- 缓解：fingerprint 去重（同一 alert 复用 thread），`max_concurrent_runs=3` 兜底（多出来的进 `queued`）。
- 测试：`tests/test_oncall_ingest_dedupe.py`。

### 11.3 飞书异步 → 值班员尝试澄清

- 现象：飞书消息是异步到达的，agent 跑着跑着发现信息不全，调 `ask_clarification` 工具 — 但用户已经离开群。
- 缓解：`configurable.non_interactive=true` → lead agent 中间件把 `ask_clarification` 从工具集移除（[AGENTS.md Scheduled-task note](../AGENTS.md) 关于 `non_interactive` 的描述）。
- 测试：`tests/test_non_interactive_tool_filter.py`。

### 11.4 多员工的 subagent 执行器争抢

- 现象：值班员和 DBA 同时跑任务，subagent 容量不够。
- 缓解：subagent runtime 是 **进程内单例**（[SubagentRuntime](backend/packages/harness/deerflow/subagents/runtime.py)），`max_total_per_run` 默认就 cap 了；不够就在 `config.yaml` 调大。
- **不要**给每个员工单独起 `SubagentRuntime` —— 那会绕过共享上限。

### 11.5 系统用户被误用

- 现象：值班同事知道 `user_id=ops` 后用 admin 后台切过去，假冒数字员工。
- 缓解：`ops` 用户的密码/API key 不存在；只能被 Gateway 内部流程使用；管理员页面里把 `user_id=ops` 的操作标红。

### 11.6 SOUL.md 演进失控

- 现象：值班员 SOUL.md 越改越长，引入矛盾规则。
- 缓解：① 长度上限（例如 8KB）；② 改 SOUL.md 走 PR review；③ 用 [skill-reviewer](../AGENTS.md#) 自检；④ 在 `tests/` 留 regression 测试，把"关键守则"翻译成断言。

### 11.7 Checkpoint lineage 串台

- 现象：值班员的 thread 复用到了 DBA 员工的配置（或反之）。
- 缓解：每次 run 强制写入 `metadata.deerflow_agent_name`（[context_keys.py:12](backend/packages/harness/deerflow/runtime/context_keys.py#L12)），compaction 读这个 metadata 而不是 `request.agent_name`（[context_compaction.py:152-155](backend/packages/harness/deerflow/runtime/context_compaction.py#L152-L155)）。
- 测试：`tests/test_checkpoint_agent_binding.py`。

---

## 12. 验收标准（MVP 阶段）

数字员工"运行值班员"上线必须满足：

- [ ] **可达性**：在 飞书群 `@bot` 收到响应
- [ ] **可达性**：Prometheus AlertManager 推一条 alert，`/api/oncall/ingest` 落库后飞书群收到诊断
- [ ] **可达性**：scheduler 每 5 分钟自动跑一次巡检
- [ ] **可达性**：前端 UI 选 oncall_operator 能发起 run
- [ ] **隔离性**：`user_id=ops` 在 `run_events` 表中清晰可辨
- [ ] **隔离性**：oncall_operator thread 的 `metadata.deerflow_agent_name=oncall_operator`，不串到其他员工
- [ ] **安全性**：`bash-readonly` 拒绝 5 类常见写命令（unit test 覆盖）
- [ ] **安全性**：`mcp:ticket_system` 只能"建/查/评/关"，不能删
- [ ] **安全性**：`non_interactive=true` 时 `ask_clarification` 工具不在工具集
- [ ] **可观测**：所有 run 在 `run_events` 表留痕；Grafana 面板能看到 24h 失败率
- [ ] **可回滚**：删除 `users/ops/agents/oncall_operator/` 即可下线值班员（无代码改动）
- [ ] **可复制**：照着阶段 0+1 复制一份 `dba_agent` ≤ 2 天工作量

---

## 13. 关键文件索引

| 主题 | 文件 | 关键行 |
| --- | --- | --- |
| Hub 分派核心 | [backend/packages/harness/deerflow/runtime/runs/worker.py](backend/packages/harness/deerflow/runtime/runs/worker.py) | 993, 1089-1098 |
| Checkpoint agent 绑定 | [backend/packages/harness/deerflow/runtime/context_compaction.py](backend/packages/harness/deerflow/runtime/context_compaction.py) | 121-185 |
| Agent 名 checkpoint key | [backend/packages/harness/deerflow/runtime/context_keys.py](backend/packages/harness/deerflow/runtime/context_keys.py) | 12 |
| AgentConfig schema | [backend/packages/harness/deerflow/config/agents_config.py](backend/packages/harness/deerflow/config/agents_config.py) | 224 |
| Custom Agent 落盘 | [backend/packages/harness/deerflow/persistence/agents/file.py](backend/packages/harness/deerflow/persistence/agents/file.py) | 94 |
| Subagent config | [backend/packages/harness/deerflow/subagents/config.py](backend/packages/harness/deerflow/subagents/config.py) | — |
| Subagent builtin 注册 | [backend/packages/harness/deerflow/subagents/builtins/\_\_init\_\_.py](backend/packages/harness/deerflow/subagents/builtins/__init__.py) | — |
| Subagent runtime 共享 | [backend/packages/harness/deerflow/subagents/runtime.py](backend/packages/harness/deerflow/subagents/runtime.py) | 22-78 |
| Scheduler 配置 | [config.example.yaml](config.example.yaml) | 2447 |
| 飞书 channel 入口 | [config.example.yaml](config.example.yaml) | 2644 |
| Channel session.assistant_id | [config.example.yaml](config.example.yaml) | 2636 |
| 现有 channel 代码 | [backend/app/channels/](backend/app/channels/) | — |
| Gateway 路由点 | [backend/app/gateway/routers/](backend/app/gateway/routers/) | 新增 `oncall.py` |
| run_events 持久化 | [backend/packages/harness/deerflow/persistence/run_events/](backend/packages/harness/deerflow/persistence/run_events/) | — |

---

## 14. 与其他文档的关系

- 三层模型（Lead / SDK Agent / Subagent）—— 见 [lead-agent-vs-subagent.md](./lead-agent-vs-subagent.md)
- 长期任务的"维护者 + 编排者"模式 —— 见 [maintainer-orchestrator-design.md](./maintainer-orchestrator-design.md)
- Scheduler 的并发模型 —— 见 [backend/AGENTS.md Scheduled-task note](../AGENTS.md)
- MCP 工具如何挂载 —— 见 [backend/AGENTS.md](../AGENTS.md) "Long-running MCP work" 节
- Skill 编写规范 —— 见 [skills/public/skill-reviewer/](../AGENTS.md)（skill-reviewer 自身）

---

**TL;DR**：在 DeerFlow 里做"多个数字员工"的最短路径是 **Custom Agent + Hub 复用 + 角色系统用户**。Lead Agent 已经是 Hub（`assistant_id` 维度分派），不需要新建模块；每加一个员工 = 落一个 `users/ops/agents/{role}/` 目录。安全和审计天然落在 subagent `disallowed_tools` + `run_events` 上。
