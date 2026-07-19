# 第 6 章：Subagent 子 Agent —— 任务委派

> **本章目标**：讲透 OpenSquilla 的子 Agent 系统。读完本章，你会理解 Agent 怎么 spawn（生成）子 Agent、深度限制、隔离上下文、孤儿清理。

---

## 6.1 为什么需要子 Agent？（与 DeerFlow 相同的动机）

复杂任务（如"调研三个主题并写报告"）在一个上下文里完成会上下文爆炸。子 Agent 让主 Agent 把子任务委派给**独立上下文**的子 Agent，只回收精炼结果。

---

## 6.2 核心数据结构

### SubagentSpec —— 生成参数

```python
# 引用位置：src/opensquilla/engine/subagent.py:23-32
@dataclass
class SubagentSpec:
    task: str                    # 任务描述
    label: str = ""              # 标签（展示用）
    model_id: str | None = None  # 指定模型（None=继承）
    timeout: float = 300.0       # 超时（5分钟）
    max_iterations: int = 0      # 最大迭代（0=不限）
    workspace_dir: str | None = None  # 工作区（None=继承）
    extra_context: dict[str, Any] = field(default_factory=dict)  # 额外上下文
```

### SubagentHandle —— 运行引用

```python
# 引用位置：src/opensquilla/engine/subagent.py:36-47
@dataclass
class SubagentHandle:
    run_id: str                    # 唯一ID
    label: str
    task: asyncio.Task[str]        # asyncio 任务引用
    status: str = "running"        # running/done/error/aborted/archived/orphaned
    result: str = ""               # 结果文本
    error: str = ""                # 错误
    parent_task_id: int | None = None  # 父任务 id（孤儿追踪）
    spawned_at: float = ...        # 生成时间
    completed_at: float | None = None  # 完成时间
```

### 六种状态

```
running → done / error / aborted
         ↓
       archived（归档）
       orphaned（孤儿——父任务死了）
```

---

## 6.3 SubagentManager —— 生命周期管理

```python
# 引用位置：src/opensquilla/engine/subagent.py:165-180
class SubagentManager:
    def __init__(self, spawn_depth=0, max_depth=DEFAULT_MAX_SPAWN_DEPTH, max_concurrent=5):
        self.spawn_depth = spawn_depth    # 当前深度
        self.max_depth = max_depth        # 最大深度
        self.max_concurrent = max_concurrent  # 最大并发
        self.registry = SubagentRegistry()

    def can_spawn(self) -> bool:
        """Return True if depth and concurrency limits allow spawning."""
```

### 两个限制

```python
# 引用位置：src/opensquilla/agents/limits.py:11
MAX_SPAWN_DEPTH = 3  # 最大嵌套深度3层
```

- **`max_depth = 3`**：子 Agent 最多嵌套 3 层（子→孙→重孙，不能更多）。防止无限递归。
- **`max_concurrent = 5`**：同一个父 Agent 最多 5 个并发子 Agent。

**`can_spawn()` 检查两个限制**——深度和并发都满足才能 spawn。

---

## 6.4 SubagentRegistry —— 运行追踪

```python
# 引用位置：src/opensquilla/engine/subagent.py:50-162
class SubagentRegistry:
    # _runs: dict[str, SubagentHandle]     — 活跃运行
    # _archived: dict[str, SubagentHandle]  — 归档运行
    # _parent_tasks: dict[str, asyncio.Task] — 父任务引用
```

### 孤儿清理（cleanup_orphans）

```python
# 引用位置：src/opensquilla/engine/subagent.py:105-114
    def cleanup_orphans(self) -> list[str]:
        """Abort handles whose parent task is done."""
        aborted: list[str] = []
        for run_id, parent_task in list(self._parent_tasks.items()):
            if parent_task.done():  # 父任务已结束
                handle = self._runs.get(run_id)
                if handle and handle.status == "running":
                    self.abort(run_id)  # 中止孤儿子 Agent
                    aborted.append(run_id)
        return aborted
```

**► 设计动机**：如果父 Agent 的 turn 结束了（正常或异常），它 spawn 的子 Agent 不应该继续跑——它们的结果没人接收了。`cleanup_orphans` 检测父任务已结束的子 Agent，中止它们。

