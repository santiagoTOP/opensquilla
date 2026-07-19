# 第 2 章：Turn Loop —— 一次 Agent 运行如何完成

> **本章目标**：从源码调用顺序出发，把一个 turn 从进来到出去经历的全部环节讲透——输入规范化、provider/tool 解析、11 步 pre-turn pipeline、Agent bootstrap、历史压缩、附件、流式 provider↔tool 循环、工具并发、预算门控、重试恢复、收尾落地。
>
> 本章是全书最硬的一章，因为 `Agent._turn_generator` 是一个约 **4600 行**（`agent.py:3758-8373`）的巨型 generator。按第一章约定，我把它切成 **6 个逻辑阶段**，每段**完整贴出不省略任何分支**，紧跟逐段注解。本章里的"11 步"指 pre-turn pipeline，"8 个 stage"指 TurnRunner 的 stage 组件——它们不是同一层，也不是 11+8 个串行函数。

---

## 2.1 先看真实的执行骨架

把 `_run_turn()` 的真实调用顺序钉死，后面所有小节都按这个骨架展开：

```text
TurnRunner.run()                          runtime.py:2697
  1. canonicalize session_key / normalize agent_id
  2. 构造带本 turn 信息的 effective_tool_context（派生副本）
  3. 获取 session lock，进入 _run_turn()
  4. InputStage                           runtime.py:2921
  5. ProviderAndToolsStage                runtime.py:2938
  6. PromptAssemblerStage                 runtime.py:2979
       └─ 创建 pipeline.TurnContext
       └─ 执行 11 步 pre-turn pipeline      runtime.py:5186
       └─ 把 turn.model 应用到 cloned selector  (apply_model_override, selector_override.py:178)
  7. AgentBootstrapStage                  runtime.py:3082
  8. CompactionAndHistoryStage            runtime.py:3150
  9. AttachmentStage                      runtime.py:3178
 10. StreamConsumerStage                  runtime.py:3231
       └─ Agent.run_turn()
           └─ Agent._turn_generator()     agent.py:3758-8373
               └─ provider stream ↔ tool loop
 11. TurnFinalizerStage                   runtime.py:3292
 12. 释放锁 / 清理本 turn compaction 状态
```

源码锚点：
- `TurnRunner.run()`：`src/opensquilla/engine/runtime.py:2697`
- `_run_turn()`：`src/opensquilla/engine/runtime.py:2844-3578`
- 11 步 pipeline 列表：`src/opensquilla/engine/runtime.py:5186-5201`
- Agent 主循环：`src/opensquilla/engine/agent.py:3758-8373`

### 为什么旧文档会出现"阶段顺序不一致"

stage 文件的职责命名和 `_run_turn()` 的历史 docstring（`runtime.py:2730-2740`，第 1 章 1.3.2 已贴）并非完全同步。当前执行顺序里，`ProviderAndToolsStage` 和 `PromptAssemblerStage` **先于** `CompactionAndHistoryStage`，而附件在 Agent bootstrap 和 history/compaction **之后**处理。**不要按 stage 文件名的自然语言顺序推断运行顺序**；后文的表和时序以 `_run_turn()` 的调用顺序为准。

---

## 2.2 八个 stage：职责与数据契约

stage 的实现位于 `src/opensquilla/engine/turn_runner/`（目录下有 `input_stage.py`、`provider_and_tools_stage.py`、`prompt_assembler_stage.py`、`agent_bootstrap_stage.py`、`compaction_and_history_stage.py`、`attachment_stage.py`、`stream_consumer_stage.py`、`turn_finalizer_stage.py` 共 8 个 stage，外加 `context.py`、`harness.py`、`outcome.py` 三个支撑文件）。

每个 stage 用 **Input/Output dataclass** + **`StageOutcome`** 返回结果，harness 负责顺序、早停和状态回写。

### 2.2.1 `StageOutcome`：stage 的统一返回契约

源码：`src/opensquilla/engine/turn_runner/outcome.py:30-90`。关键字段：

```python
@dataclass(frozen=True)
class StageOutcome[StageOutputT]:
    # ...
    terminate: bool = False
```

`terminate: bool = False`（`:42`）是早停开关。`require_output()` 在 terminate 时会报错，`require_early_yield()` 在非 terminate 时会报错——这迫使调用方明确处理两种情况。

### 2.2.2 八个 stage 的职责总表

| stage | 执行位置 | 产生的关键数据 | 失败时的含义 |
|---|---|---|---|
| `InputStage` | `runtime.py:2921` | `runtime_message`、`semantic_input`、normalization metadata | 输入规范化或持久化失败；可能提前结束 |
| `ProviderAndToolsStage` | `runtime.py:2938` | provider、cloned selector、tool defs、tool handler、effective ToolContext | 无 provider 时生成 `ProviderResolutionError` 并结束 |
| `PromptAssemblerStage` | `runtime.py:2979` | pipeline turn、final prompt、resolved model、cache breakpoints、prompt report | prompt/selector/pipeline 装配失败 |
| `AgentBootstrapStage` | `runtime.py:3082` | Agent、AgentConfig、有效预算、model capabilities | 无法构建 Agent 或配置不合法 |
| `CompactionAndHistoryStage` | `runtime.py:3150` | compaction 状态、历史、request context prompt | 可能触发压缩、预检或上下文升级 |
| `AttachmentStage` | `runtime.py:3178` | `extra_messages`、`turn_input` | 物化或附件消息构建失败 |
| `StreamConsumerStage` | `runtime.py:3231` | text parts、segments、artifacts、error/done event | 消费 Agent 流、取消、恢复和 warning 转换 |
| `TurnFinalizerStage` | `runtime.py:3292` | final text、transcript、memory/cost/error side effects | 收尾副作用尽量隔离，避免持久化失败吞掉运行结果 |

### 2.2.3 `StageOutcome` 与早停的真实代码

`ProviderAndToolsStage` 可以返回 `terminate=True` 的 outcome。`_run_turn()` 里的处理（`runtime.py:2938-2967` 附近）是：

```python
        pt_outcome = await self._provider_and_tools_stage.run(...)
        if pt_outcome.terminate:
            provider_error_event = cast(
                ErrorEvent,
                pt_outcome.require_early_yield(),
            )
            log.error("turn_runner.no_provider", session_key=session_key)
            self._emit_turn_event("turn_error", ...)
            await self._persist_turn_error(session_key, provider_error_event)
            yield provider_error_event
            return
```

► **注解**：
- provider 缺失时，runtime 先写 trace，再持久化 error，再 yield error，最后 return。**顺序是有意保留的**，不能简单改成 raise——因为 trace 和持久化必须在 yield 之前完成，否则客户端收到 error 时日志还没落盘。
- 因此"八阶段总会全部执行"是**错误的**。其他阶段也可能因错误 outcome、取消或 replay 路径提前结束。

### 2.2.4 为什么 AgentBootstrap 在压缩前

`AgentConfig` 里有 context window、provider、model capabilities、timeout、compaction 相关配置。**先 bootstrap 才能确定当前模型的有效窗口和运行预算**，`CompactionAndHistoryStage` 才能决定压缩/升级策略。这个顺序是**数据依赖**，不是视觉上的编排偏好。

### 2.2.5 StreamConsumerStage 的特殊之处

其他 7 个 stage 都用 Input/Output dataclass + `StageOutcome` 返回；但 `StreamConsumerStage` **偏离了统一契约**——它是一个 `async def run(...) -> AsyncIterator[AgentEvent]`（`stream_consumer_stage.py:1054-1057`），因为它要**边消费边 yield**实时事件给客户端，不能用一次性返回值。注释 `:1007-1010` 明确标了这个偏离。这是全书"显式优于统一抽象"哲学的一个例子：当实时性和统一契约冲突时，OpenSquilla 选择暴露真实形态，而不是塞进一个假 Output。

---

## 2.3 Pipeline 层：11 步、可失败但不应污染后续状态

### 2.3.1 当前 11 步的真实顺序

实际列表在 `src/opensquilla/engine/runtime.py:5186-5201`：

