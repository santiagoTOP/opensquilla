# 第 13 章：Onboarding + Health + Eval —— 辅助子系统

> **本章目标**：讲透三个辅助但重要的子系统：首次启动引导（Onboarding）、模型健康检查（Health）、模型评估（Eval）。

---

## 13.1 Onboarding 首次启动引导（onboarding/，21 py）

```python
# 引用位置：src/opensquilla/onboarding/__init__.py
"""Shared onboarding/configuration core used by CLI, RPC, and WebUI."""
```

### 设计动机

用户第一次使用 OpenSquilla 时需要配置（选 provider、填 API key、选模型等）。Onboarding 是一个**交互式引导向导**，帮用户完成这些配置。

### 三端共用

```python
# 引用位置：src/opensquilla/onboarding/__init__.py
# "used by CLI, RPC, and WebUI"
```

引导逻辑是**共享核心**——CLI、RPC（Web UI 调用）、WebUI 三个入口用同一套引导代码。

### 核心组件

| 文件 | 作用 |
|------|------|
| `onboarding/wizard.py` | 引导向导（交互式问答） |
| `onboarding/wizard_rpc.py` | 引导的 RPC 接口 |
| `onboarding/audio_specs.py` | 音频规格配置 |
| 其他 18 个文件 | 各种配置步骤的辅助 |

### 与 questionary 的依赖

```python
# 引用位置：pyproject.toml
"questionary>=2.1"  # CLI 交互式选择，依赖 use_search_filter（type-to-filter）
```

CLI 引导用 questionary 库做交互式选择。版本要求 >=2.1 因为需要 `use_search_filter` 功能。

---

## 13.2 Health 模型健康检查（health/，4 py）

```python
# 引用位置：src/opensquilla/health/
```

### 核心组件

| 文件 | 作用 |
|------|------|
| `health/evaluator.py` | `HealthEvaluator` — 健康评估器 |
| `health/model.py` | 健康模型 |
| `health/recovery_commands.py` | 恢复命令 |

### 设计动机

LLM provider 可能**宕机**或**降级**。Health 子系统定期检查 provider 健康：
- **评估**：`HealthEvaluator` 对 provider 发探测请求。
- **恢复**：检测到不健康时，触发恢复（如切换 fallback provider）。
- **恢复命令**：`recovery_commands.py` 提供手动恢复命令。

---

## 13.3 Eval 模型评估（eval/，4 py）

### 核心组件

| 文件 | 作用 |
|------|------|
| `eval/ensemble_benchmark.py` | 集成基准测试 |
| `eval/scenarios.py` | 评估场景 |
| `eval/synthetic.py` | 合成测试数据 |

### 设计动机

SquillaRouter 的自学习闭环（第 3 章）需要**评估模型质量**——新训练的路由模型是否比旧的更好？Eval 子系统提供基准测试工具：
- **`scenarios.py`**：定义评估场景（不同难度的任务）。
- **`ensemble_benchmark.py`**：基准测试——对比不同路由策略的效果。
- **`synthetic.py`**：生成合成测试数据。

这就是 README 提到的 "multi-model ensemble routing surpasses Fable 5" 的**验证工具**。

---

## 13.4 本章小结

三个辅助子系统的核心设计：

1. **Onboarding**：三端共用的交互式引导向导。questionary 驱动。
2. **Health**：provider 健康评估 + 自动/手动恢复。
3. **Eval**：路由模型评估基准——验证自学习改进效果。

**核心思想**：这三个子系统分别覆盖 Agent 生命周期的三个阶段——**开始**（Onboarding 配置）、**运行**（Health 监控）、**改进**（Eval 评估）。

**下一章**：Gateway + 渠道 + 技能 + 配置 + 安全。
