# DeerFlow: Lead Agent / Agent / Subagent — 区别与联系

> **Written for:** engineers new to the DeerFlow codebase who need a mental
> model of the three different "agent" things they will encounter.
> Source of truth: `backend/packages/harness/deerflow/{agents,subagents}/`.
> Re-derived from `agents/AGENTS.md`, `subagents/AGENTS.md`, and the symbols
> `make_lead_agent` / `create_deerflow_agent` / `SubagentExecutor` /
> `BUILTIN_SUBAGENTS`. If this doc and the source disagree, the source wins.

---

## 1. 一句话总览

DeerFlow 里有 **三层** "agent",层级严格扁平,**只有 Lead 可以调度 subagent,subagent 之间互不可见**:

| 层级 | 是什么 | 谁实例化 | 谁能再 spawn |
|---|---|---|---|
| **Lead Agent** | 顶级 LangGraph 图,LangGraph Server 注册的入口 | Gateway / LangGraph Server / 直接 SDK | 可以调 `task` / `batch_task` |
| **Agent** (SDK factory) | 纯 Python 参数的 `create_deerflow_agent(...)`,被 Lead Agent 内部使用,也供第三方直接调用 | 直接调用者 / Lead agent 工厂 | 视配置而定 |
| **Subagent** | 受 `SubagentConfig` 限制的专用 agent(general-purpose / bash / 自定义),**不能调用 `task`** | Lead agent 通过 `task` 工具 | **不能** |

---

## 2. 一图看懂 (mental model)

```
┌─────────────────────────────────────────────────────────────────┐
│ LangGraph Server entrypoint                                     │
│   langgraph.json: { "graph": "deerflow.agents:make_lead_agent" } │
│                                                                  │
│   def make_lead_agent(config: RunnableConfig) -> CompiledGraph   │
│       │                                                          │
│       ▼  (this is the Lead Agent — published ABI, do not change) │
│   ┌────────────────────────────────────────────────────────────┐ │
│   │ LEAD AGENT (assemble_lead_agent → LeadAgentAssembly)       │ │
│   │                                                            │ │
│   │  • 全套 middleware: memory, summarization, sandbox,        │ │
│   │    clarification, loop_detection, todo, title, view_image, │ │
│   │    terminal_response, subagent_limit, token_usage, ...     │ │
│   │  • 工具: sandbox + built-in + MCP + community + subagent   │ │
│   │  • System prompt: apply_prompt_template (含 subagent 段)   │ │
│   │                                                            │ │
│   │  ┌──────────────────┐    ┌──────────────────┐              │ │
│   │  │ task_tool        │    │ batch_task_tool  │              │ │
│   │  │ (普通委派)       │    │ (持久化批量)     │              │ │
│   │  └────────┬─────────┘    └────────┬─────────┘              │ │
│   └───────────┼──────────────────────┼─────────────────────────┘ │
│               │                      │                            │
│               ▼                      ▼                            │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │ SubagentExecutor (one per delegate call)                │    │
│   │  • capacity.slot() admission                            │    │
│   │  • 提交到 persistent isolated event loop                │    │
│   │  • 编译 child graph (checkpointer=False, one-shot)      │    │
│   │  • 继承 parent checkpoint namespace, NOT thread_id       │    │
│   │  • 复制 sandbox lease / uploaded_files 跨 loop 边界      │    │
│   └─────────────────────────┬───────────────────────────────┘    │
│                             │                                     │
│               ┌─────────────┴─────────────┐                       │
│               ▼                           ▼                       │
│   ┌────────────────────┐      ┌────────────────────────┐         │
│   │ general-purpose    │      │ bash                   │         │
│   │ SubagentConfig     │      │ SubagentConfig         │         │
│   │ tools=None(inherit)│      │ tools=[bash,ls,read..] │         │
│   │ disallowed=[task,  │      │ disallowed=[task,      │         │
│   │   ask_clarification│      │   ask_clarification,   │         │
│   │   , present_files] │      │   present_files]       │         │
│   │ max_turns=150      │      │ max_turns=60           │         │
│   │ model="inherit"    │      │ model="inherit"        │         │
│   └────────────────────┘      └────────────────────────┘         │
│                                                                    │
│   ★ 还有 user-defined: custom_agents (config.yaml)                │
│                      + managed agents (Settings UI / DB)          │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Lead Agent — 顶级入口

**文件:** `backend/packages/harness/deerflow/agents/lead_agent/agent.py`

### 3.1 双重工厂签名 (published ABI)

```python
# 在 agents/__init__.py 模块级 — LangGraph Server 直接解析
def make_lead_agent(config: RunnableConfig) -> Any:
    prime_enabled_skills_cache()
    return factory(config)
```

- **`make_lead_agent(config)`** — LangGraph Server 调用的薄包装,返回 `.graph`。
  这是 **published ABI**,签名/返回类型不能改。
- **`assemble_lead_agent(config, *, app_config=None) -> LeadAgentAssembly`**
  — Gateway 用的富入口,返回 `(graph, descriptor)`,descriptor 由
  `agents/assembly_descriptor.py::build_assembly_descriptor()` 构建,
  携带:解析后的 model、渲染过的 prompt hash、authorization 之后的 tool 列表、
  按顺序排列的 middleware stack。

```python
@dataclass(frozen=True)
class LeadAgentAssembly:
    graph: Any
    descriptor: Any

def unwrap_agent_graph(agent_result: Any) -> Any:
    """Unwrap a lead assembly, leaving any other factory result untouched."""
    return agent_result.graph if isinstance(agent_result, LeadAgentAssembly) else agent_result
