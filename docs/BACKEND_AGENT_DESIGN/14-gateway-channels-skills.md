# 第 14 章：Gateway + 渠道 + 技能 + 配置 + 安全

> **本章目标**：讲透 OpenSquilla 的 HTTP/WebSocket 网关、三入口（CLI/Web/IM）渠道接入、技能系统（含 Meta-skill）、配置热重载、安全机制。这是覆盖面最广的一章。

---

## 14.1 Gateway（Starlette 网关）

### 框架选型：Starlette（非 FastAPI）

```python
# 引用位置：src/opensquilla/gateway/app.py:1, 11-17
# 使用 Starlette ASGI（from starlette.applications import Starlette）
# 依赖：starlette>=0.40, uvicorn[standard]>=0.30
```

**► 为什么 Starlette 而非 FastAPI？** Starlette 是 FastAPI 的底层——更轻量、更少抽象。OpenSquilla 不需要 FastAPI 的 Pydantic 自动文档等功能，用 Starlette 更精简。

### 应用工厂

```python
# 引用位置：src/opensquilla/gateway/app.py:43-66, 700
def create_gateway_app():
    # ...
    return Starlette(routes=routes, middleware=middleware, debug=config.debug)
```

### 启动

```python
# 引用位置：src/opensquilla/gateway/boot.py:2855, 3719-3744
def start_gateway_server():
    # uvicorn.Config(...)
    # server = uvicorn.Server(uv_config)
    # setattr(server, "install_signal_handlers", lambda: None)  # 抑制信号处理
    # task = create_background_task(server.serve())
```

**► 注解**：`install_signal_handlers = lambda: None`——**抑制 uvicorn 默认信号处理**，把优雅退出交给 `GatewayServer.close()`。注释说这是"跨平台 drain 的关键"（Windows 无真 SIGTERM）。

### 路由结构

```python
# 引用位置：src/opensquilla/gateway/app.py:632-657
# 健康/就绪：/, /health, /healthz, /ready, /readyz
# 只读 API：GET /api/config, /api/sessions, /api/chat/history, /api/agents, /api/cron, ...
# 写 API：POST /api/chat, /api/system/shutdown, /api/approvals/resolve, ...
# WebSocket：WebSocketRoute("/ws", ws_endpoint)
# 渠道 webhook：Slack/Feishu/Telegram 通过 extra_routes 注入
# Control UI：create_control_ui_routes
```

**► 关键设计**：HTTP 处理器**不直接处理业务**，而是**转发成 RPC**（如 `dispatcher.dispatch("_http", "chat.send", body, ctx)`）。所有业务逻辑在 RPC 层，HTTP 只是另一个 transport。

### 中间件链

```python
# 引用位置：src/opensquilla/gateway/app.py:668-698
# ErrorHandlingMiddleware → CORSMiddleware → RateLimitMiddleware → SecurityHeadersMiddleware → AuthMiddleware
```

---

## 14.2 WebSocket 协议（有状态握手）

WebSocket 不是简单 echo，而是**有状态的握手协议**：

```python
# 引用位置：src/opensquilla/gateway/websocket.py:526-726
def handle_ws_connection():
    # 1. 发 connect.challenge（带 nonce）
    # 2. pre-auth 超时（10秒）
    # 3. resolve_auth 鉴权
    # 4. 协议版本协商（PROTOCOL_VERSION = 3）
    # 5. 发 HelloOk（含 features/snapshot/policy/auth）
    # 6. 启动独立 writer 协程（conn._start_writer）
    # 7. 进 _message_loop
```

### 独立 writer 协程

```python
# 引用位置：src/opensquilla/gateway/websocket.py:673
conn._start_writer(...)
# post-auth 所有发送走 conn._outbox 队列
# WS 帧序号在 dequeue 时分配
# 丢弃的 lossy 帧不消耗 seq
```

**► 设计动机**：独立的 writer 协程解耦了"生产事件"和"发送 WS 帧"。生产者只管入队，writer 负责发送。慢客户端不会阻塞生产者。

### 协议帧

```python
# 引用位置：src/opensquilla/gateway/protocol.py
ReqFrame(:33), ResFrame(:56), EventFrame(:71，带 seq/state_version), HelloOk(:151)
MAX_PAYLOAD_BYTES = 26_214_400  # 25 MiB
```

---

## 14.3 RPC 分发

```python
# 引用位置：src/opensquilla/gateway/rpc/registry.py:181
class RpcRegistry:  # 别名 RpcDispatcher
    # register() — 注册 RPC 方法
    # dispatch() — 分发调用（含 scope 鉴权）
    # lock_registration() — boot 后锁定（防运行时漂移）
```

**24 个 rpc_ 子模块**（`rpc/__init__.py:53-77`）：rpc_chat、rpc_config、rpc_sessions、rpc_skills、rpc_memory 等。所有业务逻辑在这里。

