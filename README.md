# net-cli-mcp

> 面向网络工程师的 **MCP 服务 + 实时 Web 控制台**：让 AI 用自然语言管理 Cisco / 华为 / H3C 交换机，命令和回显在同一个窗口里同步可见。

把原来两个只支持单一厂商的工具（`cisco-cli-mcp` / `h3c-cli-mcp`）合并重写，补齐了华为 VRP 支持、多厂商自动识别、串口与 SSH 通道，并加了一个 SSE 实时串流窗口——AI 下的每一条命令、设备的每一块回显，你都能亲眼看到。

![status](https://img.shields.io/badge/python-3.10%2B-blue)
![status](https://img.shields.io/badge/vendors-Cisco%20%7C%20Huawei%20%7C%20H3C-green)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

---

## 为什么需要它

| 痛点 | 这个项目怎么解决 |
|---|---|
| AI 直接连交换机，下了什么命令你不知道 | Web 控制台实时串流：`tx`（命令）/ `rx`（回显）逐块推送，命令带来源徽标 |
| 同一套提示符正则在华为设备上不匹配 | 多厂商识别：banner 关键字 → `display version` 精判 → 提示符兜底，三级判定 |
| 长回显卡在 `---- More ----`，下一条命令首字符被吃掉 | 发送前探测残留并自动翻完页；执行中遇分页标记自动续翻 |
| 只能 Telnet，设备没网口时无计可施 | Telnet / SSH / Console 串口三种通道，共用同一套会话抽象与命令执行逻辑 |
| 输出里混着 ANSI 控制序列，日志没法看 | 统一净化：剔除 `\x1b[42D` 等序列与页标记 |

---

## 架构

```
┌─────────────────────┐        ┌──────────────────────┐
│  MCP 客户端          │        │  浏览器窗口            │
│  (Claude / WorkBuddy)│        │  http://127.0.0.1:8199│
└──────────┬──────────┘        └───────────┬──────────┘
           │ MCP (stdio)                   │ SSE + HTTP
           ▼                               ▼
┌────────────────────────────────────────────────────────┐
│  server.py  ──  会话管理 / 厂商识别 / 提示符 / 翻页 / 净化  │
│  console/app.py  ── 事件总线 · 流式执行 · 历史落盘          │
└────────────────────────┬───────────────────────────────┘
                         │ reader.read(n) / writer.write()
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   telnetlib3        paramiko          pyserial
   Telnet :23        SSH :22          COMx @9600
        └────────────────┼────────────────┘
                         ▼
              Cisco IOS / Huawei VRP / H3C Comware
```

三种传输都以 `reader.read()` / `writer.write()` / `drain()` / `close()` 对齐，所以会话逻辑只有一份。

---

## 快速开始

```bash
git clone https://github.com/gufeihong/AI-net-cli-mcp.git
cd AI-net-cli-mcp

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> `pyserial` 不装 → 串口工具自动隐藏；`paramiko` 不装 → SSH 工具自动隐藏，其余功能不受影响。

### 1) 注册到 MCP 客户端

把 `mcp-config.example.json` 的内容合并进你的客户端配置：

- **Claude Desktop**：`claude_desktop_config.json`
- **WorkBuddy**：`~/.workbuddy/mcp.json`

```json
{
  "mcpServers": {
    "net-cli-mcp": {
      "command": "python",
      "args": ["C:/tools/net-cli-mcp/server.py"]
    }
  }
}
```

重启客户端后可见 7 个工具：

| 工具 | 说明 |
|---|---|
| `telnet_connect(host, port, username, password, timeout)` | 建立 Telnet 会话，自动登录 / 关分页 / 识别厂商 |
| `telnet_execute(session_id, command, wait_ms)` | 下发命令，返回净化后的回显、当前提示符与视图 |
| `telnet_list_sessions()` | 列出活动会话 |
| `telnet_disconnect(session_id)` | 断开并释放 |
| `serial_connect(port, baudrate, ...)` | Console 串口会话（USB 转串口） |
| `serial_list_ports()` | 枚举本机串口 |
| `ssh_connect(host, port, username, password, timeout)` | SSH 会话（paramiko，`invoke_shell` 通道） |

一串自然语言的典型往返：

```
你：连一下 192.168.1.1，看看有哪些端口是 UP 的
AI：telnet_connect(192.168.1.1, username=..., password=...)
    → vendor=huawei, prompt=<SW1>, view=user-view
    telnet_execute("display interface brief | include up")
    → 24 个 GE 口 UP
```

### 2) 打开实时控制台

```bash
cd console
python app.py
# 或双击 start-console.bat（Windows）
```

浏览器打开 <http://127.0.0.1:8199>。窗口特性：

- **逐块串流**：命令下发与回显实时推送，不是执行完才一次性返回
- **视觉高亮**：IP / 接口名 / 数字 / `UP` / `DOWN` / MAC / 时间戳 / 配置关键字 / 报错分色显示，可一键开关
- **字号可调**：12–30px，`A+` / `A−` 或 Ctrl+滚轮，localStorage 记忆
- **命令行交互**：窗口里直接敲命令，走同一条会话；↑↓ 翻历史，Ctrl+L 清屏
- **来源徽标**：AI 发的命令带 `[来源]` 标记，和手敲的区分开
- **历史落盘**：`console/logs/<host>-<时间>.jsonl`，一次会话一个文件，便于复盘
- 只监听 `127.0.0.1`，不对外暴露

---

## HTTP API（给 Agent / 脚本用）

控制台同时是一个极简的 HTTP 桥，任何能发 HTTP 的程序都能驱动它——这是把网络设备能力挂到自研 Agent 上最省事的路径。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 当前连接状态：host / vendor / prompt / view |
| GET | `/api/serialports` | 枚举串口 |
| GET | `/api/stream` | SSE 事件流（`tx` / `rx` / `session` / `prompt` / `err`） |
| POST | `/api/connect` | `{transport: telnet\|ssh\|serial, host, port, username, password}` |
| POST | `/api/exec` | `{command, wait_ms, source}` → `{output, prompt, view, waitingInput}` |
| POST | `/api/disconnect` | 断开当前会话 |
| POST | `/api/clear` | 清空窗口历史 |

```python
import json, urllib.request

BASE = "http://127.0.0.1:8199"

def api(path, data=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())

api("/api/connect", {"transport": "telnet", "host": "192.168.1.1",
                     "username": "admin", "password": "***"})
print(api("/api/exec", {"command": "display version", "wait_ms": 8000})["output"])
api("/api/disconnect", {})
```

想让 LLM Agent 按「先思考 → 命令上屏 → 逐步播报」的规矩干活，见 [docs/agent-bridge.md](docs/agent-bridge.md)。

---

## 多厂商兼容

| 能力 | Huawei VRP | H3C Comware | Cisco IOS |
|---|---|---|---|
| 提示符识别 | `<HOST>` / `[HOST]` / `[HOST-GE0/0/1]` / `[~HOST]` | 同左 | `SW1>` / `SW1#` / `SW1(config)#` |
| 关闭分页 | `screen-length 0 temporary` | 同左 | `terminal length 0` |
| 分页标记 | `---- More ----` | 同左 | `--More--` |
| 交互确认 | `[Y/N]:` 立即返回 | 同左 | 同左 |
| 厂商精判 | `display version` | `display version` | `show version` |

识别顺序：连接 banner 关键字 → `display version` / `show version` → 提示符兜底。实测在 FutureMatrix（华为系）设备上不会误判为 H3C。

---

## 实战踩过的坑（都已修）

这些是拿真实生产设备磨出来的，写在这里省你几天时间：

1. **命令首字符被吞**
   上一条命令停在 `---- More ----` 时，VRP 把新命令的第一个 `d` 当作「向下翻半屏」的按键。→ `execute()` 发送前先做 0.15s 残留探测，有输出就完整排干（含自动翻页）。

2. **SSH 必须用 `invoke_shell`**
   网络设备不支持 exec 通道，`paramiko.exec_command` 一律失败。必须 `invoke_shell(term='vt100', width=200, height=60)` 走交互式 shell。

3. **主机密钥必须先接受**
   设备没有稳定指纹，`RejectPolicy` 会卡在 `yes/no`。→ `AutoAddPolicy`。

4. **华为 `display ip pool` 的接口名不带空格**
   `display ip pool name Vlanif1 used` 报错，正确的是 `display ip pool interface Vlanif1 used`。

5. **不可逆密码 + 老固件的 SSH 密码认证可能完全不可用**
   某些 VRP 版本（实测 V200R020C30）即使 telnet 同密码可登录，SSH 密码认证恒为 `User password authentication failed`；想改可逆存储 `cipher` 又被拒（`not allowed to use a reversible encryption algorithm`）。换客户端（paramiko / asyncssh）、换认证域、重建 `ssh user` 绑定均无效。→ 这类设备只能走 Telnet / Console，代码无需改。

6. **USB 转串口线要认芯片**
   PL2303 经典款（`VID_067B&PID_2303`）必须用 3.x 版驱动，5.x 只支持新芯片（23A3/23B3/23C3），装上去设备管理器直接报错。

7. **Windows 下设备会主动断连**
   长会话偶尔出现 `WinError 64 / 10054`，是设备侧行为，重连即可，别去改客户端参数。

---

## 目录结构

```
net-cli-mcp/
├── server.py                  # MCP 服务：会话管理 + 多厂商适配
├── console/
│   ├── app.py                 # Web 控制台后端（Starlette + SSE）
│   ├── static/index.html      # 前端窗口（单文件，无构建）
│   ├── start-console.bat
│   └── stop-console.bat
├── docs/
│   └── agent-bridge.md        # 让 LLM Agent 驱动控制台的规范
├── requirements.txt
├── mcp-config.example.json
└── LICENSE
```

---

## 安全说明

- 控制台**只绑 127.0.0.1**，不对外网开放。
- Telnet 是**明文协议**，账号密码在网络上是裸奔的。生产环境请优先 SSH / Console 串口，或把 Telnet 限制在带外管理网。
- 这个工具能改配置。**接到生产设备前，先确认你的 AI 客户端会为写操作征求你的同意**——这是使用者的责任，工具本身不做权限拦截。
- `console/logs/` 里有完整设备回显，可能含敏感信息，已在 `.gitignore` 中排除。

---

## 致谢

- [Meteorite123/cisco-cli-mcp](https://github.com/Meteorite123/cisco-cli-mcp) —— 最初的思科实现与提示符思路
- [enzymecat9/h3c-cli-mcp](https://github.com/enzymecat9/h3c-cli-mcp) —— H3C Comware 适配参考
- [telnetlib3](https://github.com/jquast/telnetlib3) · [paramiko](https://github.com/paramiko/paramiko) · [pyserial](https://github.com/pyserial/pyserial) · [Starlette](https://github.com/encode/starlette) · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

## License

MIT
