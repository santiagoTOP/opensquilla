# 第 7 章：Provider 层 —— 对接 49 个 LLM Provider

> **本章目标**：讲透 OpenSquilla 的 Provider 层。读完本章，你会理解它怎么用一个统一协议对接 49 个 provider ID（背后是 5 个后端实现），故障转移怎么工作，集成（ensemble）怎么实现。

---

## 7.1 统一协议：LLMProvider

所有 provider 实现同一个 Protocol：

```python
# 引用位置：src/opensquilla/provider/protocol.py:160-187
class LLMProvider(Protocol):
    provider_name: str  # 如 "anthropic", "openai"

    def chat(
        self,
        messages,                    # 消息列表
        tools: list[ToolDefinition] | None = None,  # 工具定义
        config: ChatConfig | None = None,            # 聊天配置
    ) -> AsyncIterator[StreamEvent]:  # 流式事件
        """流式聊天。按顺序产出 TextDeltaEvent, ToolUseStart/Delta/End, DoneEvent, ErrorEvent"""

    async def list_models(self) -> list[ModelInfo]:
        """列出可用模型"""
```

**► 注解**：
- **`chat()` 返回 `AsyncIterator[StreamEvent]`**——流式接口。Agent 的状态机循环消费这个流。
- **5 种 StreamEvent**：`TextDeltaEvent`（文本增量）、`ToolUseStartEvent`/`ToolUseDeltaEvent`/`ToolUseEndEvent`（工具调用）、`DoneEvent`（完成）、`ErrorEvent`（错误）。

---

## 7.2 5 个后端实现（支撑 49 个 provider ID）

只有 5 个具体的后端类：

| 后端类 | 文件 | 支撑的 provider 示例 |
|--------|------|---------------------|
| **AnthropicProvider** | `provider/anthropic.py` | anthropic |
| **OpenAIProvider** | `provider/openai.py` | openai, openrouter, deepseek, groq, moonshot, dashscope... |
| **OpenAIResponsesProvider** | `provider/openai_responses.py` | openai_responses |
| **OllamaProvider** | `provider/ollama.py` | ollama |
| **OpenAICodexProvider** | `provider/openai_codex.py` | openai_codex |

**► 为什么 49 个 provider 只有 5 个后端？** 因为大多数 provider 是 **OpenAI-compatible** 的——它们用相同的 HTTP API 格式，只是 `base_url` 和 `api_key` 不同。`OpenAIProvider` 通过配置适配这几十个 provider。

### ProviderSpec —— 每个 provider 的规格

```python
# 引用位置：src/opensquilla/provider/registry.py:64
@dataclass
class ProviderSpec:
    backend: str              # 用哪个后端（如 "openai"）
    provider_kind: str        # provider 类型
    env_key: str              # 环境变量名（API key）
    default_base_url: str     # 默认 base_url
    reasoning_shape: str      # 推理格式（影响流解析）
    failure_family: str       # 故障分类族
    auth_header_style: str    # 认证头风格
    catalog_source: str       # 模型目录来源
```

### 49 个 provider ID

注册在 `_PROVIDER_SPECS` 字典（`registry.py:116`），包括：`openrouter, openai, azure, anthropic, ollama, deepseek, gemini, mistral, groq, moonshot, dashscope, qianfan, zhipu, minimax, volcengine, byteplus, siliconflow, tencent_tokenhub, tokenrhythm, kimi_coding, mimo, bailian, aihubmix, github_copilot, litellm_proxy, vllm, lm_studio, ovms, custom` 等。

---

## 7.3 故障转移（failover）

### ModelSelector

```python
# 引用位置：src/opensquilla/provider/selector.py:289
class ModelSelector:
    # resolve() 返回主 provider
    # build_provider() 构建实例
    # SelectorConfig = primary + fallbacks 列表
```

### 故障转移链

```python
# 引用位置：src/opensquilla/provider/protocol.py:231
def resolve_failover_chain(primary_failure, config, plugin):
    """默认链是配置的 fallbacks；ProviderPlugin.failover_hook 可覆盖"""
```

**工作流**：
1. 主 provider 调用失败。
2. `classify_provider_error`（`failures.py`）分类故障。
3. `decide_recovery_action` 决定恢复动作（重试/切换/放弃）。
4. 沿 fallback 链尝试下一个 provider。

### 故障分类

```python
# 引用位置：src/opensquilla/provider/failures.py
# classify_provider_error — 分类故障
# decide_recovery_action — 决定恢复
# ProviderFailureKind — 故障类型枚举
# ProviderRecoveryAction — 恢复动作枚举
```

---

## 7.4 集成（Ensemble）

OpenSquilla 支持把**多个 provider 组合成一个**：

```python
# 引用位置：src/opensquilla/provider/ensemble.py
# EnsembleProvider / build_ensemble_provider_from_config
# 提议者/投票者融合
# B5 选择模式（runtime.py:5224-5306）
```

**集成模式**：多个 provider 同时处理，通过投票/融合选出最佳结果。README 提到的 "multi-model ensemble routing surpasses Fable 5" 就是这个机制。

---

## 7.5 凭证管理

```python
# 引用位置：src/opensquilla/provider/credentials.py
# Credential — 单个凭证
# CredentialPool — 凭证池（轮转使用多个 API key）
# NoCredentialsAvailable — 无可用凭证异常
```

**凭证池**：支持配置多个 API key 轮转使用——分散限流压力。

---

## 7.6 本章小结

Provider 层的核心设计：

1. **统一协议**：`LLMProvider.chat()` 返回 `AsyncIterator[StreamEvent]`。
2. **5 后端支撑 49 provider**：大多数是 OpenAI-compatible。
3. **故障转移**：分类故障 → 决定动作 → 沿 fallback 链重试。
4. **集成（Ensemble）**：多 provider 投票融合。
5. **凭证池**：多 API key 轮转。

**核心思想**：用一个协议 + 5 个后端覆盖整个 LLM 生态。故障转移保证可用性。集成是多模型超越单模型的基础。

**下一章**：记忆+会话——SQLite+向量+FTS 混合检索。
