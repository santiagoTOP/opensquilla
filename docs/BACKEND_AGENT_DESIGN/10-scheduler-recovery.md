# 第 10 章：Scheduler + Recovery —— 定时任务与崩溃恢复

> **本章目标**：讲透 OpenSquilla 的定时任务系统（Scheduler）和 Desktop 恢复系统（Recovery）。

---

## 10.1 Scheduler 定时任务（scheduler/，21 py）

```python
# 引用位置：src/opensquilla/scheduler/__init__.py
"""Scheduler package — cron job management with stagger strategy."""
from .engine import SchedulerEngine
from .parser import CronExpression, parse_cron
```

### 核心组件

| 文件 | 作用 |
|------|------|
| `scheduler/engine.py` | `SchedulerEngine` — 调度引擎 |
| `scheduler/parser.py` | `CronExpression`/`parse_cron` — cron 表达式解析 |
| `scheduler/ops.py` | `SchedulerOps` — 调度操作 |
| `scheduler/jobs.py` | 任务定义 |
| `scheduler/handlers.py` | 任务处理器 |
| `scheduler/heartbeat.py` + `heartbeat_loop.py` + `heartbeat_service.py` | 心跳系统 |
| `scheduler/delivery.py` | 任务投递 |
| `scheduler/dream_handler.py` | dream 任务处理（配合记忆系统） |
| `scheduler/auto_propose_handler.py` | 自动提议处理 |

### cron 表达式 + stagger 策略

```python
# 引用位置：src/opensquilla/scheduler/parser.py
class CronExpression:
    # 标准 cron 表达式解析
```

**stagger 策略**（`__init__.py` 的 docstring 提到）：多个相同 cron 的任务不**同时**触发，而是错开（stagger）执行——防止资源峰值。

### 与 turn loop 的集成

定时任务触发时，`delivery.py` 把任务投递到 TurnRunner——和用户消息走**同一个 turn loop**。这让定时任务和用户交互行为一致。

### dream handler

`scheduler/dream_handler.py` 配合记忆系统的 dream 子系统（第 8 章）——在空闲时段（如凌晨）触发记忆离线整理。

---

## 10.2 Recovery 恢复（recovery/，11 py）

```python
# 引用位置：src/opensquilla/recovery/__init__.py
"""Public RC4 Desktop recovery contracts.

This package stays standard-library-only at import time so Desktop can
inspect recovery state without importing the full OpenSquilla stack."""
```

**► 设计动机**：Recovery 包**只用标准库**——因为恢复时可能 OpenSquilla 已经损坏，不能依赖完整 stack 能 import。这是"最小依赖恢复"设计。

### 核心组件

| 文件 | 作用 |
|------|------|
| `recovery/engine.py` | 恢复引擎 |
| `recovery/atomic.py` | 原子操作 |
| `recovery/transaction.py` | 事务 |
| `recovery/locking.py` | 锁 |
| `recovery/restore.py` | 恢复操作 |
| `recovery/cleanup.py` | 清理 |
| `recovery/config_patch.py` | 配置补丁 |
| `recovery/settings_transaction.py` | 设置事务 |
| `recovery/errors.py` | 错误类型 |
| `recovery/models.py` | 数据模型 |

### 恢复场景

- **Desktop 应用崩溃后**：下次启动检测到异常退出，运行恢复流程。
- **配置损坏**：`config_patch.py` 修复配置。
- **数据库锁死**：`locking.py` 检测并清理 stale lock（检测死 PID）。
- **原子写入失败**：`atomic.py` + `transaction.py` 保证操作的原子性——要么完全成功，要么完全回滚。

### 与持久化的关系

Recovery 和 persistence（第 8 章）配合——持久化的 yoyo 迁移有类似的 stale lock 检测（`migrator.py` 的 `_recover_stale_yoyo_lock`）。Recovery 是更上层的"应用级恢复"。

---

## 10.3 本章小结

Scheduler + Recovery 的核心设计：

1. **Scheduler**：cron 表达式 + stagger 策略 + 心跳。定时任务走同一个 turn loop。
2. **Recovery**：标准库 only（最小依赖恢复）。原子操作 + 事务 + 锁 + 配置补丁。
3. **dream handler**：定时触发记忆离线整理。

**核心思想**：Scheduler 让 Agent 能"自主定时工作"；Recovery 让 Agent"崩溃后能恢复"。两者都是生产级可靠性的基础。

**下一章**：Hooks + Observability。
