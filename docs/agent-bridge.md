# 让 LLM Agent 驱动网络设备（HTTP 桥接规范）

控制台（`console/app.py`）暴露了 7 个 HTTP 接口。任何具备「发 HTTP 请求」能力的 Agent 都能借此操作网络设备，不需要 MCP 支持。

本文档既是给开发者看的协议说明，也可以整份喂给 Agent 当作行为规范。

---

## 1. 接线

```
Agent  ──HTTP──▶  http://127.0.0.1:8199  ──▶  Telnet / SSH / Console  ──▶  网络设备
        (Python urllib / requests / fetch)

人     ──浏览器──▶ 同一个 8199，看到同一条会话的完整命令与回显
```

Agent 与浏览器操作的是**同一条会话**，所以人类可以随时接管。

## 2. 标准调用模板

```python
import json, urllib.request

BASE = "http://127.0.0.1:8199"

def api(path, data=None, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
```

## 3. 接口清单

### `GET /api/status`

```json
{"connected": true, "host": "192.168.1.1", "port": 23, "vendor": "huawei",
 "prompt": "[SW1]", "view": "system-view", "sessionId": "…", "transport": "telnet"}
```

### `POST /api/connect`

```json
{"transport": "telnet", "host": "192.168.1.1", "port": 23,
 "username": "admin", "password": "***", "timeout": 10000}
```

`transport` 可取值：`telnet`（默认，端口 23）、`ssh`（端口 22，必须带 username）、`serial`（`host` 填 `COM3`，`port` 复用为波特率）。

连接成功后服务端会自动完成：登录 → 关分页 → 厂商识别 → 提示符 / 视图判定。

### `POST /api/exec`

```json
{"command": "display version", "wait_ms": 8000, "source": "agent"}
```

返回：

```json
{"ok": true, "output": "…净化后的回显…", "prompt": "<SW1>",
 "view": "user-view", "waitingInput": false}
```

- `output` 已经是完整回显（自动翻页完成、ANSI 与页标记已剔除），**不需要重试拼接**。
- `waitingInput=true` 表示设备正等 `[Y/N]` 确认——此时把 `y` / `n` 作为下一条命令发出。
- `source` 只影响窗口里的来源徽标，便于区分人机操作。

### 其他

| 接口 | 用途 |
|---|---|
| `POST /api/disconnect` | 结束会话（用完必须调） |
| `GET /api/serialports` | 枚举本机串口，返回 `{"ports":[{"device":"COM3","description":"…"}]}` |
| `GET /api/stream` | SSE 事件流，元素含 `type`（`tx`/`rx`/`session`/`prompt`/`err`/`info`）与 `text` |
| `POST /api/clear` | 清空窗口历史（不影响会话） |

## 4. 行为规范（给 Agent 的硬要求）

1. **先思考再动手**：执行前在对话里说明——查什么、为什么这么查、用哪条命令。
2. **命令必须上屏**：每次 `/api/exec` 前先把命令原文写进对话（`即将执行：xxx`），禁止静默执行。
3. **一次一个动作**：一次工具调用只做一件事（连接 / 一条命令 / 断开），不要整流程塞进一个脚本。这样人能在对话里跟上节奏。
4. **执行后播报**：长回显只给要点（数字、异常、关键状态），不要整段粘贴。
5. **配置变更单独确认**：涉及 `system-view` 之后的写操作、`save`、`undo`，必须先说明影响面并取得明确同意；`save` 永远单独确认，绝不与查询合并。
6. **完成即断开**：任务结束调 `/api/disconnect`，不在设备上留空闲会话。
7. **遇异常停止**：认证失败、配置冲突、需要改配置才能继续时，停下来报告，不要自行尝试绕过。

## 5. 参考实现片段

```python
# 一条只读查询的完整流程
if not api("/api/status")["connected"]:
    api("/api/connect", {"transport": "telnet", "host": "192.168.1.1",
                         "port": 23, "username": "admin", "password": "***"})

print("即将执行：display ip routing-table statistics")
r = api("/api/exec", {"command": "display ip routing-table statistics",
                      "wait_ms": 10000, "source": "agent"})
print(r["output"])
print("当前视图：", r["view"])
```

```python
# 处理 [Y/N] 交互（例如 save 确认）
r = api("/api/exec", {"command": "save", "wait_ms": 8000})
if r.get("waitingInput"):
    r = api("/api/exec", {"command": "y", "wait_ms": 25000})
print(r["output"])
```

## 6. 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `409 尚未连接设备` | 会话已断 | 重新 `/api/connect` |
| `502 连接失败：…` | 地址不可达 / 账号密码错 / 该账号无此服务类型 | 先确认 `display local-user` 里的 AuthMask（T=Telnet、S=SSH、H=HTTP） |
| `output` 缺首字符 | 上一条命令停在分页状态 | 已在服务端修复；如仍出现请提 issue |
| 窗口不更新 | SSE 断连（浏览器休眠/代理） | 刷新页面，`/api/status` 检查服务是否存活 |