| # | step | 输入/输出重点 |
|---:|---|---|
| 1 | `resolve_model` | 得到基础模型选择；为后续 provider resolution 提供起点 |
| 2 | `apply_vision_followup_gate` | 判断近期图片跟进是否需要视觉模型 |
| 3 | `apply_squilla_router` | 按图片、hold、ML/启发式和策略门控得到 tier/model |
| 4 | `observe_reasoning_hint` | 观察并整理推理相关提示 |
| 5 | `meta_resolution` | 解析 meta-skill/clarification 相关状态 |
| 6 | `enforce_coding_mode` | 根据编码场景补齐或约束运行提示 |
| 7 | `meta_command_launch` | 处理 meta-skill 命令启动信息 |
| 8 | `filter_skills` | 根据当前 turn 过滤可见技能，并记录 skill ids |
| 9 | `inject_subagent_grounding` | 为子 Agent 场景注入父级/任务 grounding |
| 10 | `inject_platform_hint` | 注入运行平台相关提示 |
| 11 | `apply_prompt_cache` | 处理 prompt cache breakpoints 和 cache metadata |

> **注意**：这里"11 步"是当前源码事实。某些旧文档仍写"10 步"，通常是遗漏了 `apply_prompt_cache`，**不是**另一个 pipeline 版本。第 3 章会逐行展开第 3 步 `apply_squilla_router`。

### 2.3.2 `run_pipeline()` 的完整实现 + fail-open 语义

源码：`src/opensquilla/engine/pipeline.py:45-114`（**完整贴出**，第 1 章已贴过一次，这里聚焦 fail-open 语义的逐行讲）

```python
async def run_pipeline(ctx: TurnContext, steps: list[TurnStep]) -> TurnContext:
    records: list[PipelineStepRecord] = ctx.metadata.setdefault("pipeline_steps", [])
    for step in steps:
        step_name = step.__name__
        snapshot_meta = dict(ctx.metadata)
        try:
            ctx = await step(ctx)
        except Exception as exc:
            log.warning("pipeline.step_failed", step=step_name, error=str(exc))
            ctx.metadata.clear()
            ctx.metadata.update(snapshot_meta)
            records = ctx.metadata.setdefault("pipeline_steps", records)
            records.append(
                PipelineStepRecord(
                    step_name=step_name,
                    applied=False,
                    routing_source="none",
                    fallback_reason=str(exc),
                )
            )
            continue

        applied = bool(ctx.metadata.get(f"{step_name}__applied", True))
        if step_name == "apply_squilla_router":
            routed_tier = ctx.metadata.get("routed_tier")
            routing_source = cast(RoutingSource, ctx.metadata.get("routing_source", "none"))
            confidence = ctx.metadata.get("routing_confidence")
            filtered_skill_ids = None
        elif step_name == "filter_skills":
            routed_tier = None
            routing_source = "none"
            confidence = None
            filtered_skill_ids = ctx.metadata.get("filtered_skill_ids")
        else:
            routed_tier = None
            routing_source = "none"
            confidence = None
            filtered_skill_ids = None

        records = ctx.metadata.setdefault("pipeline_steps", records)
        records.append(
            PipelineStepRecord(
                step_name=step_name,
                applied=applied,
                routed_tier=routed_tier,
                filtered_skill_ids=filtered_skill_ids,
                routing_source=routing_source,
                confidence=confidence,
                fallback_reason=None,
            )
        )
    return ctx
```

► **注解（逐段）**
1. `records = ctx.metadata.setdefault("pipeline_steps", [])`：pipeline record 和其他 metadata 共用同一个 dict，但 record list 由执行器统一拥有。
2. `step_name = step.__name__`：记录的是函数名。router wrapper（`_bounded_apply_squilla_router`）在 runtime 中会显式把 `__name__` 重设成 `"apply_squilla_router"`（`runtime.py:5059`），保证 record 不会出现内部 wrapper 名。
3. `snapshot_meta = dict(ctx.metadata)`：**顶层浅拷贝**。它保护 metadata 的键值集合，但**不复制嵌套 list/dict 的内部对象**。
4. `ctx = await step(ctx)`：step 可以原地修改 ctx，也可以返回新 ctx。执行器必须用返回值。
5. `except Exception`：异常被记录后**不传播**给外层 TurnRunner。当前 step 被视为没应用，后面的 step 继续。
6. `ctx.metadata.clear()` + `update(snapshot_meta)`：**只回滚 metadata**。它**不会**自动回滚 `ctx.model`、`ctx.attachments`、外部数据库写入或嵌套对象的原地修改。
7. `PipelineStepRecord(applied=False, ..., fallback_reason=str(exc))`：把失败原因写进 `fallback_reason`，便于 decision record 解释"为什么没应用"。
8. `applied = bool(ctx.metadata.get(f"{step_name}__applied", True))`：step 可以不抛异常但主动设置 `step_name__applied=False`，表示某个 gate 判断当前 turn 不适用。
9. router 特殊字段：**只有** `apply_squilla_router` 会把 `routed_tier`、`routing_source`、`confidence` 放进 record；普通 step 不会伪造路由字段。
10. `filter_skills` 特殊字段：只有技能过滤 step 填 `filtered_skill_ids`。
11. `return ctx`：即使某一步失败，返回的仍是可继续执行的 context。

### 2.3.3 fail-open 的精确边界（设计动机）

> **解释**：fail-open 的精确定义是"单个 step 异常 → 恢复 metadata 顶层快照 → 写失败记录 → 继续后续 step → pipeline 最终返回"。它**不是**"任何副作用都自动事务回滚"。

| 失败类型 | fail-open 是否保护 | 说明 |
|---|---|---|
| step 抛异常，只改了 metadata | ✅ 保护 | 顶层快照恢复 |
| step 抛异常，但已改了 `ctx.model` | ❌ 不保护 | model 不在快照范围 |
| step 抛异常，但已写外部 DB | ❌ 不保护 | 外部副作用无事务 |
| step 抛异常，嵌套 list 已原地改 | ❌ 不保护 | 浅拷贝不复制嵌套对象 |

这个边界对新增 step 很重要。新 step 如果要写外部存储，**必须自己设计幂等或提交点**，不能依赖 `run_pipeline` 的 `snapshot_meta`。官方路由 wrapper 通过对 turn 做**深拷贝**并延迟提交 routing history，额外降低了这类污染风险——下面讲。

### 2.3.4 路由 step 的时间隔离：`_bounded_apply_squilla_router`

`runtime.py:5037-5059` 定义了 `_bounded_apply_squilla_router`，它做三件事：

```text
原 pipeline.TurnContext
  -> 深拷贝 metadata、复制 tool_defs/attachments
  -> 标记 _defer_squilla_router_history
  -> 在单独 executor 中 asyncio.run(apply_squilla_router(...))
  -> wait_for(routing_timeout_seconds)
  -> 成功后 commit_deferred_router_history
```

默认路由超时来自 `SquillaRouterConfig.routing_timeout_seconds`，当前默认 5 秒。超时会让该 pipeline step 进入 fail-open；它**不会**把一个未完成的路由结果直接写回主 turn。这是路由污染防护的第二层（第一层是 `run_pipeline` 的 metadata 快照，第三层是 cloned selector）。

### 2.3.5 真实数据样例：pipeline step 失败的 record

假设第 4 步 `observe_reasoning_hint` 抛了 `KeyError`：

```json
// 失败后 ctx.metadata["pipeline_steps"] 里多一条
{
  "step_name": "observe_reasoning_hint",
  "applied": false,
  "routing_source": "none",
  "fallback_reason": "'reasoning_key'",
  "routed_tier": null,
  "confidence": null
}

// 而第 3 步 apply_squilla_router 的成功 record 仍保留
{
  "step_name": "apply_squilla_router",
  "applied": true,
  "routed_tier": "c2",
  "routing_source": "v4_phase3",
  "confidence": 0.71,
  "fallback_reason": null
}
```

关键：失败 step **不会抹掉**成功 step 的 record，也不会阻止后续 step 继续跑。

---

## 2.4 模型选择如何回到执行链

pipeline 的路由结果主要写入 `ctx.metadata` 的这些键（`ctx` 是 pipeline.TurnContext）：

