"""
网络设备实时控制台（本地 Web 窗口）

设计目标：让"我下发的命令"和"设备回显"在同一个窗口里实时可见。

- 服务端独占一条 telnet 会话（复用 net-cli-mcp 的会话管理与厂商识别）
- 命令下发 → 逐块读取回显 → 立即通过 SSE 推给所有打开的窗口
- 窗口里也能直接敲命令，走同一条会话，双方看到的是同一份状态
- 事件落盘：logs/<host>-<date>.jsonl，便于事后复盘

只监听 127.0.0.1，不对外暴露。
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))  # 仓库根目录的 server.py

import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import server as cli  # net-cli-mcp 的实现：会话管理 + 提示符/厂商识别

HOST = "127.0.0.1"
PORT = 8199
LOG_DIR = BASE / "logs"

# ---------------------------------------------------------------- 事件总线

subscribers: set[asyncio.Queue] = set()
history: list[dict] = []
_seq = 0
_state = {
    "connected": False,
    "host": None,
    "port": None,
    "vendor": None,
    "prompt": None,
    "view": None,
    "sessionId": None,
    "transport": None,
}
_exec_lock = asyncio.Lock()
_log_file = None


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def push(kind: str, **payload) -> dict:
    """向所有订阅窗口广播一条事件，并缓存/落盘"""
    global _seq
    _seq += 1
    ev = {"seq": _seq, "type": kind, "ts": _now()}
    ev.update(payload)
    history.append(ev)
    if len(history) > 4000:
        del history[:2000]
    for q in list(subscribers):
        try:
            q.put_nowait(ev)
        except Exception:
            pass
    if _log_file:
        try:
            _log_file.write(json.dumps(ev, ensure_ascii=False) + "\n")
            _log_file.flush()
        except Exception:
            pass
    return ev


def _display(chunk: str) -> str:
    """把设备裸字节转成适合展示的文本：去 ANSI、去页标记"""
    chunk = cli.ANSI_RE.sub("", chunk)
    chunk = cli.MORE_LINE_RE.sub("", chunk)
    return chunk


# ---------------------------------------------------------------- 核心：流式执行

async def exec_stream(cmd: str, wait_ms: int = 15000) -> str:
    """下发一条命令，逐块把回显推给窗口；返回完整原始回显"""
    sid = _state["sessionId"]
    if not sid or sid not in cli.session_manager.sessions:
        raise RuntimeError("尚未连接设备")

    sess = cli.session_manager.sessions[sid]

    lowered = cmd.lower().strip()
    for c in cli.LONG_RUNNING:
        if lowered.startswith(c):
            wait_ms = max(wait_ms, 15000)
            break

    # 发送前先把残留输出读干净：
    # 上一条命令若停在 ---- More ----，直接写命令会被分页吃掉首字符，导致命令残缺
    loop0 = asyncio.get_event_loop()
    idle_deadline = loop0.time() + 3.0
    while loop0.time() < idle_deadline:
        try:
            leftover = await asyncio.wait_for(sess.reader.read(4096), timeout=0.08)
        except asyncio.TimeoutError:
            break  # 静默 80ms，认为通道干净
        if not leftover:
            continue
        if any(m in leftover for m in cli.MORE_MARKERS):
            sess.writer.write(" ")
            await sess.writer.drain()
            shown = _display(leftover)
            if shown:
                push("rx", text=shown)

    sess.writer.write(cmd + "\r\n")
    await sess.writer.drain()

    out = ""
    loop = asyncio.get_event_loop()
    start = loop.time()
    last = start

    while loop.time() - start < wait_ms / 1000.0:
        try:
            data = await asyncio.wait_for(sess.reader.read(4096), timeout=0.1)
        except asyncio.TimeoutError:
            if out and (loop.time() - last) >= 1.0 and cli.match_prompt(cli._clean(out)):
                break
            continue
        if not data:
            continue

        out += data
        last = loop.time()

        # 分页停顿：自动发空格续翻，窗口不显示页标记
        if any(m in data for m in cli.MORE_MARKERS):
            sess.writer.write(" ")
            await sess.writer.drain()
            shown = _display(data)
            if shown:
                push("rx", text=shown)
            continue

        shown = _display(data)
        if shown:
            push("rx", text=shown)

        clean = cli._clean(out)
        if re.search(cli.INTERACTIVE_PROMPT, clean):
            break
        if cli.match_prompt(clean):
            await asyncio.sleep(0.15)
            try:
                extra = await asyncio.wait_for(sess.reader.read(4096), timeout=0.1)
                if extra:
                    out += extra
                    s = _display(extra)
                    if s:
                        push("rx", text=s)
            except asyncio.TimeoutError:
                pass
            break

    return out


# ---------------------------------------------------------------- HTTP 处理

async def page(request):
    return HTMLResponse((BASE / "static" / "index.html").read_text(encoding="utf-8"))


async def api_stream(request):
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            for ev in list(history):
                yield {"data": json.dumps(ev, ensure_ascii=False)}
            while True:
                ev = await q.get()
                yield {"data": json.dumps(ev, ensure_ascii=False)}
        finally:
            subscribers.discard(q)

    return EventSourceResponse(gen())


async def api_status(request):
    return JSONResponse(_state)


async def api_serialports(request):
    """枚举本机串口，供前端 CONSOLE 连接选择 COM 口"""
    return JSONResponse({"ok": True, "ports": cli.list_serial_ports()})


async def api_connect(request):
    global _log_file
    body = await request.json() if await request.body() else {}
    transport = (body.get("transport") or "telnet").lower()
    username = body.get("username") or ""
    password = body.get("password") or ""

    if transport == "serial":
        host = (body.get("host") or "").strip()
        port = int(body.get("port") or 9600)  # 串口模式下 port 复用为波特率
        if not host:
            return JSONResponse({"ok": False, "error": "缺少串口名（host）"}, status_code=400)
        connect_desc = f"Console {host}@{port}"
    elif transport == "ssh":
        host = (body.get("host") or "").strip()
        port = int(body.get("port") or 22)
        if not host:
            return JSONResponse({"ok": False, "error": "缺少 host"}, status_code=400)
        if not username:
            return JSONResponse({"ok": False, "error": "SSH 需要用户名"}, status_code=400)
        connect_desc = f"SSH {host}:{port}"
    else:
        host = (body.get("host") or "").strip()
        port = int(body.get("port") or 23)
        if not host:
            return JSONResponse({"ok": False, "error": "缺少 host"}, status_code=400)
        connect_desc = f"{host}:{port}"

    if _state["connected"] and _state["sessionId"]:
        try:
            await cli.session_manager.disconnect(_state["sessionId"])
        except Exception:
            pass

    async with _exec_lock:
        push("info", text=f"正在连接 {connect_desc} …")
        try:
            if transport == "serial":
                sid = await cli.session_manager.connect_serial(
                    host, port, username=username, password=password,
                )
            elif transport == "ssh":
                sid = await cli.session_manager.connect_ssh(
                    host, port, int(body.get("timeout") or 10000),
                    username=username, password=password,
                )
            else:
                sid = await cli.session_manager.connect(
                    host, port, int(body.get("timeout") or 10000),
                    username=username, password=password,
                )
        except Exception as e:
            push("err", text=f"连接失败：{e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

        sess = cli.session_manager.sessions[sid]
        probe = await cli.session_manager.execute(sid, "", 1500)
        prompt = cli.detect_device_mode(probe)
        vendor = sess.vendor
        if vendor == "unknown":
            vendor = cli.detect_vendor(probe)
            sess.vendor = vendor

        _state.update({
            "connected": True, "host": host, "port": port, "vendor": vendor,
            "prompt": prompt, "view": cli.describe_view(prompt, vendor),
            "sessionId": sid, "transport": transport,
        })

        try:
            LOG_DIR.mkdir(exist_ok=True)
            name = f"{host.replace('.', '-').replace(':', '-')}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            _log_file = open(LOG_DIR / name, "a", encoding="utf-8")
        except Exception:
            _log_file = None

        push("session", text=f"已连接 {connect_desc}", **{k: _state[k] for k in
             ("vendor", "prompt", "view", "host", "port")})

    return JSONResponse({"ok": True, **_state})


async def api_exec(request):
    body = await request.json()
    cmd = (body.get("command") or "").rstrip("\r\n")
    if not cmd.strip():
        return JSONResponse({"ok": False, "error": "命令为空"}, status_code=400)
    if not _state["connected"]:
        return JSONResponse({"ok": False, "error": "尚未连接设备"}, status_code=409)

    async with _exec_lock:
        push("tx", text=cmd, source=body.get("source", "user"))
        try:
            out = await exec_stream(cmd, int(body.get("wait_ms") or 15000))
        except Exception as e:
            push("err", text=f"执行异常：{e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        prompt = cli.detect_device_mode(out)
        if prompt and prompt != "unknown":
            _state["prompt"] = prompt
            _state["view"] = cli.describe_view(prompt, _state["vendor"])
        waiting = bool(re.search(cli.INTERACTIVE_PROMPT, cli._clean(out)))
        push("prompt", prompt=prompt, view=_state["view"], waiting=waiting)

    return JSONResponse({
        "ok": True,
        "output": cli._strip_noise(out),
        "prompt": prompt,
        "view": _state["view"],
        "waitingInput": waiting,
    })


async def api_disconnect(request):
    global _log_file
    sid = _state.get("sessionId")
    if sid:
        try:
            await cli.session_manager.disconnect(sid)
        except Exception:
            pass
    _state.update({"connected": False, "sessionId": None, "prompt": None, "view": None})
    if _log_file:
        try:
            _log_file.close()
        except Exception:
            pass
        _log_file = None
    push("info", text="已断开连接")
    return JSONResponse({"ok": True})


async def api_clear(request):
    history.clear()
    push("clear")
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/", page),
    Route("/api/stream", api_stream),
    Route("/api/status", api_status),
    Route("/api/serialports", api_serialports),
    Route("/api/connect", api_connect, methods=["POST"]),
    Route("/api/exec", api_exec, methods=["POST"]),
    Route("/api/disconnect", api_disconnect, methods=["POST"]),
    Route("/api/clear", api_clear, methods=["POST"]),
])


if __name__ == "__main__":
    print(f"网络控制台 -> http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