```

> 第三方或测试 factory 可能仍返回 bare graph,Gateway 必须用 `unwrap_agent_graph`
> 防御性解包。模块顶部 docstring 还写了一条 invariant:
> **tracing callback 必须在 graph invocation root 注入**,
> 否则会重复 span 且 Langfuse 的 `propagate_attributes` 不会触发,
> `session_id`/`user_id` 不会到达 trace。

### 3.2 Lead Agent 的关键中间件 (按 `_make_lead_agent` 装配顺序)

来源: [agents/lead_agent/agent.py:37-51](../packages/harness/deerflow/agents/lead_agent/agent.py#L37-L51) 的 import 列表 + `build_lead_runtime_middlewares`。

| 中间件 | 文件 | 作用 |
|---|---|---|
| `MemoryMiddleware` | `middlewares/memory_middleware.py` | 写前/写后触发 DeerMem 提取,`memory_enabled=False` 时整个链路 disable |
| `ClarificationMiddleware` | `middlewares/clarification_middleware.py` | 拦截 `ask_clarification`,non-interactive 上下文(例如定时任务)禁用 |
| `LoopDetectionMiddleware` | `middlewares/loop_detection_middleware.py` | 重复 tool-call 集合 → `loop_capped` stop_reason |
| `ModelLengthFinishReasonMiddleware` | `middlewares/...` | 把 `finish_reason=length` 映射到 `terminal_response` |
| `SafetyFinishReasonMiddleware` | 同上 | `finish_reason=safety` 处理 |
| `SubagentLimitMiddleware` | `middlewares/subagent_limit_middleware.py` | 强制 `max_concurrent_subagents` / `max_total_subagents` |
| `DeerFlowSummarizationMiddleware` | `middlewares/summarization_middleware.py` | 触发对话压缩 → `ThreadState.summary_text` |
| `TitleMiddleware` | `middlewares/title_middleware.py` | 自动生成 thread title,**调用 `create_chat_model(attach_tracing=False)`** |
| `TodoMiddleware` | `middlewares/todo_middleware.py` | `is_plan_mode=True` 时挂 todo 工具 |
| `TokenUsageMiddleware` | `middlewares/token_usage_middleware.py` | 累计 token 计数 |
| `TerminalResponseMiddleware` | `middlewares/terminal_response_middleware.py` | 控制 lead agent 何时向用户最终回复 |
| `ViewImageMiddleware` | `middlewares/view_image_middleware.py` | 处理 vision 模型请求中的图像 |
| `load_configured_extension_middlewares` | `middlewares/configured_extensions.py` | 第三方扩展注册的自定义中间件 |
| `build_lead_runtime_middlewares` | `middlewares/tool_error_handling_middleware.py` | 工具错误处理、AuditContext、LLM 错误转 AIMessage |

**关键 invariant** (`agent.py:1-23`): graph 装配后,**所有**调用 `create_chat_model` 的点
必须传 `attach_tracing=False`,否则会出现 duplicate span + Langfuse `propagate_attributes` 失效。
5 个当前 sites:bootstrap agent、default agent、summarization middleware、
`TitleMiddleware` 的 async 路径、skill security scanner。
**任何新的 in-graph `create_chat_model` 调用都必须加入这个列表。**

### 3.3 Lead Agent 的运行时配置 (`config.configurable`)

来源: [agents/AGENTS.md](../../packages/harness/deerflow/agents/AGENTS.md) + [lead_agent/agent.py:151-200](../packages/harness/deerflow/agents/lead_agent/agent.py#L151-L200)

| Key | 作用 |
|---|---|
| `thinking_enabled` | 是否启用 extended thinking |
| `model_name` | 选择具体 LLM |
| `is_plan_mode` | 启用 TodoList middleware |
| `subagent_enabled` | 是否暴露 `task` 工具 |
| `max_concurrent_subagents` | 单次响应内最大并发委派数 (1-64) |
| `max_total_subagents` | 单次 run 总委派上限 (1-50,默认 6) |
| `run_id` | **Gateway / `DeerFlowClient.stream()` 必填**;自定义 graph 必须提供;缺失则计为整 thread ledger(fail-restrictive) |

### 3.4 Prompt trust 边界

> framework authority 用 **system channel**;user/model-influenced text 用 sanitized `HumanMessage` data channel。
> 永远不要把不可信的值插值进 system text —— 即使转义 tag 也不能中和自然语言注入(PR #5090)。

Lead agent 渲染 prompt 时,把 subagent 段拼成一个完整的 `<subagent_system>...</subagent_system>` 块,
防止 `</subagent_system><system-reminder>...` 之类的标签逃逸([lead_agent/prompt.py:331](../packages/harness/deerflow/agents/lead_agent/prompt.py#L331))。

---

## 4. Agent (SDK factory) — `create_deerflow_agent`

**文件:** `backend/packages/harness/deerflow/agents/factory.py`

### 4.1 定位

不是用户能直接看到的"产品概念",而是 **SDK 层**:位于原始 LangChain
`langchain.agents.create_agent` 和 config-driven `make_lead_agent` 之间。
**纯参数** — 不读 YAML,不安装 process-global 依赖。

```python
def create_deerflow_agent(
    model: BaseChatModel,
    tools: list[BaseTool] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    features: RuntimeFeatures | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
    plan_mode: bool = False,
    state_schema: type | None = None,
    checkpoint_channel_mode: CheckpointChannelMode = "full",
    checkpoint_snapshot_frequency: int | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    name: str = "default",
    subagent_runtime: SubagentRuntime | None = None,
) -> CompiledStateGraph:
```

### 4.2 两种模式

| 模式 | 用法 | 限制 |
|---|---|---|
| **Full takeover** | 传 `middleware=[...]` | 不能和 `features` / `extra_middleware` 同时用 |
| **Declarative features** | 传 `features=RuntimeFeatures(...)` | 不能和 `middleware` 同时用 |

### 4.3 RuntimeFeatures ([agents/features.py:18-41](../packages/harness/deerflow/agents/features.py#L18-L41))

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True
    memory: bool | AgentMiddleware = False
    memory_config: MemoryConfig | None = None
    summarization: Literal[False] | AgentMiddleware = False
    subagent: bool | AgentMiddleware = False
    vision: bool | AgentMiddleware = False
    auto_title: bool | AgentMiddleware = False
    guardrail: Literal[False] | AgentMiddleware = False
    loop_detection: bool | AgentMiddleware = True
    token_budget: bool | AgentMiddleware = False
```

