"""Hardened local-console entrypoint, version 2.0.0. Run with: python secure_console.py"""
import json
import os
import re

import uvicorn
from starlette.responses import JSONResponse

import console.app as base
from security import MAX_OUTPUT_CHARS, MAX_WAIT_MS, audit, check_command, check_target, check_token, redact_text, sanitize

_original_push = base.push

def _safe_push(kind: str, **payload):
    safe = {k: sanitize(v) for k, v in payload.items()}
    if "text" in safe and isinstance(safe["text"], str):
        safe["text"] = safe["text"][:MAX_OUTPUT_CHARS]
    return _original_push(kind, **safe)

base.push = _safe_push


async def _guarded_connect(request):
    body = await request.json()
    transport = (body.get("transport") or "telnet").lower()
    if transport != "serial":
        ok, reason = check_target(body.get("host", ""))
        if not ok:
            audit("connect_denied", transport=transport, host=body.get("host", ""), reason=reason)
            return JSONResponse({"ok": False, "error": reason}, status_code=403)
    else:
        allowed = [x.strip().lower() for x in os.getenv("NETCLI_ALLOWED_SERIAL_PORTS", "").split(",") if x.strip()]
        if allowed and str(body.get("host", "")).lower() not in allowed:
            return JSONResponse({"ok": False, "error": "串口不在 NETCLI_ALLOWED_SERIAL_PORTS 白名单中"}, status_code=403)
    response = await base.api_connect(request)
    audit("connect", transport=transport, host=body.get("host", ""), port=body.get("port"))
    return response


async def _guarded_exec(request):
    body = await request.json()
    command = body.get("command", "")
    sid = base._state.get("sessionId") or ""
    allowed, reason, risk = check_command(command, body.get("approval", ""), sid)
    if not allowed:
        audit("command_denied", session_id=sid, command=command, reason=reason)
        return JSONResponse({"ok": False, "error": reason, "approvalRequired": risk == "write"}, status_code=428 if risk == "write" else 400)
    try:
        body["wait_ms"] = min(max(int(body.get("wait_ms") or 15000), 1000), MAX_WAIT_MS)
    except (TypeError, ValueError):
        body["wait_ms"] = 15000
    request._body = json.dumps(body).encode("utf-8")
    response = await base.api_exec(request)
    if hasattr(response, "body") and response.media_type == "application/json":
        try:
            data = json.loads(response.body)
            data = sanitize(data)
            if isinstance(data.get("output"), str):
                data["output"] = data["output"][:MAX_OUTPUT_CHARS]
            response = JSONResponse(data, status_code=response.status_code)
        except (ValueError, TypeError):
            pass
    audit("command", session_id=sid, command=command, risk=risk, status=response.status_code)
    return response


for route in base.app.routes:
    if getattr(route, "path", None) == "/api/connect":
        route.endpoint = _guarded_connect
    elif getattr(route, "path", None) == "/api/exec":
        route.endpoint = _guarded_exec


@base.app.middleware("http")
async def protect_api(request, call_next):
    if request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        provided = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-netcli-token")
        if not check_token(provided):
            return JSONResponse({"ok": False, "error": "缺少或无效的 NETCLI_API_TOKEN"}, status_code=401)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin:
            if not re.match(r"^https?://(?:127\.0\.0\.1|localhost)(?::8199)?$", origin, re.I):
                return JSONResponse({"ok": False, "error": "拒绝跨站请求"}, status_code=403)
    return await call_next(request)


if __name__ == "__main__":
    print(f"安全加固网络控制台 v2.0.0 -> http://{base.HOST}:{base.PORT}")
    uvicorn.run(base.app, host=base.HOST, port=base.PORT, log_level="warning")