---

## 14.4 渠道（三入口共用 turn loop）

### 支持的渠道

```python
# 引用位置：src/opensquilla/channels/contract.py:25-34
PUBLIC_VENDOR_ADAPTERS = (telegram, slack, discord, feishu, dingtalk, wecom, qq, matrix, terminal, websocket)
# msteams 被故意隐藏（registry.py:28 _HIDDEN）
```

### "三入口共用 turn loop"的实现

```python
# 引用位置：src/opensquilla/channels/manager.py:83-132
class ChannelManager:
    def from_config(cls, ..., turn_runner, session_manager, event_bridge, task_runtime):
        # 接收同一个 turn_runner（和其他单例）
        # 所有渠道共享这些单例
```

**► 核心设计**：ChannelManager **不自己跑 agent**，而是把消息交给注入的**同一个 turn_runner**。不管消息来自 CLI、Web 还是 Telegram，底层跑同一个 TurnRunner 8 阶段流水线。

### 渠道抽象接口

```python
# 引用位置：src/opensquilla/channels/types.py:74-134
class Channel(Protocol):
    async def receive() -> IncomingMessage
    async def send(OutgoingMessage)

class ManagedChannel(Channel):
    async def start() / stop() / health_check() -> ChannelHealth
```

### 能力声明

```python
# 引用位置：src/opensquilla/channels/contract.py:174-268
class ChannelCapabilityProfile:
    # 约 30 个 bool 标志：group_chat, streaming, webhook, threads, cards, media...
```

每个渠道声明自己的能力——系统根据能力调整行为（如不支持 streaming 的渠道用 `runs.wait` 而非 `runs.stream`）。

---

## 14.5 CLI 入口（cli/，42 py）

CLI 是三个入口之一。核心命令：
- `agent_cmd.py` — agent 相关命令
- `chat_cmd.py` — 聊天 REPL
- `gateway_cmd.py` — gateway 管理（启动/停止/重载）
- `channels_cmd.py` — 渠道管理
- `agents_cmd.py` — agent 配置管理

CLI 的消息最终也汇入同一个 TurnRunner——通过 `start_turn_via_runtime`（`engine/start_turn.py:87`）。

---

## 14.6 技能系统（skills/，53 py）

### 技能定义：SKILL.md

```python
# 引用位置：src/opensquilla/skills/types.py:77-135
@dataclass
class SkillSpec:
    name: str
    description: str
    layer: SkillLayer       # 6 层优先级
    always: bool             # 是否总是注入
    triggers: list[str]      # 触发词
    content: str             # 技能正文
    kind: str = "skill"      # 默认 "skill"，meta-skill 用别的
    # ...
```

### 6 层优先级（低→高覆盖同名）

```python
# 引用位置：src/opensquilla/skills/types.py:11-19
class SkillLayer:
    EXTRA < BUNDLED < MANAGED < PERSONAL < PROJECT < WORKSPACE
```

高层覆盖低层同名技能——用户可以用 WORKSPACE 层的技能覆盖 BUNDLED 层的。

### 技能注入

```python
# 引用位置：src/opensquilla/skills/injector.py:82
class SkillInjector:
    def inject_skills(...):
        # 渲染成 <skill kind="..."><name>... XML 块
        # 拼到 system_prompt 尾部
        # token 预算自适应：full → compact → meta_priority
```

---

## 14.7 Meta-skill（多步工作流）

### 概念

```python
# 引用位置：docs/features/meta-skills.md:1-21
# Meta-skill = 把"需要多个普通 skill/工具/检查/综合"的可复用多步工作流打包
# 普通 skill = 单一任务模式
# Meta-skill = 多步 DAG
```

### 4 个内置 meta-skill

```python
# 引用位置：docs/features/meta-skills.md:26-38
# meta-kid-project-planner, meta-paper-write, meta-short-drama, meta-skill-creator
```

### MetaOrchestrator

```python
# 引用位置：src/opensquilla/skills/meta/orchestrator.py:1
# "run a MetaPlan as a fleet of one-shot sub-Agents"
```

Meta-skill 把每个步骤作为**一次性子 Agent**执行——和第 6 章的 Subagent 系统配合。

---

## 14.8 配置系统（热重载）

### GatewayConfig（Pydantic BaseSettings）

```python
# 引用位置：src/opensquilla/gateway/config.py:1979
class GatewayConfig(BaseSettings):
    # 嵌套配置类：AuthConfig, CorsConfig, SkillsConfig, ToolsConfig, PermissionsConfig, ...
    # env_prefix = "OPENSQUILLA_*"（环境变量覆盖 TOML）
```

### 热重载（RPC）

```python
# 引用位置：src/opensquilla/gateway/rpc_config.py
# config.set（:652）— 单点设值
# config.patch（:730）— 批量补丁
# config.apply（:862）— 整体应用
# config.reload（:944）— 从磁盘重读
```