- `True` → 用 built-in default middleware
- `False` → disable
- `AgentMiddleware` 实例 → 用你自己的实现替换

### 4.4 与 Lead Agent 的关系

- Lead Agent 内部用 `create_deerflow_agent(...)` 装配子图,但入口是 `make_lead_agent`
- **不能**用 `create_deerflow_agent` 直接得到一个完整的 Lead Agent 体验,因为它不读 YAML、不装配 `assembly_descriptor`、不接 Gateway 的 HTTP/UI
- 它是 **第三方嵌入**和 **测试** 的入口,以及 **Custom Agent** 的内部实现基础
- `SubagentRuntime` 通过显式 DI 让多个 graph 共享一个执行容量控制器,而不读全局 YAML

### 4.5 `Next` / `Prev` — 中间件定位装饰器

```python
@Next(LoopDetectionMiddleware)
class MyMiddleware: ...    # 会被插入到 LoopDetectionMiddleware 之后

@Prev(SubagentLimitMiddleware)
class MyMiddleware: ...    # 会被插入到 SubagentLimitMiddleware 之前
```

只用于 `features=...` 路径 — middleware 装饰器不能跨过显式 `middleware=[...]` 的 takeover。

---

## 5. Subagent — 受限的代理工人

**文件:**
- `backend/packages/harness/deerflow/subagents/`(包根,运行时入口)
- `subagents/registry.py`(名字解析和 overrides)
- `subagents/builtins/`(built-in 定义)
- `subagents/executor.py`(执行引擎,核心)
- `subagents/runtime.py`(显式 SDK runtime)
- `subagents/config.py`(`SubagentConfig` 数据类)

### 5.1 数据形态

```python
# subagents/config.py
@dataclass
class SubagentConfig:
    name: str
    description: str                                # 何时委派给这个 subagent
    system_prompt: str | None = None
    tools: list[str] | None = None                  # None = 继承父全部
    disallowed_tools: list[str] = ["task"]          # 默认禁止 task
    skills: list[str] | None = None                 # None = 全部启用,[] = 全部禁用
    model: str = "inherit"                          # "inherit" = 用 parent model
    max_turns: int = 50
    timeout_seconds: int = 900                      # 15 min 默认
```

### 5.2 名字解析顺序 (subagents/registry.py:106-176)

1. **`BUILTIN_SUBAGENTS`** — built-in (`general-purpose`, `bash`)
2. **`config.yaml -> subagents.custom_agents[name]`** — 静态自定义
3. **Administrator-managed**(同 `agent_storage.backend` 持久化,通过 Settings UI 维护)
4. **`subagents.agents[name]` overrides** — 每个 subagent 单独的 `timeout_seconds` / `max_turns` / `model` / `skills`

Built-in 优先级最高;managed 与 built-in 同名时**只持久化、不出现在运行时**。
`host_bash_allowed=False` 时 `bash` 也不出现在运行时。

### 5.3 Built-in subagents

#### general-purpose ([subagents/builtins/general_purpose.py](../packages/harness/deerflow/subagents/builtins/general_purpose.py))

- **何时用**:bounded exploration + clear delegation benefit、独立的非重叠并行工作、bounded context-heavy 调查
- **不要**仅仅因为复杂或多步就委派;bounded dependent chain 在 specialist/context-isolation 明显赢的时候仍可委派
- `tools=None`(继承父) · `disallowed_tools=["task", "ask_clarification", "present_files"]`
- `model="inherit"` · `max_turns=150`(默认) · `timeout_seconds` 全局默认 1800
- system_prompt 强制声明:`task` 工具 **不可用**,不要尝试 spawn 子 subagent;并行需求请用 bash 后台或顺序执行

#### bash ([subagents/builtins/bash_agent.py](../packages/harness/deerflow/subagents/builtins/bash_agent.py))

- **何时用**:多命令 workflow 的日志/中间态会 displacing lead context 时;独立非重叠 shell workload;保持顺序命令链在一个隔离上下文里
- **routine git/build/test/deploy 不是委派的理由** — 直接用 bash 工具
- `tools=["bash", "ls", "read_file", "write_file", "str_replace"]`(仅沙箱工具)
- `disallowed_tools=["task", "ask_clarification", "present_files"]`
- `model="inherit"` · `max_turns=60`

### 5.4 三个 subagent 执行入口

| 工具 | 文件 | 模式 |
|---|---|---|
| `task` | `tools/builtins/task_tool.py:651` | 普通委派,一次返回 |
| `batch_task` | `tools/builtins/batch_task_tool.py:28` | 持久化批量,lease 恢复 |
| (内部 `execute_async`) | `subagents/executor.py:1831` | 后台 fire-and-forget,极少见 |

