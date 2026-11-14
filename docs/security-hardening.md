# 安全加固版使用说明

安全入口是**新增的可选入口**，不覆盖原始 `server.py` / `console/app.py`。MCP 客户端应改为启动 `secure_server.py`；浏览器控制台应使用 `python secure_console.py`。请先在隔离实验环境验证厂商命令与交互流程，再连接生产设备。

## 安全策略

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `NETCLI_COMMAND_POLICY` | `guarded` | `guarded` 允许识别为只读的命令，其他命令须提供 `approval="CONFIRM <session_id>"`；`readonly` 拒绝其他命令；`unrestricted` 关闭命令审批（不建议） |
| `NETCLI_ALLOWED_HOSTS` | 未设置 | 逗号分隔的主机/IP/CIDR 白名单；设置后拒绝不匹配的 Telnet/SSH 目标。例如 `192.168.10.0/24,sw1.lab.example`。主机名只做精确匹配 |
| `NETCLI_ALLOWED_SERIAL_PORTS` | 未设置 | 逗号分隔串口白名单，例如 `COM3,/dev/ttyUSB0` |
| `NETCLI_API_TOKEN` | 未设置 | 设置后所有 `/api/*` 请求必须带 `Authorization: Bearer <token>` 或 `X-NetCLI-Token`。控制台内置浏览器调用也需要带 token；为避免把长期 token 嵌入静态页面，请优先由受信任的本地代理注入，或仅将 API 暴露给受信任进程 |
| `NETCLI_MAX_OUTPUT_CHARS` | `65536` | MCP/API 返回文本的截断上限 |
| `NETCLI_MAX_WAIT_MS` | `120000` | 单命令最大等待时间 |
| `NETCLI_AUDIT_LOG` | 未设置 | JSONL 审计日志路径；建议设为权限受限的本地目录 |

默认 `guarded` 是保守启发式策略，不是设备端授权系统。命令确认字符串用于降低误操作，不能替代 MCP 客户端的人类审批、设备 RBAC/AAA 或变更流程。需要强制只读时设 `NETCLI_COMMAND_POLICY=readonly`，并在设备侧使用只读账号。

## 启动

```json
{
  "mcpServers": {
    "net-cli-mcp": {
      "command": "python",
      "args": ["/绝对路径/AI-net-cli-mcp/secure_server.py"]
    }
  }
}
```

控制台启动：

```bash
python secure_console.py
```

Linux/macOS 示例：

```bash
export NETCLI_COMMAND_POLICY=readonly
export NETCLI_ALLOWED_HOSTS="192.168.10.0/24,127.0.0.1"
export NETCLI_AUDIT_LOG="$HOME/.local/state/net-cli-mcp/audit.jsonl"
python secure_console.py
```

Windows PowerShell 示例：

```powershell
$env:NETCLI_COMMAND_POLICY = "readonly"
$env:NETCLI_ALLOWED_HOSTS = "192.168.10.0/24"
$env:NETCLI_AUDIT_LOG = "$env:LOCALAPPDATA\net-cli-mcp\audit.jsonl"
python secure_console.py
```

## 当前边界与注意事项

- 原始 `server.py` 和 `console/app.py` 为兼容性保留，直接运行它们不会启用这里的增强保护；务必改用安全入口。
- 只读命令识别是保守规则，厂商扩展命令可能被当作未知写命令。不要为了方便直接切换到 `unrestricted`；先 review 命令后再逐条确认。
- 白名单支持精确主机名和 IP/CIDR；主机名不做 DNS 解析，避免 DNS 重绑定造成的目标校验歧义。
- 输出脱敏是通用正则，不能保证识别所有厂商/版本的密钥格式。原始控制台日志若由旧入口开启仍可能含敏感数据；请限制文件权限并清理旧日志。
- 输出截断限制返回内容与广播事件大小，不是设备侧分页/执行取消机制；设备仍可能在本地会话中产生较大回显。
- API token 配置后，所有 API 请求都需带 token。内置网页目前未注入 token，因此启用 token 时建议配合本地受信任代理/客户端，或继续仅绑定 loopback 且不设置 token。不要把 token 放进仓库或浏览器可公开访问的静态文件。