**热重载流程**（`config.set`）：
1. 读路径 → 还原脱敏密钥 → 写入 dict
2. **重新构造 GatewayConfig 整体校验**
3. **先落盘**（失败则内存不动）
4. **热替换 live 对象**
5. 同步 provider selector / model catalog

### 重启态判定

某些改动需要重启才生效（通过指纹判定）：memory/channel/sandbox 改动 → `restart_required` 标志。

---

## 14.9 安全（safety/，6 py）

### 五个安全模块

| 模块 | 作用 |
|------|------|
| `safety/injection_guard.py` | **注入防护**——`wrap_untrusted()` 包裹工具输出/web fetch；`classify_injection()` 分类注入威胁 |
| `safety/tool_tiers.py` | **工具风险分级**——SAFE/CONFIRM/ADMIN_ONLY 三级 |
| `safety/permission_matrix.py` | **渠道→工具权限矩阵**——webui 全允许，DM/group 只允许 SAFE+CONFIRM |
| `safety/sandbox.py` | **子进程沙箱**——POSIX setrlimit（CPU/内存限制） |
| `safety/secret_redaction.py` | **密钥脱敏**——多轮正则匹配 sk-/Bearer/password 等 |

### 注入防护（最重要）

```python
# 引用位置：src/opensquilla/safety/injection_guard.py:5-9
def wrap_untrusted(source, content):
    # 用 <untrusted source='...'>...</untrusted> 包裹
    # 内部 XML 转义
```

```python
# 引用位置：src/opensquilla/safety/injection_guard.py:173-191
def classify_injection(text):
    # 基于正则模式库 INJECTION_PATTERNS 分类
    # 引用 Simon Willison taxonomy / GARAK benchmark
```

**► 设计动机**：工具输出、web fetch、渠道入站内容都是**不可信的**——用 `<untrusted>` 信封包裹再进 LLM 上下文，防止间接注入。

---

## 14.10 Identity + Search + Chat（融入本章）

### Identity 身份（identity/，6 py）

```python
# 引用位置：src/opensquilla/identity/
# prompt 身份、workspace 解析
# identity/parser.py — 解析身份配置
# identity/prompt.py — 身份 prompt 构建
```

### Search 搜索（search/，13 py）

```python
# 引用位置：src/opensquilla/search/
# Web 搜索抽象：DuckDuckGo/Bocha/Brave/IQS/Tavily/Exa
# search/registry.py — 搜索 provider 注册
# search/canonical.py — 归一化搜索结果
```

### Chat 聊天（chat/，4 py）

```python
# 引用位置：src/opensquilla/chat/
# conversation.py — 会话管理
# history.py — 历史记录
```

---

## 14.11 本章小结

Gateway + 渠道 + 技能的核心设计：

1. **Starlette 网关**：轻量 ASGI，HTTP 转发 RPC。
2. **WebSocket 有状态握手**：challenge 鉴权 + 独立 writer 协程。
3. **三入口共用 turn loop**：CLI/Web/IM 消息都汇入同一个 TurnRunner。
4. **渠道能力声明**：30 个 bool 标志，系统按能力调整行为。
5. **技能 6 层**：EXTRA < BUNDLED < MANAGED < PERSONAL < PROJECT < WORKSPACE。
6. **Meta-skill**：多步 DAG 工作流，每步作为一次性子 Agent。
7. **配置热重载**：RPC config.set/patch/apply，先落盘再热替换。
8. **安全五模块**：注入防护 + 工具分级 + 权限矩阵 + 子进程沙箱 + 密钥脱敏。

---

## 全系列总结（14 章）

| 章 | 主题 | 核心问题 |
|----|------|----------|
| 01 | 架构总览 | 整体架构是什么？ |
| 02 | Turn Loop | 一个 turn 经历什么？ |
| 03 | SquillaRouter | 怎么选最便宜的模型？ |
| 04 | 工具系统 | 怎么调用工具？ |
| 05 | 分层沙箱 | 怎么安全执行？ |
| 06 | Subagent | 怎么委派子任务？ |
| 07 | Provider 层 | 怎么对接 LLM？ |
| 08 | 记忆+会话 | 怎么记住用户？ |
| 09 | MCP | 怎么双向扩展？ |
| 10 | Scheduler+Recovery | 怎么定时+恢复？ |
| 11 | Hooks+Observability | 怎么观测调试？ |
| 12 | Plugins+Contrib | 怎么插件扩展？ |
| 13 | Onboarding+Health+Eval | 怎么配置+监控+评估？ |
| 14 | Gateway+渠道+技能 | 怎么对接入口？ |

读完这十四章，你已经理解了 OpenSquilla 后端的**每一个关键组件**——从微内核引擎到 SquillaRouter 的 ML 路由，从分层沙箱到 49 个 provider，从混合检索记忆到三入口共用 turn loop。