`task()` 是绝大多数用例。`batch_task()` 只在 explicit 工具选择时启用,不从 prompt 大小推断。

---

## 6. 执行流程 (`task()` → `SubagentExecutor` → 结果)

来源:[subagents/AGENTS.md](../../packages/harness/deerflow/subagents/AGENTS.md) + `subagents/executor.py:771-1920`。

### 6.1 端到端

```
Lead Agent 调 task(subagent_type="general-purpose", prompt=..., acceptance_criteria=[...])
       │
       ▼
task_tool.py::task_tool coroutine
       │
       ├─ 解析 subagent_type → SubagentConfig(resolve_subagent_model_name)
       ├─ 构造 SubagentExecutor(config, tools, ..., 
       │      sandbox_state, thread_data, uploaded_files(snapshot),
       │      trace_id, user_id, run_id, ..., context_snapshot)
       │
       ├─ executor.execute_async(task, task_id=provider_tool_call_id)
       │       │   (provider tool_call_id 变 external_task_id,绝不进 registry key)
       │       │
       │       ├─ 生成 server-side execution_id = uuid.uuid4()  ← 这才是 task_id
       │       ├─ 注册到 _background_tasks[execution_id] = SubagentResult(PENDING)
       │       ├─ parent_context = _copy_isolated_subagent_context()
       │       │     (filter 掉 deerflow_loop_bound handlers;copy callback manager)
       │       └─ _submit_to_isolated_loop_in_context(parent_context, run_with_timeout)
       │                │
       │                ▼
       │       ┌────────────────── persistent isolated event loop ─────────────────┐
       │       │  await self._aexecute(task, result)                              │
       │       │                                                                 │
       │       │    1. capacity.slot() admission (process-global FIFO)           │
       │       │    2. status → RUNNING, started_at = now(UTC)                   │
       │       │    3. await self._aexecute_admitted(task, result)               │
       │       │       ├─ _build_initial_state(task) 复制父 sandbox lease,       │
       │       │       │   注入 context_snapshot, 拼 acceptance_criteria         │
       │       │       ├─ _create_agent(features=...) → child CompiledStateGraph │
       │       │       │    (checkpointer=False, 不传 thread_id,继承父 ContextVar)│
       │       │       ├─ graph.astream(values, stream_mode="values")            │
       │       │       │    + capture_new_step_messages 收集 AIMessage/ToolMessage│
       │       │       │    + SubagentTokenCollector 累计 token                  │
       │       │       │    + _harvest_tool_receipts / _harvest_bash_executions   │
       │       │       │    + 持久化 subagent.start / .step / .end 事件             │
       │       │       ├─ 收尾时 _consume_guard_stop_reason() 取 stop_reason       │
       │       │       │    (token_capped | loop_capped | turn_capped | None)    │
       │       │       └─ try_set_terminal(status, stop_reason, receipts, ...)    │
       │       └──────────────────────────────────────────────────────────────────┘
       │
       ├─ poll _peek_subagent_result(execution_id) 直到 terminal
       └─ format_subagent_result_message(result) + ToolMessage(additional_kwargs={
            subagent_status, subagent_stop_reason, subagent_token_usage,
            subagent_model_name, subagent_acceptance_verdict, ...})
```

### 6.2 两个 ID,两个职责 — 不要混

| ID | 来源 | 用途 |
|---|---|---|
| `external_task_id` | provider `tool_call_id`(传 `task()` 的) | 关联 `ToolMessage`、`task_*` SSE 事件、持久化 lifecycle、frontend 卡片、`ExtensionData.scope_id` |
| `task_id` (server-side) | `str(uuid.uuid4())` | `SubagentExecutor.execute_async` 的返回,**process-wide registry key**,轮询/取消/超时清理的归属 |

Provider ID 在并发 parent run 间**不唯一**,绝不能拿它当 registry ownership key。
Scheduler closure 必须保留自己的 `SubagentResult`,**不要**通过 mutable registry 重解 ownership。

### 6.3 并发与总额度

来源:[subagents/AGENTS.md §Concurrency](../../packages/harness/deerflow/subagents/AGENTS.md) + [agents/AGENTS.md §Runtime Configuration](../../packages/harness/deerflow/agents/AGENTS.md)。

**`MAX_CONCURRENT_SUBAGENTS = 3`**(`executor.py:1922` 的常量,作为 process-global fallback)
— 单次响应内的最大并发。Schema clamp 1-64。**Hot reload 不能让两个层 advertise 的 capacity 超过已创建的 process controller**;启动期冻结的值必须重启后生效。

`max_total_per_run`(默认 6, schema 1-50)— 整个 run 的总委派上限,由 `SubagentLimitMiddleware` 强制。
**历史 run 不计入新 run 的预算**。
Gateway / `DeerFlowClient.stream()` 必须提供 `run_id`,缺失则 fail-restrictive 计为整 thread ledger。

预算用尽时:task call 被 strip、provider raw tool-call metadata 同步、
`finish_reason="stop"`、在消息里附加可见的"subagent delegation limit" 注释,
让 lead 能基于已收集结果合成。

### 6.4 State 隔离 (跨 loop 边界)

- **`checkpointer=False`** — subagent 是 one-shot,绝不可 resume
- **故意不传** `thread_id` / `checkpoint_ns` / `checkpoint_id` / `checkpoint_map` 到 child `RunnableConfig`
  - LangGraph 1.2.6+ 上,**显式重传**即使用同一个 parent thread_id,也会开新 root lineage,把 child AI/tool frame 路由到 parent `messages` stream。Regression coverage 在 `tests/test_subagent_executor.py::TestSubagentCheckpointLineage`。
  - DeerFlow 业务代码仍通过 `runtime.context` 拿 parent `thread_id`(sandbox、middleware、attribution 的首选 lookup 路径)
