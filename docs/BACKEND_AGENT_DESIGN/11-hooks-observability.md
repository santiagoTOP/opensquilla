# 第 11 章：Hooks + Observability —— 生命周期钩子与可观测性

> **本章目标**：讲透 OpenSquilla 的生命周期 hook 协议和可观测性系统。

---

## 11.1 Hooks 生命周期钩子（engine/hooks/）

### 设计哲学

```python
# 引用位置：src/opensquilla/engine/hooks/types.py:1-11
"""Hook protocol definitions for Agent + TurnRunner lifecycle.

These Protocols are structural — implementations do not need to inherit. A
concrete hook just needs to provide the named methods. Default no-op hooks live
in opensquilla.engine.hooks.defaults.

Value objects passed to hooks are intentionally narrow and frozen so a hook
cannot accidentally mutate caller state. Mutation paths must be explicit
(returning a value or going through a side-effect-bearing hook method).
"""
```

**► 与 DeerFlow 中间件的关键区别**：
- DeerFlow 中间件可以**修改 state**（返回 dict/Command）。
- OpenSquilla hooks **不能修改 caller state**——传给 hook 的值对象是 **frozen（不可变）** 的。如果需要修改，必须通过显式的返回值或 side-effect 方法。

这个设计更**保守**——hook 是"观察者"而非"修改者"，减少副作用。

### Turn 生命周期 hook

```python
# 引用位置：src/opensquilla/engine/hooks/types.py
@dataclass(frozen=True)
class TurnHookContext:
    """Per-turn context handed to every TurnHook method."""
    # trace_context, extra metadata
```

### Tool 生命周期 hook

工具的 `before_tool`/`after_tool` 扇出（第 4 章工具分发流水线的步骤 4 和 8）。

### 默认 no-op hook

```python
# 引用位置：src/opensquilla/engine/hooks/defaults.py
# 默认 no-op 实现——不传 hook 时行为不变
```

---

## 11.2 Observability 可观测性（observability/，14 py）

### 核心组件

| 文件 | 作用 |
|------|------|
| `observability/decision_log.py` | **决策日志**——记录路由决策、pipeline 步骤 |
| `observability/decision_log_aggregate.py` | 决策日志聚合 |
| `observability/trace.py` | **Trace**——分布式追踪 |
| `observability/replay.py` | **Replay**——回放 turn |
| `observability/prompt_report.py` | **提示词报告**——分析 system prompt 构成 |
| `observability/redact.py` | **脱敏**——日志/trace 中的密钥脱敏 |
| `observability/safety_log.py` | **安全日志**——安全事件 |
| `observability/turn_call_log.py` | **Turn 调用日志**——raw prompt/tool call 捕获 |
| `observability/bundle.py` | 诊断 bundle 打包 |
| `observability/cli_logging.py` | CLI 日志 |
| `observability/install_telemetry.py` | 遥测安装 |
| `observability/network_policy.py` | 网络策略 |
| `observability/update_check.py` | 更新检查 |

### Decision Log（最重要的可观测性组件）

```python
# 引用位置：src/opensquilla/observability/decision_log.py
# PipelineStepRecord — 记录每个 pipeline 步骤
# RoutingSource — 路由来源（squilla_router/heuristic/image_route/none）
# DecisionEntry — 最终决策条目
```

**► 注解**：每个 turn 的决策过程（pipeline 每步、路由 tier、置信度）都记录到 decision log。这让操作员能**事后审计**："这个 turn 为什么用了 R2 而非 R1？"——查看 decision log 就知道。

### Prompt Report

```python
# 引用位置：src/opensquilla/observability/prompt_report.py
# 分析 system prompt 的构成——多少 token 来自记忆、技能、平台提示
```

帮助理解"system prompt 为什么这么长"——哪些部分占了多少 token。

### Replay（回放）

```python
# 引用位置：src/opensquilla/observability/replay.py
# 回放 turn——用于调试和测试
```

### Redact（脱敏）

```python
# 引用位置：src/opensquilla/observability/redact.py
# 日志/trace 中的密钥脱敏（配合 safety/secret_redaction.py）
```

**安全设计**：日志可能记录 prompt 内容，里面可能含密钥。redact 在写入日志前清洗。

---

## 11.3 本章小结

Hooks + Observability 的核心设计：

1. **Hooks 是观察者非修改者**：frozen 值对象，不能意外修改 state。
2. **Decision Log**：完整记录每个 turn 的决策过程（pipeline 步骤 + 路由）。
3. **Trace + Replay**：分布式追踪 + turn 回放。
4. **Prompt Report**：分析 system prompt 构成。
5. **Redact**：日志密钥脱敏。

**核心思想**：Hooks 用 frozen 设计保证安全（观察不修改）；Observability 提供完整的决策审计能力——每个 turn 的路由决策都可追溯。

**下一章**：Plugins + Contrib 插件生态。