```text
turn.model                       # 路由后的模型 id（写到 ctx.model 字段）
turn.metadata["routed_tier"]     # policy 后最终路由 tier
turn.metadata["routed_model"]    # tier 配置的模型
turn.metadata["routing_applied"] # 是否真的切换 baseline
turn.metadata["routing_source"]  # v4_phase3 / image_route / hold / default
turn.metadata["routing_confidence"]
turn.metadata["router_fallback_chain"]
```

`PromptAssemblerStage` 结束后，runtime 用 `apply_model_override()` 把 `turn.model` 应用到 cloned selector。注意 `apply_model_override` **定义在** `src/opensquilla/engine/selector_override.py:178`（不在 runtime.py），runtime 在 `:5210` 调用它。

```text
共享 provider selector
        │ clone
        ▼
本 turn cloned selector
        │ apply_model_override(turn.model, turn.metadata, ...)
        ▼
本 turn provider
```

> **设计动机**：这条边界极其重要。如果直接改共享 selector，两个并发 session 可能**互相覆盖模型**。`cross_provider_tiers`、tier provider mismatch、provider credentials 的处理也发生在这次局部 rebind 周围；路由本身**不等于** provider 已经成功解析。这是第 1 章 1.11"不变量 1"的具体落地。

---

## 2.5 Agent 循环：状态、事件和 iteration

### 2.5.1 状态定义（完整）

源码：`src/opensquilla/engine/types.py:42-48`（第 1 章已贴，这里聚焦状态转换）

```python
class AgentState(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    ERROR = "error"
    DONE = "done"
```

### 2.5.2 正常路径的状态转换

```text
IDLE
  -> THINKING            （进入 _turn_generator 时，agent.py:3774）
      -> STREAMING       （provider.chat() 流式响应，agent.py:4438）
          -> DONE        （没有 tool call，正常结束，agent.py:8347）
          -> TOOL_CALLING
              -> THINKING  （工具结果加入消息，进入下一次 provider 调用）
```

**注意三个易错点**：
1. `STREAMING` 是 provider 响应消费状态，**不代表**用户最终答案已经确定；provider 同一轮可以同时输出文本 delta 和 tool-use。只有确认没有工具调用并完成收尾，文本才是 turn 的 final answer。
2. 初始 `IDLE -> THINKING` 在 `_turn_generator()` 进入时发出（`:3774`）。每次新 provider iteration 前进入 `STREAMING`（`:4438`）；工具批次前进入 `TOOL_CALLING`；工具结果处理完后回到 `THINKING`。
3. 错误路径可以从**多个位置**进入 `ERROR`（如 `:6356`），并不只有一个固定的 `STREAMING -> ERROR` 边。正常结束时发出 `DONE`（`:8347`），generator 最后把内部 state 直接赋值重置为 `IDLE`（`:8372`）；**这个 reset 不等于额外向客户端发了一个 `DONE -> IDLE` 事件**。

### 2.5.3 `_transition` helper

源码：`src/opensquilla/engine/agent.py:10610-10613`

```python
    def _transition(self, to: AgentState) -> AgentEvent:
        # （方法体：设置 self._state = to，构造并返回 StateChangeEvent）
```

它做两件事：把内部 `_state` 改成目标状态，构造一个 `StateChangeEvent` 供 generator yield。所以 `yield self._transition(AgentState.THINKING)` 既改了状态、又发了事件。

---

## 2.6 `_turn_generator` 切片一：入口与 LLM 短路

从本节开始，我们把 `agent.py:3758-8373` 这个约 4600 行的巨型 generator 切成 6 片完整呈现。**第一片：入口初始化 + 三条 LLM 短路路径**。

源码位置：`src/opensquilla/engine/agent.py:3758-3805`

```python
    async def _turn_generator(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
        *,
        pending_input_provider: PendingInputProvider | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator that drives the state machine."""
        self._provider_tool_result_overrides = {}
        self._projected_diagnostic_evidence = {}
        self._focused_retrieved_tool_result_handles = set()
        self._current_turn_message = message
        _meta_invoke_turn_count.set(0)

        # ------ IDLE → THINKING ------
        yield self._transition(AgentState.THINKING)

        # PR7/9 E2E fix — consume meta_resolution's awaiting-branch
        # outcomes. meta_resolution stages six distinct outcomes on
        # ctx.metadata (resume / errors / cancelled / expired /
        # race_lost / [trigger match for fresh turn]) and returns; the
        # runtime owns the user-visible feedback for the first five so
        # the turn terminates cleanly instead of falling through to the
        # LLM (which would re-trigger meta_invoke and hit the
        # awaiting-guard with an opaque message).
        metadata = self.config.metadata or {}
        meta_resume = metadata.get("meta_resume")
        if meta_resume is not None:
            async for ev in self._run_meta_resume(meta_resume):
                yield ev
            return
        meta_launch = metadata.get("meta_launch")
        if meta_launch is not None:
            launch_name = (
                meta_launch.get("name") if isinstance(meta_launch, dict) else None
            )
            if launch_name:
                async for ev in self._run_meta_launch(launch_name):
                    yield ev
                return
        clarify_outcome = self._read_clarify_outcome(metadata)
        if clarify_outcome is not None:
            text, terminates = clarify_outcome
            async for ev in self._emit_terminal_text(text, iterations=0):
                yield ev
            _ = terminates  # always terminates today; reserved for future
            return
```

► **注解（逐行）**
- `:3767-3771`：每次进入 generator 都先**清空本 turn 的 override、诊断证据、focused retrieval handles**，并重置 `_meta_invoke_turn_count`。这是为了防止 Agent 实例被复用时把上一 turn 的临时状态带进来。
- `:3770`：`_current_turn_message` 保存当前运行文本，多个恢复/工具策略会读取它。
- `:3771`：`_meta_invoke_turn_count.set(0)`——`_meta_invoke_turn_count` 是当前 turn 的 meta 调用计数器（ContextVar），**不是**全局计数。
- `:3774`：`yield self._transition(AgentState.THINKING)`——这是 `IDLE → THINKING` 的状态转换，第一个被 yield 的事件。
- `:3776-3783`（注释）：解释了 meta_resume 短路存在的原因——meta_resolution 有六种 awaiting 分支结果（resume/errors/cancelled/expired/race_lost/trigger match），runtime 必须为前五种给用户可见反馈，否则会 fall through 到 LLM，重新触发 meta_invoke 并撞上 awaiting-guard。
- `:3784-3789`：`meta_resume` 短路。如果 config metadata 里有 `meta_resume`，直接跑 `_run_meta_resume` 并 return，**不调用 provider**。
- `:3790-3798`：`meta_launch` 短路，同理。
- `:3799-3805`：`clarify_outcome` 短路。如果上游 clarify 解析已有结论，直接 emit 终态文本并 return。注意 `_ = terminates`——`terminates` 今天恒为真，这里用 `_ =` 显式标记"保留给未来用"，避免 lint 警告未使用变量。

> **关键结论**：这三条短路证明"Agent turn 必定调用 provider"是**错误的**。短路 turn 可以在 `iterations=0` 的情况下结束。

### 2.6.1 真实数据样例：clarify 短路的 metadata

```json
// config.metadata（由 meta_resolution step 写入）
{
  "clarify_outcome": {
    "text": "你想让我检查 report.md 还是 report_final.md？",
    "terminates": true
  }
}

// Agent 直接 emit 这段文本，iterations=0，DoneEvent.iterations=0
```

---

## 2.7 `_turn_generator` 切片二：历史投影（不是历史删除）

进入 provider 前，历史消息会经过一组**安全/兼容投影**。这些操作发生在 **provider request view** 上，**不直接修改持久化 transcript**。

源码位置：`src/opensquilla/engine/agent.py:3814-3871`