- **`uploaded_files`** deep-copy 跨 isolated loop 边界(seeded into child state);`batch_task` 故意保留 disabled(没有 valid parent-run upload boundary)
- **`sandbox_lease_owner_id` + `sandbox_command_scope_id`** — 同一 lead thread 的所有 child 共享 sandbox provider client;最后一个 child 释放 provider。AIO 上,command scope 给每个 subagent 一个独立 persistent shell session,scope 内顺序保留
- **`_copy_isolated_subagent_context()`** — copy `ContextVars` 后**移除** `deerflow_loop_bound` handlers(`RunJournal` 标记);callback manager/list 整体保留。绝不能换成空白 `Context`,否则继承的 checkpoint namespace 和 framework stream callback 会断掉

### 6.5 异步反向路径 (deferred cleanup / final usage)

- Registry cleanup **pin 到 persistent subagent loop**(通过 `run_on_isolated_subagent_loop()`),不在 caller loop 上 fire `asyncio.create_task` — `asyncio.run()` 退出时 cancel 一切,caller-loop task 会先死
- 最终 usage report 走**另一向**:`_schedule_deferred_subagent_cleanup` 在 unwind 时**捕获 parent run 的 loop**,`_deliver_final_usage_report` 用 `call_soon_threadsafe` 回到那个 loop
- **`record_external_llm_usage_records` 永远不要在 persistent loop 或 `to_thread` worker 里调** — journal 的 accumulators 是 unlocked read-modify-write 字段,cross-thread write 会静默丢 token
- 捕获 loop 已经关闭时(`asyncio.run` teardown),report 主动 drop 并 info 日志 — 那条 run 已经持久化 completion data,没人读 counters

### 6.6 Subagent 的中间件链 (与 Lead 不同)

来源:[subagents/AGENTS.md §Context compaction](../../packages/harness/deerflow/subagents/AGENTS.md)。

| 顺序 | middleware | 来源 |
|---|---|---|
| (model call) | `SubagentDateContextMiddleware` | 首次 model call 前注入 `<current_date>` 隐式 SystemMessage |
| | `SystemMessageCoalescingMiddleware` | 把多个 SystemMessage 合并成一个(严格 OpenAI-compatible provider 必需) |
| | (可选) `DeerFlowSummarizationMiddleware` | 与 lead **共享** `summarization.enabled` 开关,trigger/keep/model/prompt 一致 |
| | `DurableContextMiddleware` | 把 `ThreadState.summary_text` 投影成 guarded hidden human data |
| | `TokenBudgetMiddleware` | hard cap 默认 1,000,000(summarization on)/ 2,000,000(off),warn 0.7,stop 1.0;user 设的永远赢 |
| | `LoopDetectionMiddleware` | 重复 tool-call 集合 → `loop_capped` |
| | `McpRoutingMiddleware` + `DeferredToolFilterMiddleware` | MCP 延迟工具(若 `tool_search.enabled`) |
| | `SkillToolPolicyMiddleware` | 动态 SKILL 工具策略 |

**关键差异**(相对 lead):
- subagent 不挂 `TitleMiddleware` / `TodoMiddleware` / `MemoryMiddleware` flush hook / `TerminalResponseMiddleware`
- `memory_flush_hook` **必须 skip**(`skip_memory_flush=True`):lead hook 用 `thread_id` 做 key,而 subagent 共享 parent thread_id,不 skip 会污染 parent 的 durable memory
- summarization 在 subagent 链上的位置在 guard trio 之后(lead 在之前);良性,因为该 middleware 只实现 `before_model`,不会干扰 guard stop_reason channel
- **关闭 `summarization.enabled` 时** `SystemMessageCoalescingMiddleware` 仍需保留(无条件最后)

### 6.7 持久化 & 事件流

来源:[subagents/AGENTS.md §Step capture](../../packages/harness/deerflow/subagents/AGENTS.md)。

- **`subagents/step_events.py::capture_new_step_messages`** 走 `stream_mode="values"` 的 *新 append tail* 而非 `messages[-1]`,保证 multi-tool-call turn 的每个 ToolMessage 都拿到
- **`runtime/runs/worker.py::_SubagentEventBuffer`** 批量写 `RunEventStore`,category="subagent",`task_id` 在 metadata
- `FLUSH_THRESHOLD` 事件、terminal `subagent.end`、worker `finally` 触发 flush — `put()` 是 per-thread advisory lock per call,deep subagent (150 turns) 一秒能 emit 几百 step,必须批量
- `subagent_run_event` 拒绝 malformed chunk;running chunk 还要求 `message_index` 非负整数 + 真 message 对象
- `build_subagent_step` 限 `SUBAGENT_STEP_MAX_CHARS`(`text` 和每个 tool call 的 `args` 都截)
- `list_events(task_id=..., after_seq=...)` 支持单 subagent step 流式 backfill,不耗尽 run-wide `limit`
- **History 收缩**:`DeerFlowSummarizationMiddleware` 通过 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 改写 messages channel → `len(messages)` 收缩过 cursor → cursor reset 到新 tail,id/content dedup 防止重新发出 pre-compaction step

---

## 7. 三者的关系 — 一表对照

