# 第 5 章：分层沙箱 —— OS 级安全隔离

> **本章目标**：讲透 OpenSquilla 的分层沙箱系统。读完本章，你会理解 L0-L3 四级安全、Linux(bwrap)/macOS(seatbelt)/Windows(WFP) 三平台 OS 级隔离、治理批准门怎么工作。
>
> 这是 OpenSquilla 比 DeerFlow 复杂得多的子系统——DeerFlow 用 Docker 容器隔离，OpenSquilla 用**操作系统原生隔离机制**。

---

## 5.1 为什么用 OS 级隔离而非 Docker？（设计动机）

| 方案 | 隔离强度 | 启动开销 | 依赖 | 适用场景 |
|------|----------|----------|------|----------|
| Docker 容器 | 强 | 秒级 | Docker daemon | 服务器 |
| **OS 原生隔离** | 强 | 毫秒级 | 系统工具 | **桌面/本地** |

OpenSquilla 的核心场景是**桌面/本地**（CLI/Desktop app）。要求用户装 Docker 不现实。所以它用操作系统自带的隔离机制：
- **Linux**：`bwrap`（bubblewrap，命名空间隔离，Flatpak 同款）
- **macOS**：`sandbox-exec`（Seatbelt SBPL profile，Apple 原生）
- **Windows**：WFP 防火墙 + ACL + 身份隔离

这些工具**系统自带或轻量安装**，启动开销毫秒级。

---

## 5.2 四级安全（SecurityLevel）

```python
# 引用位置：src/opensquilla/sandbox/types.py:28-57
class SecurityLevel(IntEnum):
    # Integer ordering is load-bearing: callers can write level >= STRICT
    DISABLED = 0   # L0-disabled：遗留模式，需 allow_legacy_mode
    STANDARD = 1   # L1-standard：默认，workspace-rw, tmp, network=none
    STRICT = 2     # L2-strict：只读根目录，workspace可写，更紧限制
    LOCKED = 3     # L3-locked：默认拒绝，最低可见性，需批准

    _LABELS = {
        SecurityLevel.DISABLED: "L0-disabled",
        SecurityLevel.STANDARD: "L1-standard",
        SecurityLevel.STRICT: "L2-strict",
        SecurityLevel.LOCKED: "L3-locked",
    }
```

**► 注解**：
- **整数有序**（`IntEnum`）：可以写 `level >= STRICT` 判断"是否至少 STRICT 级别"。这是注释说的"load-bearing"——顺序是有意义的。
- **L0（DISABLED）**：沙箱关闭，仅遗留模式。需要显式 `allow_legacy_mode`。
- **L1（STANDARD）**：**默认级别**。workspace 可读写，短暂 tmp，**无网络**。
- **L2（STRICT）**：高风险操作。根目录只读，只有 workspace 可写，限制更紧。
- **L3（LOCKED）**：不受信任/注入暴露的路径。**默认拒绝**，最低可见性，需要批准。

### 网络模式

```python
# 引用位置：src/opensquilla/sandbox/types.py:61
class NetworkMode(StrEnum):
    NONE              # 无网络
    PROXY_ALLOWLIST   # 通过代理白名单
    HOST              # 完全主机网络
```

---

## 5.3 级别选择（policy.py）

系统怎么决定一个操作用哪个级别？

```python
# 引用位置：src/opensquilla/sandbox/policy.py:84
def select_level(action_kind: str, hints: LevelHints) -> SecurityLevel:
    """基于 LevelHints 的确定性规则表"""
```

### LevelHints —— 上下文提示

```python
# 引用位置：src/opensquilla/sandbox/policy.py:62
@dataclass
class LevelHints:
    trusted_source: bool          # 可信来源？
    needs_network: bool           # 需要网络？
    writes_outside_workspace: bool # 写workspace外？
    crosses_trust_boundary: bool   # 跨信任边界？
    high_impact: bool              # 高影响？
```

### 动作类型 → 级别映射

```
fs.read/list/grep    → L1（读操作，低风险）
fs.write/edit        → L1 或 L2（取决于 hints）
patch.apply          → L1 或 L2
code.exec            → L2（代码执行，较高风险）
shell.exec           → L2 或 L3（命令执行）
shell.background      → L2 或 L3
git.read             → L1
git.write            → L2
network.fetch/http   → L1（配合 NetworkMode）
```

**规则倾向升级**（`policy.py:84-130`）——有 `high_impact`、`crosses_trust_boundary` 等 hint 时自动升级到更高级别。

---

## 5.4 平台后端（三平台 OS 隔离）

```python
# 引用位置：src/opensquilla/sandbox/backend/__init__.py:81
def _auto_backend():
    # Linux  → BubblewrapBackend (backend/bubblewrap.py)
    # macOS  → SeatbeltBackend (backend/seatbelt.py)
    # Windows → WindowsDefaultBackend (backend/windows_default.py)
    # 无后端 → NoopBackend (backend/noop.py, 仅 rlimit)
```

### Linux：BubblewrapBackend

使用 `bwrap`（bubblewrap）进行**命名空间隔离**：
- mount namespace：文件系统隔离
- network namespace：网络隔离
- 辅助：`linux_seccomp.py`（系统调用过滤）、proxy bridge、protected-create

**bwrap 是 Flatpak 的底层技术**——被广泛验证的 Linux 沙箱方案。

### macOS：SeatbeltBackend

