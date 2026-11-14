"""Shared guardrails for the optional hardened MCP and local web entrypoints.

Configuration is via environment variables; see docs/security-hardening.md.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLICY = os.getenv("NETCLI_COMMAND_POLICY", "guarded").strip().lower()
if POLICY not in {"guarded", "readonly", "unrestricted"}:
    POLICY = "guarded"
MAX_OUTPUT_CHARS = max(1024, int(os.getenv("NETCLI_MAX_OUTPUT_CHARS", "65536")))
MAX_WAIT_MS = max(1000, int(os.getenv("NETCLI_MAX_WAIT_MS", "120000")))

# Conservative allowlist: unknown CLI syntax is treated as potentially mutating.
_READ_ONLY = re.compile(
    r"(?i)^\s*(?:display|show|ping|tracert|traceroute|terminal\s+length|"
    r"screen-length|quit|exit|return|end|help|\?)\b"
)
_DANGEROUS = re.compile(
    r"(?i)(?:^|\s)(?:system-view|configure(?:\s+terminal)?|conf\s+t|"
    r"interface|undo|no\s+|write(?:\s+memory)?|save|copy|delete|erase|"
    r"reload|reboot|shutdown|reset|format|debug|bootrom|startup\s+saved-configuration)(?:\s|$)"
)
_CONTROL = re.compile(r"[\r\n\x00;|`]")

# Redact common password/secret forms while preserving the directive.
_REDACTIONS = [
    (re.compile(r"(?i)(\b(?:password|passwd|secret|community|auth-key|priv-key)\b\s+(?:cipher|simple|0|7|8|9)?\s*)(\S+)") , r"\1<REDACTED>"),
    (re.compile(r"(?i)(\b(?:snmp-server\s+community|snmp-agent\s+community)\s+)(?:read|write)?\s*\S+") , r"\1<REDACTED>"),
    (re.compile(r"(?i)(\b(?:password|token|api[_-]?key)\s*[:=]\s*)([^\s,;]+)"), r"\1<REDACTED>"),
]


def redact_text(value: str) -> str:
    text = value
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)[:MAX_OUTPUT_CHARS]
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    return value


def check_command(command: str, approval: str | None = None, session_id: str = "") -> tuple[bool, str, str]:
    """Return (allowed, reason, risk); callers must fail closed on invalid input."""
    if not isinstance(command, str) or not command.strip():
        return False, "命令为空", "invalid"
    if len(command) > 4096 or _CONTROL.search(command):
        return False, "拒绝超长命令或包含多命令/控制字符的输入；请一次执行一条命令。", "invalid"
    safe = bool(_READ_ONLY.match(command)) and not bool(_DANGEROUS.search(command))
    if POLICY == "unrestricted" or (POLICY == "guarded" and safe):
        return True, "", "read" if safe else "write"
    if POLICY == "readonly":
        return False, "只读策略仅允许已识别的查询命令。", "write"
    expected = f"CONFIRM {session_id}" if session_id else "CONFIRM"
    if not approval or not secrets.compare_digest(approval.strip(), expected):
        return False, f"此命令未被识别为只读操作。人工确认后重试，approval 参数必须为：{expected}", "write"
    return True, "", "write"


def check_target(host: str) -> tuple[bool, str]:
    """If NETCLI_ALLOWED_HOSTS is set, enforce exact hosts and IP/CIDR entries."""
    allowed = [x.strip().lower() for x in os.getenv("NETCLI_ALLOWED_HOSTS", "").split(",") if x.strip()]
    if not allowed:
        return True, ""
    candidate = (host or "").strip().lower().rstrip(".")
    if not candidate:
        return False, "缺少目标地址"
    if candidate in allowed:
        return True, ""
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False, f"目标 {candidate} 不在 NETCLI_ALLOWED_HOSTS 白名单中"
    for item in allowed:
        try:
            if address in ipaddress.ip_network(item, strict=False):
                return True, ""
        except ValueError:
            continue
    return False, f"目标 {candidate} 不在 NETCLI_ALLOWED_HOSTS 白名单中"


def check_token(provided: str | None) -> bool:
    expected = os.getenv("NETCLI_API_TOKEN", "")
    return not expected or bool(provided and secrets.compare_digest(provided, expected))


def audit(action: str, **fields: Any) -> None:
    path = os.getenv("NETCLI_AUDIT_LOG", "").strip()
    if not path:
        return
    record = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, **sanitize(fields)}
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except OSError:
        # Never turn an otherwise successful device operation into a hidden failure.
        pass