| 维度 | Lead Agent | Agent (SDK factory) | Subagent |
|---|---|---|---|
| **公开名字** | "lead agent" / "super agent" | (内部 SDK 概念) | "subagent" |
| **入口文件** | `agents/lead_agent/agent.py` | `agents/factory.py` | `subagents/executor.py` |
| **导出符号** | `make_lead_agent(config)` | `create_deerflow_agent(...)` | `SubagentExecutor(config, tools, ...)` |
| **谁是 caller** | LangGraph Server / Gateway / 用户 | Lead Agent 内部 / 第三方嵌入 / 测试 | Lead Agent 通过 `task` / `batch_task` |
| **配置源** | YAML (`config.yaml`) + runtime context | 纯 Python 参数 | `SubagentConfig`(从 YAML / DB / built-in 解析) |
| **工具源** | sandbox + built-in + MCP + community + subagent tools | 调用方传入 | 父 agent 工具集(过滤 `disallowed_tools`) |
| **system prompt** | `apply_prompt_template`(skills/memory/subagent sections) | 调用方传入字符串 | 解析后的 `SubagentConfig.system_prompt` + report_contract + acceptance_criteria system note |
| **Middleware stack** | 完整 14+ 件 | 视 `features` 或 `middleware` 而定 | 子集:date context / coalescer / summarization / durable context / token / loop / skill / mcp |
| **Checkpoint** | 共享 thread checkpointer | 调用方传入 | `checkpointer=False`(one-shot) |
| **执行模式** | LangGraph Server run lifecycle | `CompiledStateGraph` 对象 | persistent isolated event loop 上的 `_aexecute` |
| **生命周期** | 整个 user turn | 整个 graph run | 一次 `task()` call 的执行 |
| **能否 spawn 子 agent** | ✅ `task` / `batch_task` | 视 `subagent_runtime` 注入而定 | ❌ `disallowed_tools` 默认含 `task` |
| **能否调 `ask_clarification`** | ✅ | ✅ | ❌ `disallowed_tools` 默认含它 |
| **能否调 `present_files`** | ✅ | ✅ | ❌ `disallowed_tools` 默认含它 |
| **能否直接 user interactive** | ✅ | ✅ | ❌ non-interactive only(无 `ask_clarification`) |

---

## 8. 配置面 — 三者从哪儿来

```
┌──────────────────┐     ┌──────────────────────┐
│ config.yaml      │────▶│ AppConfig            │
│  ├ subagents:    │     │  ├ subagents_config  │──┐
│  │   ├ custom_*  │     │  ├ agents_config     │  │
│  │   ├ agents.*  │     │  ├ models_config     │  │
│  │   └ runtime   │     │  └ ...               │  │
└──────────────────┘     └──────────────────────┘  │
                                                   │
                          ┌────────────────────────┘
                          ▼
              ┌───────────────────────────────┐
              │ make_lead_agent(config)       │
              │   ├ load YAML → AppConfig     │
              │   ├ build chat model          │
              │   ├ get tools (sandbox+...)   │
              │   ├ assemble middleware stack │
              │   ├ apply_prompt_template     │
              │   └ create_deerflow_agent(...)│
              └───────────┬───────────────────┘
                          ▼
              ┌───────────────────────────────┐
              │ Lead Agent CompiledGraph      │
              │   + LeadAgentAssembly.descriptor
              └───────────┬───────────────────┘
                          │ task(subagent_type="...")
                          ▼
              ┌───────────────────────────────┐
              │ SubagentExecutor.__init__     │
              │   ├ get_subagent_config(name) │
              │   │   ├ BUILTIN_SUBAGENTS     │
              │   │   ├ custom_agents[YAML]   │
              │   │   ├ managed (DB)          │
              │   │   └ agents.* overrides   │
              │   ├ resolve_subagent_model    │
              │   └ deepcopy uploaded_files   │
              └───────────────────────────────┘
```

**`get_subagent_config` 的优先级**(registry.py:106-176,严格按此顺序):

1. `BUILTIN_SUBAGENTS` — built-in 优先级最高
2. `_build_custom_subagent_config(name)` — `config.yaml::subagents.custom_agents[name]`
3. `_build_managed_subagent_config(name)` — `agent_storage.backend` 里的 managed definitions(启用后)
4. Per-agent overrides `subagents.agents[name]`(只 override `timeout_seconds` / `max_turns` / `model` / `skills`)

**Managed definition 同名 built-in / config.yaml 时**:仅持久化用于 Settings UI,运行时排除。

---

## 9. 三个 guard rail + additive `stop_reason`

来源:[subagents/AGENTS.md §Guardrail caps](../../packages/harness/deerflow/subagents/AGENTS.md)。

| 轴 | 谁触发 | 实现 | cap 行为 |
|---|---|---|---|
| **turn** | `max_turns` × LangGraph `recursion_limit` | `turn_budget.py::resolve_recursion_limit` 按 middleware chain 深度缩放 | `GraphRecursionError` → executor 单独 catch |
| **token** | `TokenBudgetMiddleware` (per-agent 通过 `build_subagent_runtime_middlewares`) | 默认 1,000,000(summarization on)/ 2,000,000(off),warn 0.7,hard 1.0 | 不抛 — strip in-flight tool calls,force `finish_reason="stop"`,让 run 收尾 |
| **loop** | `LoopDetectionMiddleware`(同位置) | 重复 tool-call 集合或一个 tool type 多次 | 不抛 — strip `tool_calls`,force final answer,记 `loop_capped` |

