"""Hardened MCP entrypoint (version 2.0.0). Configure clients to launch this instead of server.py."""
import json
import os

from mcp.server.fastmcp import FastMCP
import server as core
from security import MAX_OUTPUT_CHARS, MAX_WAIT_MS, audit, check_command, check_target, redact_text

mcp = FastMCP("Network CLI MCP Server (guarded v2)")


def _json_result(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"success": False, "error": redact_text(str(raw))[:MAX_OUTPUT_CHARS]}


@mcp.tool()
async def telnet_connect(host: str, port: int = 23, timeout: int = 5000, username: str = "", password: str = "") -> str:
    """Connect to an authorized device using Telnet. Host allowlist is controlled by NETCLI_ALLOWED_HOSTS."""
    ok, reason = check_target(host)
    if not ok:
        audit("connect_denied", transport="telnet", host=host, reason=reason)
        return json.dumps({"success": False, "error": reason}, ensure_ascii=False)
    result = await core.telnet_connect(host, port, timeout, username, password)
    audit("connect", transport="telnet", host=host, port=port)
    return redact_text(result)[:MAX_OUTPUT_CHARS]


@mcp.tool()
async def ssh_connect(host: str, port: int = 22, username: str = "", password: str = "", timeout: int = 10000) -> str:
    """Connect to an authorized device over SSH."""
    ok, reason = check_target(host)
    if not ok:
        audit("connect_denied", transport="ssh", host=host, reason=reason)
        return json.dumps({"success": False, "error": reason}, ensure_ascii=False)
    result = await core.ssh_connect(host, port, username, password, timeout)
    audit("connect", transport="ssh", host=host, port=port)
    return redact_text(result)[:MAX_OUTPUT_CHARS]


@mcp.tool()
async def serial_connect(com_port: str, baudrate: int = 9600, username: str = "", password: str = "") -> str:
    """Connect to a local console port. Optional NETCLI_ALLOWED_SERIAL_PORTS is a comma-separated allowlist."""
    allowed = [x.strip().lower() for x in os.getenv("NETCLI_ALLOWED_SERIAL_PORTS", "").split(",") if x.strip()]
    if allowed and com_port.lower() not in allowed:
        return json.dumps({"success": False, "error": "串口不在 NETCLI_ALLOWED_SERIAL_PORTS 白名单中"}, ensure_ascii=False)
    result = await core.serial_connect(com_port, baudrate, username, password)
    audit("connect", transport="serial", port=com_port, baudrate=baudrate)
    return redact_text(result)[:MAX_OUTPUT_CHARS]


@mcp.tool()
async def telnet_execute(session_id: str, command: str, wait_ms: int = 2000, approval: str = "") -> str:
    """Run one CLI command. Unknown/mutating commands require an explicit confirmation parameter in guarded mode."""
    allowed, reason, risk = check_command(command, approval, session_id)
    if not allowed:
        audit("command_denied", session_id=session_id, command=command, reason=reason)
        return json.dumps({"success": False, "error": reason, "approvalRequired": risk == "write"}, ensure_ascii=False)
    bounded_wait = min(max(int(wait_ms), 1000), MAX_WAIT_MS)
    result = _json_result(await core.telnet_execute(session_id, command, bounded_wait))
    if isinstance(result.get("output"), str):
        result["output"] = redact_text(result["output"])[:MAX_OUTPUT_CHARS]
    audit("command", session_id=session_id, command=command, risk=risk, success=result.get("success", False))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def telnet_list_sessions() -> str:
    return redact_text(core.telnet_list_sessions())[:MAX_OUTPUT_CHARS]


@mcp.tool()
def serial_list_ports() -> str:
    return redact_text(core.serial_list_ports())[:MAX_OUTPUT_CHARS]


@mcp.tool()
async def telnet_disconnect(session_id: str) -> str:
    result = await core.telnet_disconnect(session_id)
    audit("disconnect", session_id=session_id)
    return redact_text(result)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
