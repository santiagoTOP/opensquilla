# 第 4 章：工具系统 —— Agent 怎么调用工具

> **本章目标**：讲透 OpenSquilla 的工具系统。读完本章，你会理解工具怎么定义（`@tool` 装饰器 + ToolSpec）、怎么分发（10 步流水线）、有哪些内置工具、策略链怎么安全地执行。

---

## 4.1 工具定义：ToolSpec + @tool 装饰器

### ToolSpec 数据类

每个工具的"规格说明"：

```python
# 引用位置：src/opensquilla/tools/types.py:170-183
@dataclass
class ToolSpec:
    name: str                    # 工具名（如 "exec_command"）
    description: str             # 给 LLM 看的描述
    parameters: dict[str, Any]   # JSON Schema 参数定义
    required: list[str]          # 必填参数
    owner_only: bool = False     # 是否仅 operator 可用
    exposed_by_default: bool = True  # 是否默认暴露给 LLM
    execution_timeout_seconds: float | None = None  # 执行超时
    execution_timeout_argument: str | None = None   # 参数级超时
    execution_timeout_padding: float = 0.0           # 超时padding
    result_budget_class: str | None = None           # 结果预算类
    sandbox: SandboxToolDescriptor = ...  # 沙箱描述符（第5章详讲）
```

**► 字段注解**：
- **`sandbox: SandboxToolDescriptor`**：这个字段把工具和沙箱系统关联——每个工具声明自己需要的沙箱级别（第 5 章）。
- **`owner_only`**：有些工具只有 operator 角色能用（如 `router_control` 改路由配置）。
- **`result_budget_class`**：结果预算分类——控制工具输出的大小（防撑爆上下文）。

### @tool 装饰器

```python
# 引用位置：src/opensquilla/tools/registry.py:449
def tool(...):
    # 1. 用 ToolSpec 包装异步函数
    # 2. 在默认（或自定义）ToolRegistry 中注册
    # 3. 可选：包装在沙箱操作保护中（registry.py:491-524）
```

**用法示例**（shell 工具）：
```python
@tool(
    name="exec_command",
    description="Execute a shell command",
    parameters={...},
    sandbox=SandboxToolDescriptor(action_kind="shell.exec", enforce=True),
)
async def exec_command(command: str, ...) -> str:
    ...
```

**► 注解**：`@tool` 装饰器做三件事：(1) 创建 ToolSpec；(2) 注册到 ToolRegistry；(3) 如果 `spec.sandbox.enforce=True`，把 handler 包装在沙箱操作保护里——每次调用都过沙箱门控。

### ToolContext —— 请求级上下文

```python
# 引用位置：src/opensquilla/tools/types.py:32
@dataclass
class ToolContext:
    # 非常丰富的请求级上下文：
    workspace_dir: str           # 工作区目录
    caller_kind: str             # 调用者类型
    interaction_mode: str        # 交互模式
    sandbox_mounts: ...          # 沙箱挂载
    allowed_tools: set[str]      # 允许的工具集
    denied_tools: set[str]       # 禁止的工具集
    # ... 更多
```

通过 `current_tool_context` ContextVar（`types.py:111`）流动——工具执行时可以读取当前上下文。

---

## 4.2 10 步分发流水线（build_tool_handler）

这是工具系统最核心的部分。当 Agent 的状态机循环执行一个 tool_call 时，它经过 **10 步流水线**：

```python
# 引用位置：src/opensquilla/tools/dispatch.py:993-1018
def build_tool_handler(registry, ctx, *, known_skill_names=None, tool_hooks=None) -> AgentToolHandler:
    """Build an async tool handler from a ToolRegistry.

    The returned handler:
    1. Injection-guard check before registry lookup.
    2. Registry lookup; returns structured error on miss.
    3. ToolHook.before_tool fan-out (no-op if tool_hooks is empty).
    4. Policy chain; first denial returns immediately.
    5. Reserves run budget, including external call counts and text caps.
    6. Dispatches to the registered handler inside the request-scoped contextvar.
    7. Commits or aborts the run-budget reservation.
    8. ToolHook.after_tool fan-out with the raw outcome.
    9. Finalises the result (execution status, budget, artefacts) via finalize.
    10. Resets current_tool_context unconditionally in finally.
    """
```

**► 逐步注解**：

| 步骤 | 操作 | 设计动机 |
|------|------|----------|
| **1. 注入保护** | `_check_injection_guard`（dispatch.py:1063）| 检查工具调用是否来自不可信内容（注入防护） |
| **2. 注册表查找** | 按 name 查找工具 | 找不到 → 结构化错误（不崩溃） |
| **3. 参数清洗** | 嵌套 JSON 解包、别名规范化、schema 检查 | LLM 可能给出格式错误的参数 |
| **4. ToolHook.before_tool** | 扇出钩子 | 生命周期钩子（第 11 章） |
| **5. 策略链** | `run_chain_with_emit`，first-denial-wins | 安全策略（写策略/权限矩阵） |
| **6. 预算预留** | 外部调用计数 + 文本上限 | 防资源耗尽 |
| **7. 分发执行** | `current_tool_context` 内调注册的 handler | 在正确的上下文里执行 |
| **8. 预算提交/回滚** | 成功提交，失败回滚 | 原子预算管理 |
| **9. ToolHook.after_tool** | 扇出钩子（带结果） | 生命周期钩子 |
| **10. 重置 ContextVar** | `finally` 无条件重置 | 防泄漏 |