每个 guard middleware 暴露 `consume_stop_reason(run_id)` duck-typed accessor。
`_aexecute` 收集所有具备该方法的 middleware,取**首个非 None** reason。
**为什么是 additive field 而不是新 status enum**:
新 status 值会破坏 v1 consumer;可选字段被旧 frontend / ledger reader 忽略,保证 `contracts/subagent_status_contract.json` v2 + `frontend/.../subtask-result.ts` 跨语言向后兼容。

`SubagentResult.stop_reason` 通过 `task_tool.py::_task_result_command` → `format_subagent_result_message`
渲染成 `Task Succeeded (capped: ...)` / `Task failed (capped: ...)`,并写到 `make_subagent_additional_kwargs`
的 `subagent_stop_reason` key。

---

## 10. Acceptance criteria (RFC #4651)

Lead-supplied 的可判定标准,经过三层验证:

| 层 | 文件 | 做什么 |
|---|---|---|
| **Prompt** | `subagents/report_contract.py` | 在子 subagent SystemMessage 追加 `build_report_contract_section(receipts_enabled=...)`,要求 `[rN tool_name]` 引用(来自 tool receipts ledger)、可验证 handle、显式失败报告 |
| **Code (Layer 2)** | `subagents/acceptance_checks.py` | `check_acceptance_criteria` 在 `task` 完成分支判定 leaf:`file:<path> exists|non-empty` / `file_written:<path>` / `tests_passed:<command>` |
| **Judge (Layer 3)** | RFC §6 重新执行(待 PR5) | 模型判断声明正确性 |

**Neutralization**:criterion 文本先用 `neutralize_untrusted_tags` 转义再存储/渲染;
blocked tag 永远到不了 lead-visible checklist。

**Prompt-channel 边界**:criterion 只追加到 task `HumanMessage`(untrusted channel),
由 `InputSanitizationMiddleware` 转义并 boundary-frame;
**subagent 的 SystemMessage 只放框架拥有的 `build_acceptance_criteria_system_note(...)` 指针**,
让自然语言注入无法拿到 system-channel priority。

---

## 11. SubagentRuntime — 显式 SDK runtime

**文件:** `subagents/runtime.py`(direct `create_deerflow_agent` 的 DI 路径)

`create_deerflow_agent(..., subagent_runtime=runtime)` 是 direct graph caller 的显式依赖注入路径。
**复用同一个 `deerflow.subagents.SubagentRuntime`** 跨多个属于同一 application capacity boundary 的 graph。
默认 subagent feature 会绑定:
- middleware concurrency / total limits
- 普通 `task` 工具
- **一个**真实 execution controller
- 任何 active 的 durable-batch submitter

caller-owned batch repository 需要:
- `await runtime.start()` 或 `async with runtime` **在 graph 构造前**
- shutdown 时 `stop()`
- factory 在 worker stopped 时 **fail closed**;已构建的 bound batch tools 在 stopped 后必须 **fail unavailable**,绝不回退到另一个 process-global submitter

**factory 永远不**:
- 创建 SQL infrastructure
- 渲染 caller-owned 的 `system_prompt`
- 挂载 Gateway API / UI routes

**Full middleware takeover 不能和这个 runtime 同时用**;direct caller 和自定义 subagent middleware 仍要负责 model-visible call-policy wording。

---

## 12. 一些容易踩坑的点

1. **`task` provider `tool_call_id` ≠ subagent registry key** — 跨并发 parent run 不唯一,绝不能拿它当 ownership
2. **不要把 trust 字段放进 system text** — 转义 tag 不能中和自然语言注入;user/model 数据走 sanitized HumanMessage
3. **`max_concurrent` 启动后不能热改** — 改了必须重启,否则 middleware 会 advertise 一个不存在的 capacity
4. **child `RunnableConfig` 不能显式重传 `thread_id`** — LangGraph 1.2.6+ 会开新 root lineage,把 child frame 路由到 parent messages stream
5. **`RunJournal` 不能跨 loop** — 必须用 `_ParentLoopMiddlewareRecorderProxy` 代理到 parent loop,或在 subagent loop 上 fire 任何 journal 调用都会 `Future attached to a different loop`
6. **`record_external_llm_usage_records` 永远不要在 persistent loop 或 `to_thread` worker** — cross-thread write 会静默丢 token
7. **`is_internal` 必须无条件写(包括 False)** — subagent 的 `GuardrailMiddleware` 才能拿到和 lead 同样的 provenance
8. **`extensions` snapshot 在 `__init__` 时绑定**,不在 `_aexecute` 时读 singleton — 否则并发 `set_loaded_extensions()` 会换掉 generation
9. **subagent summarization 共享 lead 开关** — 改 `summarization.enabled` 影响两条链;trigger/keep/model/prompt 必须来自 shared config
10. **`subagent.end` 事件 flush 触发条件**:`subagent.end` 到达 / `FLUSH_THRESHOLD` events / worker `finally`;缺一不可,否则 deep run 会丢步

---

## 13. 去哪里继续读

