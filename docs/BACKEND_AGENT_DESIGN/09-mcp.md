# 第 9 章：MCP —— 标准协议双向扩展

> **本章目标**：讲透 OpenSquilla 的 MCP（Model Context Protocol）集成。OpenSquilla 既是 MCP **客户端**（连外部 server 导入工具）又是 MCP **服务端**（把会话暴露给外部 client）。这两个方向是**完全分离**的。

---

## 9.1 MCP 客户端（出站：连外部 server）

```python
# 引用位置：src/opensquilla/mcp/__init__.py:1
"""MCP client package — connect to external MCP servers and register their tools."""
```

### 核心组件

| 文件 | 作用 |
|------|------|
| `mcp/client.py` | `MCPClient` — MCP 客户端，连接外部 server |
| `mcp/discovery.py` | `discover_and_register` — 发现并注册工具 |
| `mcp/stdio.py` | stdio 传输（命令式启动子进程） |
| `mcp/sse.py` | SSE 传输（Server-Sent Events） |
| `mcp/types.py` | `MCPServerConfig`/`MCPToolDef`/`MCPToolResult` 类型 |

### 工作流

1. 从配置读取 MCP server 列表。
2. `discover_and_register` 连接每个 server。
3. 发现 server 提供的工具。
4. 把工具注册到 OpenSquilla 的 `ToolRegistry`——变成 Agent 可调用的工具。
5. Agent 调用这些工具时，请求转发给对应的 MCP server。

### 支持的传输

- **stdio**：`mcp/stdio.py`——启动子进程（如 `npx -y some-mcp-server`），通过 stdin/stdout 通信。
- **SSE**：`mcp/sse.py`——通过 Server-Sent Events 连接远程 server。

---

## 9.2 MCP 服务端（入站：暴露给外部 client）

```python
# 引用位置：src/opensquilla/mcp_server/__init__.py:1-6
"""Inbound MCP server bridge for OpenSquilla.

This package exposes OpenSquilla sessions to external MCP clients. It is
intentionally separate from opensquilla.mcp, which is the outbound MCP
client integration used to import tools from external servers.
"""
```

### 核心组件

| 文件 | 作用 |
|------|------|
| `mcp_server/bridge.py` | `OpenSquillaMCPBridge` — 桥接 OpenSquilla 会话到 MCP |
| `mcp_server/server.py` | `create_mcp_server` — 创建 MCP server 实例 |

### 工作流

1. 外部 MCP client（如 Claude Desktop）连接 OpenSquilla 的 MCP server。
2. Client 发现 OpenSquilla 暴露的工具/会话。
3. Client 调用工具 → bridge 转发到 OpenSquilla 的 TurnRunner。
4. 结果通过 MCP 协议返回 client。

**► 设计动机**：这让 OpenSquilla 成为**MCP 生态的一部分**——既消费外部工具（客户端），又被外部工具消费（服务端）。双向互操作。

---

## 9.3 与 DeerFlow MCP 的对比

| 维度 | DeerFlow | OpenSquilla |
|------|----------|-------------|
| **客户端** | ✅ MultiServerMCPClient | ✅ MCPClient |
| **服务端** | ❌ 只是客户端 | ✅ **双向**（暴露给外部） |
| **传输** | stdio/SSE/HTTP + OAuth | stdio/SSE |
| **延迟工具** | ✅ tool_search + McpRouting | ❌ 直接注册 |

---

## 9.4 本章小结

MCP 系统的核心设计：**双向**——客户端导入外部工具，服务端暴露 OpenSquilla 会话。两个方向完全分离（`mcp/` vs `mcp_server/`）。

**下一章**：Scheduler 定时任务 + Recovery 恢复。