```python
        thinking_prompt = semantic_message if semantic_message is not None else message
        thinking_enabled, thinking_budget = self.config.resolve_thinking(prompt=thinking_prompt)

        # Preprocess history for the provider request view. This does not
        # mutate persisted transcript rows or tool result content.
        # Some reasoning tool-call providers require the prior assistant
        # tool-call message to carry its reasoning_content while reasoning is
        # enabled, so keep that narrow field only for tool-call history.
        caps_reasoning_format = (
            getattr(self.config.model_capabilities, "reasoning_format", "")
            if self.config.model_capabilities is not None
            else ""
        )
        preserve_reasoning_content = bool(
            _is_direct_deepseek_v4_model_id(self.config.model_id)
            or (
                thinking_enabled
                and caps_reasoning_format == "deepseek"
                and _is_deepseek_model_id(self.config.model_id)
            )
            or (thinking_enabled and caps_reasoning_format == "dashscope")
        )
        loaded_history = list(self._history)
        self._write_context_stage("session:loaded", loaded_history)
        sanitized_history, sanitize_result = sanitize_session_messages(loaded_history)
        sanitized_history, historical_projection_result = project_historical_tool_payloads(
            sanitized_history,
            preserve_reasoning_content=preserve_reasoning_content,
        )
        sanitized_history = repair_tool_pairing(sanitized_history)
        sanitized_history = drop_reasoning(
            sanitized_history,
            preserve_tool_call_reasoning=thinking_enabled,
            preserve_reasoning_content=preserve_reasoning_content,
        )
        preserve_historical_images = bool(
            self.config.preserve_historical_images
            and getattr(self.config.model_capabilities, "supports_vision", False)
            if self.config.model_capabilities is not None
            else False
        )
        sanitized_history = _strip_historical_image_blocks(
            sanitized_history,
            preserve_images=preserve_historical_images,
        )
        self._write_context_stage(
            "session:sanitized",
            sanitized_history,
            sanitize=sanitize_result,
            historical_projection=historical_projection_result.__dict__,
        )
        history = limit_turns(sanitized_history, self.config.max_history_turns)
        history = repair_tool_pairing(history)
        self._write_context_stage(
            "session:limited",
            history,
            removed_messages=max(len(sanitized_history) - len(history), 0),
        )
```

► **注解（五个转换的区别）**
1. `:3838` `sanitize_session_messages`：处理基本消息结构和非法/不完整记录（如缺 role、缺 content 的脏数据）。
2. `:3839-3842` `project_historical_tool_payloads`：根据 provider 兼容性投影历史工具载荷。有些 provider 对 tool result 的格式有特殊要求（如 reasoning_content 字段），这里按 `preserve_reasoning_content` 标志决定保留哪些字段。
3. `:3843` `repair_tool_pairing`：**确保历史 assistant tool call 与 tool result 成对**。provider 要求 `tool_calls` 和后续的 `role=tool` 消息严格配对，缺失配对会导致 provider 报错。
4. `:3844-3848` `drop_reasoning`：按当前 thinking 配置去掉不需要发送的 reasoning 内容。`preserve_tool_call_reasoning=thinking_enabled` 控制是否保留 tool call 消息里的 reasoning。
5. `:3855-3858` `_strip_historical_image_blocks`：剥离历史图片块（除非模型支持 vision 且配置保留历史图片）。
6. `:3865-3866` `limit_turns` + 二次 `repair_tool_pairing`：按 `max_history_turns` 截断后**再修一次配对**——因为截断可能把一对 tool call/result 从中间切开。

> **关键结论**：文档如果只写"加载历史"，会掩盖 provider 兼容性和上下文 token 的实际变化。这些投影是**运行时构造的请求视图**，持久化 transcript 一行不动。

### 2.7.1 真实数据样例：历史投影的 before/after

```json
// before：持久化 transcript 里的原始历史（假设 thinking 关闭）
[
  {"role": "assistant", "content": "让我读取文件。",
   "reasoning_content": "<很长的思考过程>", "tool_calls": [{"id":"c1","name":"read_file",...}]},
  {"role": "tool", "tool_call_id": "c1", "content": "<文件内容>",
   "reasoning_content": "<更多思考>"}
]

// after：经 drop_reasoning + project_historical_tool_payloads 后，发给 provider 的请求视图
[
  {"role": "assistant", "content": "让我读取文件。",
   "tool_calls": [{"id":"c1","name":"read_file",...}]},  // ← reasoning_content 被去掉
  {"role": "tool", "tool_call_id": "c1", "content": "<文件内容>"}  // ← reasoning_content 被去掉
]

// 持久化 transcript：原封不动，仍然保留 reasoning_content
```

---

## 2.8 `_turn_generator` 切片三：provider iteration 骨架与预算门控

接下来进入主循环。先看 iteration 骨架和**前置预算门控**（这是第 1 章 1.8 讲过的 `max_turn_llm_calls` 真正阻止下一次调用的地方）。

### 2.8.1 前置预算门控：`_turn_llm_call_budget_error`

源码：`src/opensquilla/engine/agent.py:4237-4247`

```python
        def _turn_llm_call_budget_error(next_call_number) -> ErrorEvent | None:
            max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
            if max_llm_calls is not None and next_call_number > max_llm_calls:
                return ErrorEvent(
                    message=(
                        f"Turn stopped before LLM call {next_call_number} "
                        f"(max_turn_llm_calls={max_llm_calls})."
                    ),
                    code="turn_llm_call_budget_exceeded",
                )
            return None
```

► **注解**：
- 这是**前置门控**——在发起下一次 provider 调用**之前**检查 `next_call_number > max_llm_calls`。
- 注意和后置门控 `_turn_budget_error`（`:4173`，第 1 章 1.8.2 样例用的那个）的区别：后置门控在调用**之后**检查已发生的累计值（input/output tokens、billed cost）；前置门控只针对 LLM call 次数，**阻止调用发生**。
- 实际触发点在 `agent.py:4628`：`terminal_error = _turn_llm_call_budget_error(turn_llm_calls + 1)`。

### 2.8.2 主循环入口与 iteration 计数

源码：`src/opensquilla/engine/agent.py:4293-4294`（主循环入口）、`:4435-4438`（iteration 开始）

```python
                # （主循环入口附近）
                try:
                    while True:                       # :4294 主循环
                        # ... 各种前置检查（deadline、预算、finalization pending）...
                        _arm_endgame_git_freeze_if_due()

                        iterations += 1               # :4435 进入新一轮

                        # ------ THINKING → STREAMING ------
                        yield self._transition(AgentState.STREAMING)   # :4438
```

► **注解**：
- `iterations += 1`（`:4435`）表示进入新一轮 Agent↔LLM 交互。
- `yield self._transition(AgentState.STREAMING)`（`:4438`）发出 `THINKING → STREAMING` 状态转换。
- 主循环是 `while True`，靠**内部的各种 break/return/error**退出，不是靠条件表达式。

### 2.8.3 真实数据样例：前置门控触发

```json
// 假设 max_turn_llm_calls=3，turn_llm_calls 已经是 3
// 进入第 4 次调用前，agent.py:4628 触发：
{
  "terminal_error": {
    "message": "Turn stopped before LLM call 4 (max_turn_llm_calls=3).",
    "code": "turn_llm_call_budget_exceeded"
  },
  "turn_llm_calls": 3,          // ← 没有变成 4，调用被阻止
  "iterations_completed": 2
}
```

关键：`turn_llm_calls` 停在 3，**第 4 次调用根本没发起**。这就是第 1 章说的"`max_turn_llm_calls` 在下一次 provider 调用前阻止超限，不是事后记录"。

---

## 2.9 `_turn_generator` 切片四：provider 调用与流消费（issue #358 在这里）

这是 turn loop 的心脏：调用 provider、消费流、区分 answer/intermediate 文本。

### 2.9.1 provider 调用与流消费循环

源码：`src/opensquilla/engine/agent.py:4730-4745`（调用）、`:4745-4775`（流消费，issue #358 核心）