| 你想知道的 | 读哪里 |
|---|---|
| Lead 装配细节 | [agents/AGENTS.md](../../packages/harness/deerflow/agents/AGENTS.md) + [agents/lead_agent/agent.py](../../packages/harness/deerflow/agents/lead_agent/agent.py) |
| Subagent 执行细节 | [subagents/AGENTS.md](../../packages/harness/deerflow/subagents/AGENTS.md) + [subagents/executor.py](../../packages/harness/deerflow/subagents/executor.py) |
| Subagent 任务工具 | [tools/builtins/task_tool.py](../../packages/harness/deerflow/tools/builtins/task_tool.py) |
| Subagent 持久化批量 | [tools/builtins/batch_task_tool.py](../../packages/harness/deerflow/tools/builtins/batch_task_tool.py) + [subagents/batch_service.py](../../packages/harness/deerflow/subagents/batch_service.py) |
| 配置 schema | [backend/docs/CONFIGURATION.md](../CONFIGURATION.md) |
| 整体架构 | [backend/docs/ARCHITECTURE.md](../ARCHITECTURE.md) |
| Prompt 委派策略 | [tests/test_subagent_routing_prompt.py](../../../tests/test_subagent_routing_prompt.py) + [tests/test_lead_agent_prompt.py](../../../tests/test_lead_agent_prompt.py) |
| Checkpoint lineage 回归 | [tests/test_subagent_executor.py::TestSubagentCheckpointLineage](../../../tests/test_subagent_executor.py) |
| Status contract 兼容 | [contracts/subagent_status_contract.json](../../../contracts/subagent_status_contract.json) v2 + [subagents/status_contract.py](../../packages/harness/deerflow/subagents/status_contract.py) |

---

## 14. 关键文件索引 (按出现顺序)

| 角色 | 路径 |
|---|---|
| Lead Agent 工厂 | [backend/packages/harness/deerflow/agents/lead_agent/agent.py](../../packages/harness/deerflow/agents/lead_agent/agent.py) |
| Lead Agent 提示词 | [backend/packages/harness/deerflow/agents/lead_agent/prompt.py](../../packages/harness/deerflow/agents/lead_agent/prompt.py) |
| SDK Agent 工厂 | [backend/packages/harness/deerflow/agents/factory.py](../../packages/harness/deerflow/agents/factory.py) |
| RuntimeFeatures | [backend/packages/harness/deerflow/agents/features.py](../../packages/harness/deerflow/agents/features.py) |
| Assembly descriptor | [backend/packages/harness/deerflow/agents/assembly_descriptor.py](../../packages/harness/deerflow/agents/assembly_descriptor.py) |
| ThreadState schema | [backend/packages/harness/deerflow/agents/thread_state.py](../../packages/harness/deerflow/agents/thread_state.py) |
| Middleware(lead 装配位置) | [backend/packages/harness/deerflow/agents/middlewares/](../../packages/harness/deerflow/agents/middlewares/) |
| Subagent 数据类 | [backend/packages/harness/deerflow/subagents/config.py](../../packages/harness/deerflow/subagents/config.py) |
| Subagent 注册表 | [backend/packages/harness/deerflow/subagents/registry.py](../../packages/harness/deerflow/subagents/registry.py) |
| Built-in subagents | [backend/packages/harness/deerflow/subagents/builtins/](../../packages/harness/deerflow/subagents/builtins/) |
| Subagent 执行引擎 | [backend/packages/harness/deerflow/subagents/executor.py](../../packages/harness/deerflow/subagents/executor.py) |
| SubagentRuntime | [backend/packages/harness/deerflow/subagents/runtime.py](../../packages/harness/deerflow/subagents/runtime.py) |
| Subagent AGENTS.md | [backend/packages/harness/deerflow/subagents/AGENTS.md](../../packages/harness/deerflow/subagents/AGENTS.md) |
| Task 工具(普通委派) | [backend/packages/harness/deerflow/tools/builtins/task_tool.py](../../packages/harness/deerflow/tools/builtins/task_tool.py) |
| Batch task 工具(持久化批量) | [backend/packages/harness/deerflow/tools/builtins/batch_task_tool.py](../../packages/harness/deerflow/tools/builtins/batch_task_tool.py) |
| Acceptance 报告契约 | [backend/packages/harness/deerflow/subagents/report_contract.py](../../packages/harness/deerflow/subagents/report_contract.py) |
| Acceptance 自动检查 | [backend/packages/harness/deerflow/subagents/acceptance_checks.py](../../packages/harness/deerflow/subagents/acceptance_checks.py) |
| Step events | [backend/packages/harness/deerflow/subagents/step_events.py](../../packages/harness/deerflow/subagents/step_events.py) |
| Status contract | [backend/packages/harness/deerflow/subagents/status_contract.py](../../packages/harness/deerflow/subagents/status_contract.py) |
| Context snapshot | [backend/packages/harness/deerflow/subagents/context_snapshot.py](../../packages/harness/deerflow/subagents/context_snapshot.py) |
| Token collector | [backend/packages/harness/deerflow/subagents/token_collector.py](../../packages/harness/deerflow/subagents/token_collector.py) |
| Turn budget | [backend/packages/harness/deerflow/subagents/turn_budget.py](../../packages/harness/deerflow/subagents/turn_budget.py) |
| Capacity | [backend/packages/harness/deerflow/subagents/capacity.py](../../packages/harness/deerflow/subagents/capacity.py) |
| Batch runtime | [backend/packages/harness/deerflow/subagents/batch_runtime.py](../../packages/harness/deerflow/subagents/batch_runtime.py) |
| Batch service | [backend/packages/harness/deerflow/subagents/batch_service.py](../../packages/harness/deerflow/subagents/batch_service.py) |
| Batch acceptance | [backend/packages/harness/deerflow/subagents/batch_acceptance.py](../../packages/harness/deerflow/subagents/batch_acceptance.py) |
| Status contract JSON | [contracts/subagent_status_contract.json](../../../contracts/subagent_status_contract.json) |
| Agents module AGENTS.md | [backend/packages/harness/deerflow/agents/AGENTS.md](../../packages/harness/deerflow/agents/AGENTS.md) |
| Backend module AGENTS.md | [backend/AGENTS.md](../../AGENTS.md) |
| Repository AGENTS.md | [AGENTS.md](../../../AGENTS.md) |