### 策略链（policy chain）

步骤 5 的"策略链"是安全的核心：

```python
# 引用位置：src/opensquilla/tools/policy_config.py, policy_runtime.py, write_policy.py
# 策略模块：
#   - policy_config.py — 策略配置
#   - policy_runtime.py — 策略运行时（run_chain_with_emit）
#   - write_policy.py — 写策略（写文件/编辑的权限控制）
#   - write_tracking.py — 写追踪
#   - policy_helpers.py — 策略辅助
```

**策略链是"first-denial-wins"**——多个策略按顺序评估，第一个拒绝的就立即返回，不执行工具。这保证安全策略是**保守的**（宁可拒绝也不执行不安全操作）。

---

## 4.3 内置工具

内置工具通过 import 副作用注册（`tools/builtin/__init__.py:35`）：

```python
# 引用位置：src/opensquilla/tools/builtin/__init__.py:35
# 注册的内置工具模块：
admin, agents, artifacts, code_exec, feishu_platform, filesystem,
git, media, messaging, meta_tools, nodes, patch, router_control,
sessions, session_search, shell, tool_results, web, web_fetch
```

**`shell`、`patch`、`filesystem` 是致命的**——导入失败会引发异常（`builtin/__init__.py:9`），因为它们是核心功能。

### 主要内置工具

| 工具 | 文件 | 作用 |
|------|------|------|
| **exec_command** | `shell.py:4204` | 执行 shell 命令（沙箱保护） |
| **background_process** | `shell.py` | 后台进程管理 |
| **read/write/edit_file** | `filesystem.py` | 文件读写编辑 |
| **apply_patch** | `patch.py` | 应用 diff 补丁 |
| **execute_code** | `code_exec.py` | 代码执行（沙箱内） |
| **web_search** | `web.py` | Web 搜索（第 14 章的 Search 子系统） |
| **web_fetch** | `web_fetch.py` | 抓取网页 |
| **git** | `git.py` | Git 操作 |
| **memory_get/search** | `memory_tools.py` | 记忆检索（在 SUBAGENT_TOOL_DENY 中） |
| **router_control** | `router_control.py` | 路由控制（钉住 tier/model） |
| **messaging** | `messaging.py` | 消息发送（渠道间） |

### shell 工具的沙箱集成

```python
# 引用位置：src/opensquilla/tools/builtin/shell.py:4245（exec_command handler）
# 当 runtime.effective.sandbox_enabled 时：
#   1. gate_action(action_kind="shell.exec", argv=..., cwd=..., env=..., hints=...)
#   2. 如果批准 → 构建 SandboxRequest → run_under_backend 执行
#   3. 如果拒绝 → 返回序列化的 DenialResult
```

**► 注解**：shell 工具是"沙箱感知"的——如果沙箱开启，命令在沙箱后端（bwrap/seatbelt/WFP）里执行；如果关闭，直接在宿主执行（但仍有 rlimit 保护）。

---

## 4.4 工具并发执行（Agent 侧）

第 2 章讲了 Agent 状态机循环的工具执行阶段。这里补充并发策略：

```python
# 引用位置：src/opensquilla/engine/agent.py:7167-7528（工具执行阶段）
# 并发策略：
#   - 互斥（mutex）工具串行运行
#   - 其他工具并行批次，每 key 锁 + 全局信号量
#   - _max_safe_tool_concurrency() 默认 6（types.py:470）
```

**并发设计**：
- **互斥工具串行**：标记为 mutex 的工具（如文件写入）不能并发。
- **其他工具并行**：独立工具（如多个 web_search）并行，加速。
- **每 key 锁**：同一资源的操作串行化。
- **全局信号量**：限制总并发数（默认 6），防资源耗尽。

### 失败循环保护

```python
# 引用位置：src/opensquilla/engine/agent.py:11672（_execute_tool）
# 对重复的相同失败调用进行指纹识别（agent.py:11679-11701）
# 防止"同一工具同一参数反复失败"的死循环
```

---

## 4.5 工具可见性（visibility.py）

```python
# 引用位置：src/opensquilla/tools/visibility.py
# 按 profile + caller_kind 过滤工具可见性
```

不同场景（CLI vs Web vs IM 渠道）能看到不同工具集。结合第 14 章的权限矩阵（`safety/permission_matrix.py`），实现"渠道粒度的工具权限"。

---

## 4.6 本章小结

工具系统的核心设计：

1. **ToolSpec + @tool 装饰器**：声明式工具定义，自动注册 + 沙箱包装。
2. **10 步分发流水线**：注入保护 → 查找 → 参数清洗 → 钩子 → 策略链 → 预算 → 执行 → 钩子 → 定稿 → 清理。
3. **策略链 first-denial-wins**：保守安全，第一个拒绝即返回。
4. **工具并发**：互斥串行 + 其他并行（最多 6），每 key 锁。
5. **沙箱集成**：`sandbox.enforce=True` 的工具自动包装操作保护。
6. **失败循环保护**：指纹识别重复失败，防死循环。

**与 DeerFlow 的对比**：DeerFlow 用中间件（GuardrailMiddleware、SandboxAuditMiddleware 等）保护工具调用；OpenSquilla 用**10 步流水线 + 策略链**——更线性、更明确（每步做什么一目了然），但灵活性略低（不像中间件那样可以自由组合 hook）。

**下一章**：分层沙箱——L0-L3 四级安全、bwrap/seatbelt/WFP 三平台 OS 级隔离。