```python
                        # （provider.chat 调用，:4730-4734）
                        raw_stream = self.provider.chat(
                            messages=...,
                            tools=provider_tools_for_call,
                            config=chat_cfg,
                            ...
                        )

                        # （流消费循环，:4745-4775）
                        async for raw_ev in self._stream_provider_events_with_deadline(
                            raw_stream,
                            loop=_loop,
                            total_deadline=_total_deadline,
                        ):
                            if first_event_at is None:
                                first_event_at = time.monotonic()
                            if isinstance(raw_ev, ProviderTextDelta):
                                assistant_text_parts.append(raw_ev.text)
                                if raw_ev.text:
                                    attempt_user_visible_emitted = True
                                if text_presentation_decided:
                                    # A tool already appeared this call, so all
                                    # text here is intermediate narration.
                                    yield TextDeltaEvent(
                                        text=raw_ev.text, presentation="intermediate"
                                    )
                                else:
                                    # No tool has appeared yet. Stream the text live,
                                    # token by token, as the answer rather than
                                    # holding it until the call ends: buffering froze
                                    # the Web UI for the whole generation on plain
                                    # (no-tool) Q&A, which is the common case on any
                                    # tools-capable model (issue #358). If a tool
                                    # later appears this call, subsequent text flips
                                    # to "intermediate" above; the few pre-tool tokens
                                    # already shown as answer are a deliberate,
                                    # harmless trade for live output.
                                    yield TextDeltaEvent(
                                        text=raw_ev.text, presentation="answer"
                                    )
```

► **注解（逐行）**
- `:4745`：provider stream 不是直接 `async for chunk in provider.stream()`，而是包了一层 `_stream_provider_events_with_deadline`（定义在 `agent.py:9941`），它把 deadline 检查织进流消费。
- `:4750-4751`：`first_event_at` 记录第一个事件到达时间，用于后续诊断 provider 延迟。
- `:4752`：`ProviderTextDelta` 是 provider 文本增量事件。
- `:4753`：文本累计到 `assistant_text_parts`，最终拼成完整回答。
- `:4756` `if text_presentation_decided:`：**这是 issue #358 的核心判断**。`text_presentation_decided`（初始化在 `:4473`）标志"本次调用里是否已经出现过 tool call"。
  - 已出现 tool call（`:4756` 为真）：后续文本标成 `presentation="intermediate"`（工具之间的旁白）。
  - 还没出现 tool call（`:4762` else）：文本标成 `presentation="answer"`，**立即实时流出**。
- `:4762-4775`（注释）：这是 issue #358 的**原文解释**——为什么不等整段响应结束再显示。

### 2.9.2 issue #358：不能等整个 provider 响应结束后才显示普通文本

源码注释位置：`src/opensquilla/engine/agent.py:4762-4775`。源码的实际策略是：
- 尚未发现 tool call：`TextDeltaEvent` 立即以 `answer` 形式流出；
- 已经出现 tool call：之后的文本以 `intermediate` 形式流出；
- 在同一次 provider 调用中，tool call 可能晚于前几个文本 token 出现，因此**已经发出的 answer 不回收**。

这是一项明确的用户体验/一致性权衡：

| 方案 | 优点 | 缺点 |
|---|---|---|
| A：缓存整段响应，确认无工具后再显示 | answer/intermediate 分类**绝对准确** | 普通问答要等整次生成结束，界面像卡住 |
| B（当前实现）：先实时显示，出现工具后再切换后续文本语义 | 普通 Q&A 立即有反馈 | tool call 出现前的少量文本已被标成 answer，不能回收 |

当前实现选择方案 B，源码注释（`:4771-4772`）明确把这视为 `harmless trade`（无害权衡）。`text_presentation_decided` 在 tool call 首次出现时置为 True（`:4952`）。

### 2.9.3 真实数据样例：answer/intermediate 切换

```json
// provider 一次调用里同时输出文本和 tool call，时序如下：
[
  {"type": "TextDelta", "text": "我来"},          // → TextDeltaEvent presentation="answer"
  {"type": "TextDelta", "text": "读取文件。"},      // → TextDeltaEvent presentation="answer"
  {"type": "ToolUse",   "name": "read_file", ...}, // ← text_presentation_decided 置 True
  {"type": "TextDelta", "text": "正在分析..."}      // → TextDeltaEvent presentation="intermediate"
]

// 客户端看到：
// "我来读取文件。"（作为 answer 流式显示）
// [tool call read_file 执行]
// "正在分析..."（作为 intermediate 显示，不覆盖 answer）
```

> **关键结论**：前两个 delta 已经以 answer 发出，**不回收**。这就是 issue #358 选择的"harmless trade"。

---

## 2.10 `_turn_generator` 切片五：retry 子循环（iteration ≠ retry）

每个 iteration 内部还有一个 retry 子循环。必须分清 **iteration** 和 **retry**。

源码位置：`src/opensquilla/engine/agent.py:4452-4463`（retry 初始化）、`:6355-6382`（retry 退出）

```python
                _retry_attempt = 0
                _call_attempt = 0
                _reasoning_cap_preempt_done = False
                attempt_reasoning_stream_chars = 0
                _retry_policy = _ProviderRetryPolicy.from_provider_budget(
                    _fallback.max_retries,
                    length_capped_continuations=self.config.length_capped_continuations,
                )
                _attempt_retries_used = _retry_policy.used_attempts()
                _invalid_response_fallback_done = False
                _message_limit_recovery_done = False
                while _retry_attempt <= _fallback.max_retries:      # :4463 retry 子循环
                    provider_error = None
                    assistant_text_parts = []
                    tool_calls = []
                    pending_tools = {}
                    seen_tool_use_ids: set[str] = set()
                    text_presentation_decided = False
                    tool_argument_heartbeat_chars = {}
                    iter_input_tokens = 0
                    iter_output_tokens = 0
                    iter_reasoning_tokens = 0
                    iter_reasoning_content = None
                    iter_thinking_signature = None
                    _got_error = False
                    _stream_policy_preempt = False
                    attempt_reasoning_stream_chars = 0
                    provider_done_for_log: ProviderDoneEvent | None = None
                    provider_error_for_log: ProviderErrorEvent | None = None
                    call_id = f"{iterations}.{_call_attempt}"
                    # ...（provider 调用与流消费，见 2.9）...
```

retry 失败后的处理（`:6355-6382`）：

```python
                if not _fallback.should_retry(provider_error, ...):     # :6355
                    yield self._transition(AgentState.ERROR)             # :6356
                    # ...（构造 error event）...
                    break                                                # :6362
                # backoff + 自增
                delay = backoff_sleep(...)                               # :6363 附近
                await asyncio.sleep(delay)
                _retry_attempt += 1                                      # :6377 附近
```

► **注解（iteration vs retry）**
- `iterations += 1`（`:4435`）：表示进入**新一轮 Agent-LLM 交互**（新一轮思考+可能工具调用）。
- `_retry_attempt += 1`（`:6377`）：只表示**当前 provider 调用失败后重试**，通常**不算新 iteration**。
- 每次 retry 都**重新清理** `assistant_text_parts`/`tool_calls`/`pending_tools`（`:4464-4482`），避免把失败响应和重试响应拼在一起。
- `iter_*_tokens`（`:4475-4477`）记录当前 iteration 的用量，最终再累加到 turn 级 totals。
- `seen_tool_use_ids`（`:4468`）：防止同一次响应重复处理同一个工具调用。
- `call_id = f"{iterations}.{_call_attempt}"`（`:4485`）：调用标识符，`iteration.retry` 格式，便于日志关联。
- `length_capped_continuations` 与普通 transient retry 共用部分预算，但业务含义不同：前者是**输出被截断后的继续生成**，后者是 **provider 调用失败后的重试**。

### 2.10.1 `FallbackPolicy` 的真实字段（修正旧文档）

源码：`src/opensquilla/engine/fallback.py:28-83`

> **修正旧文档**：旧文档写 `retry_base_backoff_ms` / `retry_max_backoff_ms` 是**错的**。实际字段名是 `base_backoff_ms`（默认 1000）和 `max_backoff_ms`（默认 30000）。

```python
@dataclass
class FallbackPolicy:
    max_retries: int = ...          # :30
    fallback_models: list[str] = ... # :31
    base_backoff_ms: int = 1000      # :32  ← 不是 retry_base_backoff_ms
    max_backoff_ms: int = 30_000     # :33  ← 不是 retry_max_backoff_ms
```

`FallbackPolicy` 提供 transient provider error 的重试参数。重试**不等于**开始了新的 Agent iteration；它仍然属于当前 provider 调用的**恢复子循环**。

