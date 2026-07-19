# 第 12 章：Plugins + Contrib —— 插件生态

> **本章目标**：讲透 OpenSquilla 的插件系统和社区贡献工具。

---

## 12.1 Plugins 插件系统（plugins/）

```python
# 引用位置：src/opensquilla/plugins/__init__.py
"""Built-in OpenSquilla plugins."""
```

### 插件注册机制

OpenSquilla 支持通过 Python `entry_points` 注册外部插件：

```python
# 引用位置：src/opensquilla/channels/registry.py:128-139
# entry_points 外部插件发现
# 渠道适配器可以通过 entry_points 注册
```

**► 设计动机**：`entry_points` 是 Python 标准的插件机制——第三方包可以在 `pyproject.toml` 里声明 entry_point，OpenSquilla 启动时自动发现并加载。这让第三方扩展**不需要修改 OpenSquilla 源码**。

### 插件类型

OpenSquilla 的多个子系统支持插件扩展：
- **Channel 插件**：通过 entry_points 注册新渠道适配器。
- **Provider 插件**：`ProviderPlugin` 协议（`provider/protocol.py:202`）——`failover_hook`/`quota_hook`。
- **Tool 插件**：通过 `@tool` 装饰器注册。
- **Skill 插件**：通过技能目录注册。

---

## 12.2 Contrib 社区贡献（contrib/，27 py）

```python
# 引用位置：src/opensquilla/contrib/__init__.py
# 社区贡献的工具/集成
```

**contrib 目录**是"社区贡献区"——存放非核心的、可选的工具和集成。27 个 py 文件包含各种扩展工具。

### 与核心代码的边界

- **核心**（`tools/builtin/`）：经过严格审查、默认启用、导入失败会致命。
- **contrib**：社区贡献、可选、质量参差。

**► 设计动机**：这种"核心 vs contrib"的分层让 OpenSquilla 保持核心精简，同时容纳社区创新。类似于 Linux 内核（核心）vs 发行版包（contrib）的模式。

---

## 12.3 本章小结

Plugins + Contrib 的核心设计：

1. **entry_points 插件发现**：Python 标准机制，第三方包自动注册。
2. **多子系统可扩展**：渠道/Provider/工具/技能都支持插件。
3. **核心 vs contrib 分层**：核心精简严格，contrib 宽松开放。

**核心思想**：微内核架构的精髓——核心极小，所有功能都是插件。entry_points 让第三方扩展零侵入。

**下一章**：Onboarding + Health + Eval。