使用 `sandbox-exec` 的 **SBPL（Seatbelt Policy Language）profile**：
- Apple 原生沙箱机制
- 声明式策略（允许/拒绝规则）
- 不需要额外安装（macOS 自带）

### Windows：WindowsDefaultBackend

最复杂——组合多种 Windows 原生机制：
- **WFP 防火墙**（`windows_default_wfp.py`）：Windows Filtering Platform 网络过滤
- **ACL**（`windows_default_acl.py`）：访问控制列表文件权限
- **身份隔离**（`windows_default_identity.py`）：进程身份

### NoopBackend（沙箱关闭时）

当 `sandbox=true` 但没有后端可用时，**硬失败**（`backend/__init__.py:131`）——不静默降级到无沙箱。如果 `sandbox=false`，用 `NoopBackend`——只有 `rlimit`（CPU/内存限制）保护。

---

## 5.5 操作运行时（operation_runtime.py）

这是工具和沙箱之间的桥梁：

```python
# 引用位置：src/opensquilla/sandbox/operation_runtime.py
# SandboxOperationRuntime — 管理工具的操作保护
# prepare_tool_operation_guard — 准备操作保护
# run_tool_handler_with_operation_guard — 带保护运行handler
# record_tool_operation_success — 记录成功
```

**`@tool` 装饰器在 `sandbox.enforce=True` 时自动接入**（`tools/registry.py:514-524`）——每次工具调用都通过操作保护运行。

### 操作域

```
FilesystemOperationRequest   — 文件系统操作
ProcessOperationRequest      — 进程/命令操作
NetworkOperationRequest      — 网络操作
ArtifactOperationRequest     — 产物操作
MediaOperationRequest        — 媒体操作
CustomOperationRequest       — 自定义操作
```

---

## 5.6 治理与批准（governance.py）

L3（LOCKED）级别需要**用户批准**：

```python
# 引用位置：src/opensquilla/sandbox/governance.py
# ApprovalGate — 批准门
# DenialLedger — 拒绝账本
# gate_execution — 门控执行
# action_fingerprint — 动作指纹（去重）
# post_denial_guard — 拒绝后保护
# on_successful_exec — 成功后记录
```

**批准流程**：
1. 工具操作触发 L3 门控。
2. `ApprovalGate` 检查是否已有授权（once/per-session）。
3. 没有 → 通过 `/api/approvals` 端点请求用户批准。
4. 用户批准 → 执行；拒绝 → 记入 `DenialLedger`。

### 用户授权持久化

```python
# 引用位置：src/opensquilla/sandbox/user_grants.py
# 持久化的用户授权（once/per-session）
```

用户可以授权"这个操作以后不用再问"（once）或"这个会话内不用问"（per-session）。

---

## 5.7 其他安全组件

```
sandbox/escalation.py        — 级别升级逻辑（32KB，复杂的升级决策）
sandbox/run_context.py       — 会话绑定的运行上下文（挂载、授权）
sandbox/network_proxy.py     — 管理的出站代理（PROXY_ALLOWLIST）
sandbox/network_guard.py     — 网络守卫
sandbox/sensitive_paths.py   — 敏感路径检测
sandbox/path_validation.py   — 路径校验（防穿越）
sandbox/domain_validation.py — 域名校验
sandbox/destructive_intents.py — 破坏性意图检测（rm -rf 等）
sandbox/run_mode.py          — 运行模式（FULL/TRUSTED/STANDARD）
```

---

## 5.8 与 DeerFlow 沙箱的对比

| 维度 | DeerFlow | OpenSquilla |
|------|----------|-------------|
| **隔离机制** | Docker 容器 | OS 原生（bwrap/seatbelt/WFP） |
| **安全分级** | 无（要么 Docker 要么 Local） | **4 级**（L0-L3） |
| **网络控制** | 无精细控制 | **3 模式**（NONE/PROXY/HOST） |
| **批准机制** | 无 | **ApprovalGate** + 用户授权持久化 |
| **启动开销** | 秒级（容器启动） | **毫秒级** |
| **适用场景** | 服务器 | **桌面/本地** |
| **依赖** | Docker | 系统自带工具 |

**核心区别**：DeerFlow 的沙箱是"全有或全无"（Docker 或 Local）；OpenSquilla 是**精细分级**的——不同操作用不同安全级别，高风险操作要批准，低风险操作透明执行。

---

## 5.9 本章小结

分层沙箱的核心设计：

1. **四级安全**：L0(DISABLED) → L1(STANDARD) → L2(STRICT) → L3(LOCKED)，整数有序。
2. **三平台 OS 隔离**：Linux bwrap / macOS seatbelt / Windows WFP+ACL。
3. **级别自动选择**：`select_level(action_kind, hints)` 基于 5 个 hint 确定性决策。
4. **治理批准门**：L3 需要用户批准，支持 once/per-session 授权。
5. **网络三模式**：NONE / PROXY_ALLOWLIST / HOST。
6. **操作运行时**：工具通过 `@tool` 装饰器自动接入沙箱保护。
7. **破坏性意图检测**：`destructive_intents.py` 检测 `rm -rf` 等危险操作。

**核心思想**：用操作系统原生隔离机制实现桌面友好的沙箱。四级安全让"低风险透明执行、高风险需批准"——平衡安全和体验。

**下一章**：Subagent 子 Agent——复杂任务怎么委派。
