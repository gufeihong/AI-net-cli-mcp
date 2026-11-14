# 安全加固版 2.0.0 使用说明

安全入口是**新增的可选入口**，不覆盖原始 `server.py` / `console/app.py`。MCP 客户端应改为启动 `secure_server.py`；浏览器控制台应使用 `python secure_console.py`。先在隔离实验环境验证命令分类、交互和设备兼容性，再连接生产设备。

## 版本入口

- 1.0.0 原始版本：`server.py`、`console/app.py`。
- 2.0.0 安全增强版：`secure_server.py`、`secure_console.py`。

## 安全策略

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `NETCLI_COMMAND_POLICY` | `guarded` | `guarded` 允许识别为只读的命令，其他命令须提供 `approval="CONFIRM <session_id>"`；`readonly` 拒绝其他命令；`unrestricted` 关闭命令审批（不建议） |
| `NETCLI_ALLOWED_HOSTS` | 未设置 | 逗号分隔的主机/IP/CIDR 白名单；设置后拒绝不匹配的 Telnet/SSH 目标。例如 `192.168.10.0/24,sw1.lab.example`。主机名只做精确匹配 |
| `NETCLI_ALLOWED_SERIAL_PORTS` | 未设置 | 逗号分隔串口白名单，例如 `COM3,/dev/ttyUSB0` |
| `NETCLI_API_TOKEN` | 未设置 | 设置后所有 `/api/*` 请求必须带 `Authorization: Bearer <token>` 或 `X-NetCLI-Token`。内置网页不会自动附带 token；启用此项时需由受信任的本地代理/客户端提供 token，不能把长期 token 写进公开静态页面 |
| `NETCLI_MAX_OUTPUT_CHARS` | `65536` | MCP/API 返回文本的截断上限 |
| `NETCLI_MAX_WAIT_MS` | `120000` | 单命令最大等待时间 |
| `NETCLI_AUDIT_LOG` | 未设置 | JSONL 审计日志路径；建议设为权限受限的本地目录 |

默认 `guarded` 是保守启发式策略，不是设备端授权系统。`approval` 是调用方传入的确认字段，不是独立的人类身份验证/审批系统；如需强制人类审批，应由可信客户端或外部审批服务实施。强制只读时设 `NETCLI_COMMAND_POLICY=readonly`，并在设备侧使用只读账号。

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

Web 控制台：

```bash
python secure_console.py
```

Linux/macOS 示例：

```bash
export NETCLI_COMMAND_POLICY=readonly
export NETCLI_ALLOWED_HOSTS="192.168.10.0/24"
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

## 边界与注意事项

- 原始 `server.py` 和 `console/app.py` 为兼容性保留；直接运行它们不会启用 V2 的增强保护。
- 只读命令识别是保守规则，厂商扩展命令可能被当作未知写命令。不要为了方便切换到 `unrestricted`。
- 白名单支持精确主机名和 IP/CIDR；主机名不做 DNS 解析。
- 输出脱敏使用通用正则，不能保证覆盖所有厂商/版本格式。请限制日志文件权限并清理旧日志。
- 输出截断限制返回内容与广播事件大小，不是设备侧分页/执行取消机制。
- 默认 Web 控制台只绑定 loopback。跨站检查和 API token 是纵深防护，不能替代操作系统隔离、设备 AAA 或变更审批。