### 2.10.2 控制动作分类器：`turn_control.py`

模型可能因为输出长度达到上限而需要 continuation，context window 可能在运行中溢出，provider 也可能要求消息数量修复。`turn_control.py` 的纯函数把停止码/运行事实映射成控制动作。

源码：`src/opensquilla/engine/turn_control.py:21-31`（控制动作枚举）、`:96-145`（主分类器 `decide_turn_control`）

```python
TurnControlAction = Literal[
    "continue",                  # :22 继续下一次 iteration
    "retry",                     # :23 provider 调用重试
    "compact_then_continue",     # :24 压缩上下文后继续
    "respond_to_model",          # :25 把恢复消息发回模型
    "finalize_partial",          # :26 部分完成
    "budget_limited",            # :27 预算耗尽
    "blocked",                   # :28 被阻断（如审批）
    "failed",                    # :29 失败
    "interrupted",               # :30 被中断
]
```

► **注解**：控制分类器只**决定**下一步动作，**不直接执行**重试或压缩；真正的执行仍在 Agent/runtime 的对应分支中。这种"决策与执行分离"是为了让决策逻辑可单元测试。

### 2.10.3 真实数据样例：retry 与 iteration 的区别

```json
// 一次 turn 发生了一次 transient provider error 并 retry 成功
{
  "iterations": 2,              // ← 2 轮 Agent-LLM 交互
  "turn_llm_calls": 3,          // ← 但 3 次 provider 调用（iteration 1 正常 + iteration 2 失败1次+retry1次）
  "call_log": [
    {"call_id": "1.0", "status": "ok"},
    {"call_id": "2.0", "status": "transient_error", "error": "connection_reset"},
    {"call_id": "2.1", "status": "ok", "retry_of": "2.0"}
  ]
}
```

注意 `iterations=2` 但 `turn_llm_calls=3`——retry 算进了 LLM call 次数，但不算进 iteration。所以 `max_iterations` 和 `max_turn_llm_calls` 是两个**独立的预算维度**（第 1 章 1.8 讲过）。

---

## 2.11 `_turn_generator` 切片六：工具并发的三层锁

工具调用阶段。当前策略是**先按工具并发策略切分，再执行**。

### 2.11.1 工具分发主循环

源码：`src/opensquilla/engine/agent.py:7448-7528`

```python
                parallel_batch: list[ToolCall] = []                    # :7448

                async def _flush_parallel_batch(                        # :7450
                    batch: list[ToolCall],
                ) -> AsyncIterator[RunHeartbeatEvent]:
                    if not batch:
                        return
                    semaphore = asyncio.Semaphore(self._max_safe_tool_concurrency())
                    keyed_locks: dict[Any, asyncio.Lock] = {}
                    limiters: dict[Any, asyncio.Semaphore] = {}

                    async def _run_limited(tc: ToolCall) -> ToolResult:
                        policy = _get_tool_concurrency_policy(
                            tc.tool_name,
                            tc.arguments,
                            parent_session_key=self._session_key,
                        )
                        key_lock = (
                            keyed_locks.setdefault(policy.key, asyncio.Lock())
                            if policy.key is not None
                            else None
                        )
                        limiter = None
                        if policy.max_inflight is not None:
                            limit_key = policy.limit_key or tc.tool_name
                            limiter = limiters.setdefault(
                                limit_key,
                                asyncio.Semaphore(max(1, int(policy.max_inflight))),
                            )

                        async def _run_after_policy_locks() -> ToolResult:
                            async with semaphore:
                                return await _run_one(tc)

                        async def _run_after_key_lock() -> ToolResult:
                            if limiter is None:
                                return await _run_after_policy_locks()
                            async with limiter:
                                return await _run_after_policy_locks()

                        if key_lock is None:
                            return await _run_after_key_lock()
                        async with key_lock:
                            return await _run_after_key_lock()

                    task_to_tool_call = {asyncio.create_task(_run_limited(tc)): tc for tc in batch}
                    async for event in _collect_tool_tasks(task_to_tool_call):
                        yield event

                for tc in tool_calls:                                   # :7497 主分发循环
                    if tc.tool_name == "meta_invoke":                   # :7498 特殊路径
                        async for event in _flush_parallel_batch(parallel_batch):
                            yield event
                        parallel_batch = []
                        active_ctx = (
                            current_tool_context.get() or self._tool_context or ToolContext()
                        )
                        async for ev in self._run_one_streaming(tc, active_ctx):
                            if isinstance(ev, ToolResult):
                                results_by_id[tc.tool_use_id] = ev
                            else:
                                yield ev
                        continue
                    policy = _get_tool_concurrency_policy(              # :7511
                        tc.tool_name,
                        tc.arguments,
                        parent_session_key=self._session_key,
                    )
                    if policy.mode != "mutex":                          # :7516
                        parallel_batch.append(tc)
                    else:                                               # :7518 mutex 分支
                        async for event in _flush_parallel_batch(parallel_batch):
                            yield event
                        parallel_batch = []
                        async for event in _collect_tool_tasks(
                            {asyncio.create_task(_run_one(tc)): tc}
                        ):
                            yield event

                async for event in _flush_parallel_batch(parallel_batch):  # :7527 收尾 flush
                    yield event
```

► **注解（三层锁的执行层级）**

执行层级从外到内是：

```text
key_lock（同资源串行）           ← _run_limited 最外层 async with
  -> limiter（某工具/limit key 的并发上限）  ← _run_after_key_lock
      -> semaphore（本 batch 全局安全并发上限）  ← _run_after_policy_locks
          -> _run_one（真正执行工具）
```

为什么需要**三层**：
- `semaphore`（`:7455`）只能限制总数量，**不知道**两个调用是否操作同一个文件。默认 `AgentConfig.max_safe_tool_concurrency = 6`（`_max_safe_tool_concurrency()` 在 `agent.py:1802`），这是安全工具并发上限，**不是**每个 turn 固定恰好并发 6 个。
- `key_lock`（`:7465-7469`）能让**同一资源**串行，但**不能**限制不同资源上同一个远程 API 的总 inflight。
- `limiter`（`:7471-7476`）处理工具级或服务级并发限制（`policy.max_inflight`）。
- `_run_one` 负责单个工具的预检、权限、超时、执行、结果记录；它**不是**并发调度器。

### 2.11.2 为什么 mutex 工具要切断 batch

`for tc in tool_calls:` 主循环（`:7497`）保证：
1. **mutex 前的并发工具先完成**（`:7519` flush）。
2. **mutex 工具单独运行**（`:7522`）。
3. **mutex 后的并发工具不会提前启动**（batch 被清空，重新累积）。
4. **`meta_invoke` 还必须走 streaming 特殊路径**（`:7498-7510`）：它先 flush 当前并发批次，再按 streaming 方式执行，**不能**与普通工具混在同一批次。
5. **最终结果仍按 `tool_calls` 原始顺序发回模型**（见下面 2.11.3）。

### 2.11.3 结果按原始顺序投递

源码：`src/opensquilla/engine/agent.py:7530-7547`

```python
                # Emit results in original tool_calls order.
                for tc in tool_calls:                                   # :7531
                    result = results_by_id[tc.tool_use_id]
                    result_tool_call = tc
                    for artifact in result.artifacts:
                        yield ArtifactEvent(**_artifact_event_kwargs(artifact))
                    projected_result = await self._project_tool_result_for_delivery(
                        result,
                        tool_call=result_tool_call,
                    )
                    yield ToolResultEvent(
                        tool_use_id=projected_result.tool_use_id,
                        tool_name=projected_result.tool_name,
                        result=projected_result.content,
                        is_error=projected_result.is_error,
                        arguments=tc.arguments,
                        execution_status=projected_result.execution_status,
                    )
```

► **注解**：`results_by_id` 是 dict（key 是 `tool_use_id`），执行时并发写入；但投递时**按 `tool_calls` 原始顺序**遍历（`:7531`）。所以即使底层执行是并发的，**工具结果的输出顺序与 provider 给出的 tool call 顺序一致**。这是 provider 消息契约的要求——provider 期望 tool result 按它发出的 tool call 顺序返回。