### 状态持久化

```python
# 引用位置：src/opensquilla/engine/subagent.py:116-162
    def save_state(self, path: Path) -> None:
        """Serialize registry metadata to JSON (no asyncio.Task objects)."""

    def load_state(self, path: Path) -> dict[str, SubagentHandle]:
        """Restore registry from JSON. All loaded handles are marked 'orphaned'."""
```

**► 注解**：注册表可以序列化到 JSON 持久化。加载时所有 handle 标记为 `orphaned`——因为重启后原来的 asyncio Task 已经不存在了。

---

## 6.5 Agent.spawn_subagent

```python
# 引用位置：src/opensquilla/engine/agent.py:13103
# Agent.spawn_subagent → 构建子 Agent（带 _subagent_tool_handler, agent.py:12958）
```

**spawn 流程**：
1. `can_spawn()` 检查深度和并发限制。
2. 创建子 `Agent` 实例（用 `SubagentSpec` 的配置）。
3. 子 Agent 带 `_subagent_tool_handler`——限制它能调用的工具（某些工具对子 Agent 禁用，如 `memory_get`/`memory_search` 在 `SUBAGENT_TOOL_DENY` 中）。
4. 在独立的 session_key（`:subagent:` 标记）下运行。
5. 结果通过 `SubagentHandle.result` 回传。

### 子 Agent 的工具限制

```python
# 引用位置：src/opensquilla/tools/types.py:118
# SUBAGENT_TOOL_DENY — 子 Agent 禁用的工具集
# 包含 memory_get, memory_search 等
```

**设计动机**：子 Agent 不应该访问主 Agent 的记忆——记忆是 per-user 的，子 Agent 是一次性任务执行者。

### 子 Agent 不走路由

```python
# 引用位置：src/opensquilla/engine/steps/squilla_router.py:1080
    if ":subagent:" in ctx.session_key:
        return ctx  # 子 Agent session 不路由
```

子 Agent 用父 Agent 指定的模型（或 `SubagentSpec.model_id`），不走 SquillaRouter——路由是 per-turn 的优化，子 Agent 的任务通常已经明确。

---

## 6.6 子 Agent 的 grounding 注入

```python
# 引用位置：src/opensquilla/engine/steps/inject_subagent_grounding.py
# pre-turn pipeline 的第 9 步
# 给子 Agent 注入父 Agent 的上下文 grounding
```

子 Agent 需要知道"父 Agent 为什么 spawn 我"——这个步骤注入必要的上下文。

---

## 6.7 与 DeerFlow 子 Agent 的对比

| 维度 | DeerFlow | OpenSquilla |
|------|----------|-------------|
| **触发方式** | `task` 工具 | `spawn_subagent`（内部） |
| **执行环境** | 双线程池 + 持久 event loop | asyncio Task（同一事件循环） |
| **深度限制** | 无显式嵌套限制 | **MAX_SPAWN_DEPTH=3** |
| **并发限制** | max_concurrent_subagents（双闸） | **max_concurrent=5** |
| **工具限制** | disallowed_tools 列表 | **SUBAGENT_TOOL_DENY** 集合 |
| **路由** | 用父 Agent 模型 | **不走 SquillaRouter** |
| **孤儿清理** | 无 | **cleanup_orphans** |
| **状态持久化** | 无 | **save_state/load_state** |

---

## 6.8 本章小结

子 Agent 系统的核心设计：

1. **SubagentSpec + SubagentHandle**：声明式 spawn 参数 + 运行引用。
2. **双重限制**：深度（3层）+ 并发（5个），`can_spawn()` 检查。
3. **孤儿清理**：父任务结束后自动中止子 Agent。
4. **状态持久化**：save/load JSON，加载后标记 orphaned。
5. **工具限制**：SUBAGENT_TOOL_DENY 禁止子 Agent 访问记忆等。
6. **不走路由**：子 Agent 用指定模型，不走 SquillaRouter。

**核心思想**：子 Agent 是"有限能力的临时帮手"——深度限制防递归、并发限制防资源耗尽、工具限制防越权、孤儿清理防泄漏。

**下一章**：Provider 层——怎么对接 49 个 LLM provider。