### 2.11.4 真实数据样例：并发 vs 顺序

```json
// provider 一次返回 4 个 tool call（顺序：A, B, C, D）
// 假设 A、C 可并发，B 是 mutex，D 可并发
// 执行时序：
[
  {"phase": "parallel_batch", "tools": ["A", "C-预备"], "note": "A,C 进入 batch"},

  {"phase": "flush", "tools": ["A", "C"], "note": "遇到 mutex B，先 flush A,C"},
  {"phase": "mutex", "tools": ["B"], "note": "B 单独串行执行"},
  {"phase": "parallel_batch", "tools": ["D"], "note": "D 进入新 batch"},
  {"phase": "flush", "tools": ["D"], "note": "收尾 flush"}
]

// 但投递给模型的 ToolResultEvent 顺序固定是：A, B, C, D（原始顺序）
// 即使 C 可能比 A 先执行完
```

---

## 2.12 预算、超时与停止条件（第 1 章 1.8 的运行时落地）

第 1 章已经给了预算维度总表。这里讲它们在 turn loop 里**怎么落地**。

### 2.12.1 后置预算门控：`_turn_budget_error`

源码：`src/opensquilla/engine/agent.py:4173-4235`（完整贴出关键分支）

```python
        def _turn_budget_error() -> ErrorEvent | None:
            max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
            if max_llm_calls is not None and turn_llm_calls > max_llm_calls:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {turn_llm_calls} LLM calls "
                        f"(max_turn_llm_calls={max_llm_calls})."
                    ),
                    code="turn_llm_call_budget_exceeded",
                )
            max_input = self._positive_int(getattr(self.config, "max_turn_input_tokens", 0))
            if max_input is not None and total_input_tokens > max_input:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_input_tokens} input tokens "
                        f"(max_turn_input_tokens={max_input})."
                    ),
                    code="turn_input_token_budget_exceeded",
                )
            max_output = self._positive_int(getattr(self.config, "max_turn_output_tokens", 0))
            if max_output is not None and total_output_tokens > max_output:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_output_tokens} output tokens "
                        f"(max_turn_output_tokens={max_output})."
                    ),
                    code="turn_output_token_budget_exceeded",
                )
            max_cost = _positive_float(getattr(self.config, "max_turn_billed_cost_usd", 0.0))
            if max_cost is not None and total_billed_cost > max_cost:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after billed cost ${total_billed_cost:.4f} "
                        f"(max_turn_billed_cost_usd={max_cost})."
                    ),
                    code="turn_billed_cost_budget_exceeded",
                )
            # ...（max_turn_cost_usd 估算累计、max_turn_tool_errors 等后续分支）...
```

► **注解**：
- 这是**后置门控**：在 provider 调用**之后**（retry loop 退出后，`:5346` 调用）检查累计值。
- 检查顺序是 LLM calls → input tokens → output tokens → billed cost → estimated cost → tool errors。
- 每个分支返回对应的 `code`，finalizer 会把这个 code 写进 turn error metadata（第 1 章 1.8.2 的样例就是 `turn_llm_call_budget_exceeded`）。

### 2.12.2 工具超时与 deadline

`timeout` / `iteration_timeout` / `tool_timeout` 还会受**外层 deadline** 限制。`_stream_provider_events_with_deadline`（`:9941`）把 `total_deadline` 织进流消费；工具收集器（`_collect_tool_tasks`）会在 deadline 到达时取消未完成任务，并生成带 `timeout` 状态的 `ToolResult`。

### 2.12.3 CompactionAndHistoryStage：不是"太长就截断"

当前 stage 的职责按四步组织：

```text
t3 upgrade / compaction policy
    -> preflight
    -> load history
    -> prepend request-context prompt
```

它需要**已 bootstrap 的 model/context window**，因此位于 `AgentBootstrapStage` 之后。压缩可能是本 turn 的**恢复动作**（运行中 context overflow 触发），也可能由当前上下文预算预检触发。Agent 内部还存在运行中 context overflow 的检测与恢复分支；二者都**不能**被描述为"只在 turn 开头压缩一次"。

### 2.12.4 AttachmentStage

附件被转换成 Agent 能消费的额外消息和 `turn_input`。图片附件会影响路由和 provider capability，普通文件附件可能物化到 workspace 或构建额外上下文。附件处理必须在**已知 workspace 和 session logging id 后**进行，所以当前调用顺序在 bootstrap/history 之后。

---

## 2.13 流消费与 finalizer：结果如何落地

### 2.13.1 StreamConsumerStage 的职责

`StreamConsumerStage`（`runtime.py:3231`）负责把 Agent event stream 消费成可供 surface 和 finalizer 使用的数据：

```text
AgentEvent stream
  -> surface event yield（实时显示给客户端）
  -> final_text_parts（最终文本片段累计）
  -> turn_segments（工具边界前后的文本片段）
  -> turn_artifacts（工具产物）
  -> error / pending error / done state
```

如果中途发生 router-control replay，stream consumer 可以发出 replay event；runtime 会以**递增的 replay depth** 重新进入 `_run_turn()`，并**避免重复持久化**原始 user input（`persist_input=False`）。

### 2.13.2 TurnFinalizerStage 的收尾顺序

流结束后，harness 会先把尚未关闭的当前文本段 flush 到 `turn_segments`，再调用 `TurnFinalizerStage`（`runtime.py:3292`）。finalizer 按既定顺序处理：

1. 规范化 heartbeat/stream 状态；
2. 追加 assistant transcript；
3. 尝试 memory_capture；
4. 持久化 turn error（如有）；
5. 计算 session totals/cost rollup；
6. 返回 final text、segments 和事件信息。

> **关键结论**："用户已经看到最后一个 text delta"**不代表** turn 已经完成——transcript、usage 和 memory 副作用可能仍未执行。这就是为什么 finalizer 失败要尽量隔离，避免持久化失败吞掉运行结果。

---

## 2.14 错误、取消和部分完成

### 2.14.1 Provider 解析失败

`ProviderAndToolsStage` 可以在没有可用 provider 时提前 yield `ProviderResolutionError`（2.2.3 已贴代码），记录 turn error 后返回。此时**不会进入 Agent loop**。

### 2.14.2 Pipeline step 失败

普通 step 失败时，pipeline 记录失败并继续（2.3.2 已讲 fail-open）。路由 step 通过 cloned turn 和独立 timeout 运行，失败通常回退到原有 model/selector；这**不是**"所有步骤失败都必然安全"——自定义 step 的外部副作用仍需自己保证幂等。

### 2.14.3 Provider transient error

由 `FallbackPolicy` 决定是否重试、退避多久（2.10 已讲）。重试耗尽后进入 error/control 路径（`:6356` 的 `_transition(AgentState.ERROR)`），是否能部分 finalization 取决于 Agent 是否已有可交付文本、artifact 或可恢复状态。

### 2.14.4 工具失败

工具异常、取消或超时会被转换为 `ToolResult(is_error=True, execution_status=...)`，然后通常继续把结果交给模型，让模型决定修复或结束。达到 `max_turn_tool_errors` 后，turn budget gate 可以终止继续调用。

### 2.14.5 取消

取消发生在 stream 开始前、provider stream 中或工具任务运行中都可能发生。runtime 在取消处理里**保留已经收集的文本片段和工具段**，并尽量执行必要的收尾；**不要假设**取消一定是"没有任何 transcript"。

### 2.14.6 上下文溢出

Agent 会检测 provider/context window 限制，并可尝试消息数恢复、flush 或 compaction。控制动作是 `compact_then_continue` 时（`turn_control.py:24`），压缩完成后才重新调用 provider；如果恢复失败，才进入 partial/error 终态。

---

## 2.15 issue #305 与 issue #418：源码注释记录的两个设计取舍

第 1 章列出了三个 issue，issue #358 已在 2.9.2 讲透。这里讲剩下两个。

### 2.15.1 issue #305：tool-result store 的 O(store) 扫描不能阻塞 event loop

源码：`src/opensquilla/engine/agent.py:3207-3212`（在 `_project_tool_result_for_delivery` 内）

```python
                raw_snapshot_record = await asyncio.to_thread(
                    self._store_tool_result_snapshot,
                    raw_snapshot_content,
                    tool_use_id=result.tool_use_id,
                    tool_name=result.tool_name,
                )
```

工具结果完整快照和 JSON projection 可能触发 store-wide cleanup/rglob。源码通过 `asyncio.to_thread` 把阻塞文件系统工作移出 gateway event loop。

> **设计动机（解释）**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 不做快照 | 省磁盘和延迟 | 丢失大工具结果的可恢复证据 |
| 在 event loop 内直接写 | 实现简单 | store 越大，所有并发 turn 的延迟越差 |
| 放到 worker thread（当前实现） | 保留 trace 能力 | 线程调度和文件写入并发复杂度 |

当前代码选择第三种，并且在完整 message assembly 上也使用 `to_thread`，确保 O(store) 扫描不阻塞 async gateway。这个 issue **不能**被概括成"工具结果异步保存"——真正原因是**共享 store 的扫描复杂度随 store 增长**。

### 2.15.2 issue #418：allow-once sandbox grant 必须按 turn 过期

源码：`src/opensquilla/engine/agent.py:3741-3749`

```python
        # （导入，:3741-3742）
        from opensquilla.sandbox.governance import (
            clear_sandbox_approval_denials,
            prune_once_mount_grants,
        )
        # （调用，:3745, :3749）
        clear_sandbox_approval_denials(self._session_key)
        prune_once_mount_grants(self._session_key)
```

源码注释明确：allow once 只授权 **granting turn**，下一 turn 必须过期，否则后续访问会被静默放行。

> **设计动机（解释）**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 按 session 保留 grant | 用户少点确认 | 越权窗口扩大 |
| 每次工具调用都重新确认 | 安全更强 | 正常连续操作体验差 |
| 只保留到当前 turn（当前实现） | 把"本 turn 的连续工具操作"和"下一 turn 的新意图"分开 | 边界稍复杂 |

这里的边界**不是** `AgentState`，而是 **sandbox escalation store 的生命周期**；文档不能只写"有审批"，还要说明审批记录**何时失效**。

---

## 2.16 一个可审计的完整例子

用户输入：`请检查 workspace/report.md 的语法错误，并告诉我第几行需要修改。`

```text
1. InputStage（runtime.py:2921）
   runtime_message = 原文
   semantic_input  = 原文

2. ProviderAndToolsStage（runtime.py:2938）
   解析 provider
   根据 ToolContext 构建 read_file 等工具定义和 handler

3. PromptAssemblerStage（runtime.py:2979）
   创建 pipeline.TurnContext
   resolve_model 得到基础模型
   apply_vision_followup_gate 无图片，放行
   apply_squilla_router 得到 c1 / 某个模型（以实际配置为准）
   后续 steps 注入 skills、platform、prompt cache
   把 turn.model 应用到 cloned selector
   生成 final_prompt 和 prompt_report

4. AgentBootstrapStage（runtime.py:3082）
   构造 AgentConfig、Agent、超时和预算快照

5. CompactionAndHistoryStage（runtime.py:3150）
   加载历史；若窗口足够则不压缩
   prepend request context（若有）

6. AttachmentStage（runtime.py:3178）
   无附件，turn_input 保持文本

7. StreamConsumerStage / Agent（runtime.py:3231）
   IDLE -> THINKING（agent.py:3774）
   THINKING -> STREAMING（agent.py:4438）
   provider 返回 read_file tool call
   STREAMING -> TOOL_CALLING
   执行 read_file，按原 tool call 顺序发出 ToolResultEvent（agent.py:7531）
   TOOL_CALLING -> THINKING -> STREAMING
   provider 返回最终文本
   STREAMING -> DONE（agent.py:8347）
   self._state = IDLE（agent.py:8372，不发事件）

8. TurnFinalizerStage（runtime.py:3292）
   追加 assistant transcript
   写入 usage/cost rollup
   返回 done event 和最终 surface 事件
```

如果 provider 在第 7 步超时，可能先重试（`_retry_attempt += 1`，不算新 iteration）；如果上下文溢出，可能走 `compact_then_continue` 恢复；如果工具报错，模型可能再进行一次 iteration。例子描述的是**正常路径**，**不是**固定调用次数。

### 2.16.1 这个例子的真实事件序列 JSON

```json
[
  {"type": "StateChange", "state": "thinking",  "seq": 1},
  {"type": "StateChange", "state": "streaming", "seq": 2},
  {"type": "TextDelta",   "text": "我来",        "presentation": "answer", "seq": 3},
  {"type": "TextDelta",   "text": "读取文件。",   "presentation": "answer", "seq": 4},
  {"type": "ToolResult",  "tool_name": "read_file",
   "tool_use_id": "c1", "is_error": false, "seq": 5},
  {"type": "StateChange", "state": "thinking",  "seq": 6},
  {"type": "StateChange", "state": "streaming", "seq": 7},
  {"type": "TextDelta",   "text": "第 12 行...", "presentation": "answer", "seq": 8},
  {"type": "StateChange", "state": "done",      "seq": 9},
  {"type": "Done",        "iterations": 2, "turn_llm_calls": 2, "seq": 10}
]
```

---

## 2.17 验收清单

修改 turn loop 代码后，至少验证：
- 同一 session 的并发 turn 不会交错写入 transcript；不同 session 可以并行。
- pipeline step 失败会有 `PipelineStepRecord(applied=False)`，且路由历史不会提交半成品。
- 路由 model 只修改 cloned selector，不修改共享 selector。
- provider 缺失会提前结束并持久化错误（`runtime.py:2938-2967`）。
- 工具结果最终按原始 tool call 顺序发送给模型和客户端（`agent.py:7531`）。
- mutex、keyed lock、global semaphore 三层约束都生效（`agent.py:7450-7495`）。
- `max_turn_llm_calls` 会在下一次 provider 调用前阻止超限（`agent.py:4628` 前置门控），而不仅是事后记录（`:5346` 后置门控）。
- context overflow、provider retry、tool timeout、取消和 replay 都不会导致锁永久不释放（`run()` 的 finally 清理）。
- finalizer 失败/部分失败不会抹掉已经收集的最终文本和 usage 信息。
- allow-once sandbox grant 在 turn 结束后被 prune（issue #418，`agent.py:3745/3749`）。

---

## 2.18 小结

一次 turn **不是**一个简单的 `provider.chat()` 调用，而是：

```text
session lock（runtime.py:2697 run()）
  -> 输入规范化（InputStage）
  -> provider/tool 解析（ProviderAndToolsStage）
  -> prompt + 11 步 pre-turn pipeline（PromptAssemblerStage）
  -> 局部 selector 重绑定（apply_model_override）
  -> AgentConfig/Agent bootstrap（AgentBootstrapStage）
  -> history/compaction/attachments（CompactionAndHistory + Attachment）
  -> 多 iteration 的流式 provider-tool loop（Agent._turn_generator）
       ├─ LLM 短路（meta_resume/launch/clarify）
       ├─ 历史投影（sanitize/project/repair/drop）
       ├─ provider iteration（前置预算门控 → 流消费 → answer/intermediate 分类）
       ├─ retry 子循环（iteration ≠ retry）
       └─ 工具并发（三层锁 + 原序投递）
  -> 事件消费（StreamConsumerStage）
  -> transcript/memory/cost/error finalization（TurnFinalizerStage）
```

**下一章（第 3 章）** 只聚焦其中的 `apply_squilla_router`：它如何从文本/附件/历史得到候选 tier，如何经过后处理和策略门控，为什么最终 `routed_tier` 不一定等于 ML 的原始 argmax，以及路由失败时到底回退到哪里。`apply_squilla_router` 会按 7 片完整呈现。

---

> **版本与准确性说明**：本章基于 OpenSquilla 当前源码（HEAD `097db9d3`）实测行号。`agent.py` 约 13000+ 行、`runtime.py` 约 7700+ 行，是两个巨型文件；`_turn_generator` 本身约 4600 行（`agent.py:3758-8373`），本章按 6 个逻辑阶段切片完整呈现了关键部分。代码会持续演进，若发现文档与代码不符，以代码为准。
