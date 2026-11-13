"""
Network CLI MCP Server —— 统一多厂商 Telnet 设备控制

合并来源：
- cisco-cli-mcp   https://github.com/Meteorite123/cisco-cli-mcp
- h3c-cli-mcp     https://github.com/enzymecat9/h3c-cli-mcp

支持 Cisco IOS / Huawei VRP / H3C Comware：

- 自动厂商识别（banner 关键字 → display version 精判 → 提示符兜底）
- 自动登录（Username / Password，含首次强制改密应答 N）
- 自动关闭分页（H3C-Huawei: screen-length 0 temporary / Cisco: terminal length 0）
- ---- More ---- 自动翻页
- 设备视图识别（用户视图 / 系统视图 / 接口视图 / 配置模式）
- 耗时命令自动延长等待（save、display current-configuration 等）
- [Y/N] 交互确认时立即返回，由上层决定应答

工具：telnet_connect / telnet_execute / telnet_list_sessions / telnet_disconnect
      serial_connect / serial_list_ports（CONSOLE 串口，与 telnet 会话共用同一套 execute）
      ssh_connect（SSH，paramiko 实现，同样复用 telnet_execute / telnet_disconnect）

会话统一抽象：reader 提供 read(n) 协程，writer 提供 write(str)/drain()/close()，
三种传输（telnetlib3 / pyserial / paramiko）都以同一接口对齐。
"""

import asyncio
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import telnetlib3
from mcp.server.fastmcp import FastMCP

try:
    import paramiko
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

try:
    import serial
    from serial.tools import list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

mcp = FastMCP("Network CLI MCP Server (Cisco/Huawei/H3C)")

# H3C / Huawei 同属 Comware / VRP 家族，提示符与命令风格一致
# comware 为兜底标识：提示符形似但版本信息未取到时的归类
COMware_VENDORS = ("h3c", "huawei", "comware")

# ---- 提示符识别 ----
# H3C / Huawei：用户视图 <HOST>，系统视图 [HOST]，子视图 [HOST-GigabitEthernet1/0/1]，
#               两阶段生效 [~HOST]
COMware_PROMPTS = [
    r'<[^<>\r\n]{1,63}>\s*$',
    r'\[~?[^\[\]\r\n]{1,63}\]\s*$',
]
# Cisco：用户模式 SW1>，特权模式 SW1#，配置模式 SW1(config)# / SW1(config-if)#
CISCO_PROMPTS = [
    r'[A-Za-z0-9_.\-]+\([^)\r\n]{1,40}\)[#>]\s*$',
    r'[A-Za-z0-9_.\-]+[#>]\s*$',
]
# 设备在等用户输入（save 确认、首次登录改密等）
INTERACTIVE_PROMPT = r'\[[Yy]/[Nn]\]\s*:?\s*$'

ALL_PROMPTS = COMware_PROMPTS + CISCO_PROMPTS + [INTERACTIVE_PROMPT]

MORE_MARKERS = ("---- More ----", "--More--", "<--- More --->")

# 厂商特征（按优先级排列，H3C 在前，避免 Comware 被误判）
VENDOR_HINTS = [
    ("h3c", [
        r"H3C\s+Comware",
        r"Comware\s+Software",
        r"HP\s+Comware",
        r"H3C\s+S\d{3,}",
        r"H3C\s+SecPath",
        r"H3C\s+MSR",
    ]),
    ("huawei", [
        r"VRP\s*\(R\)\s*software",
        r"Huawei\s+Versatile\s+Routing\s+Platform",
        r"FutureMatrix",
        r"HUAWEI\s+S\d{4}",
    ]),
    ("cisco", [
        r"Cisco\s+IOS",
        r"IOS\s*\(tm\)",
        r"Cisco\s+Internetwork\s+Operating\s+System",
    ]),
]

# 耗时命令（命中则把等待时间抬到 at least 15s）
LONG_RUNNING = (
    # 通用
    "ping", "tracert", "traceroute", "reload", "debug", "copy", "dir",
    # H3C / Huawei
    "display current-configuration", "display saved-configuration",
    "display diagnostic-information", "display logbuffer", "display trapbuffer",
    "display interface", "display arp", "display ip routing-table",
    "display mac-address", "display device", "display cpu-usage",
    "display memory", "display tech-support", "save", "backup",
    # Cisco
    "show tech", "write", "show running-config", "show startup-config",
)


def _clean(output: str) -> str:
    """清理退格等控制字符"""
    return output.replace('\x08', '').replace(' \b', '')


# 翻页产生的 ANSI 光标控制序列与页标记，返回前统一剔除
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
MORE_LINE_RE = re.compile(r'[ \t]*(----\s*More\s*----|--More--|<---\s*More\s*--->)[ \t]*')


def _strip_noise(text: str) -> str:
    """剔除 ANSI 序列、页标记与行尾空白，保留正文"""
    if not text:
        return text
    text = ANSI_RE.sub('', text)
    text = MORE_LINE_RE.sub('', text)
    return '\n'.join(line.rstrip() for line in text.split('\n'))


def match_prompt(output: str) -> Optional[str]:
    """从输出末尾识别提示符，返回提示符原文；识别不到返回 None"""
    if not output:
        return None
    clean = _clean(output)
    tail = clean[-400:]
    for pat in ALL_PROMPTS:
        m = re.search(pat, tail)
        if m:
            return m.group(0).strip()
    # 提示符后面可能跟了空行
    for line in reversed(clean.strip().split('\n')[-4:]):
        line = line.strip()
        if not line:
            continue
        for pat in ALL_PROMPTS:
            if re.fullmatch(pat.replace(r'\s*$', ''), line):
                return line
    return None


def detect_vendor_by_keyword(output: str) -> str:
    """仅凭 banner / 版本信息中的厂商关键字判断，判断不出返回 unknown"""
    if not output:
        return "unknown"
    for vendor, pats in VENDOR_HINTS:
        for pat in pats:
            if re.search(pat, output, re.IGNORECASE):
                return vendor
    return "unknown"


def looks_like_comware(output: str) -> bool:
    """提示符形态是否符合 Comware / VRP 家族"""
    tail = (output or "")[-400:]
    return any(re.search(p, tail) for p in COMware_PROMPTS)


def detect_vendor(output: str) -> str:
    """先按关键字判断，再按提示符形态兜底"""
    vendor = detect_vendor_by_keyword(output)
    if vendor != "unknown":
        return vendor
    if looks_like_comware(output):
        return "comware"
    tail = (output or "")[-400:]
    if any(re.search(p, tail) for p in CISCO_PROMPTS):
        return "cisco"
    return "unknown"


def detect_device_mode(output: str) -> str:
    """返回当前提示符原文，识别不到返回 unknown"""
    p = match_prompt(output)
    return p if p else "unknown"


def is_config_view(mode: str, vendor: str) -> bool:
    """是否处于系统视图 / 配置模式"""
    if not mode or mode == "unknown":
        return False
    if vendor in COMware_VENDORS:
        return mode.startswith("[")
    return "(config" in mode


IFACE_PROMPT_HINT = re.compile(
    r"-(Vlanif|GigabitEthernet|XGigabitEthernet|Eth-Trunk|LoopBack|InLoopBack|"
    r"Tunnel|NULL|MEth|10GE|25GE|40GE|100GE|Serial|Virtual-Template)",
    re.IGNORECASE,
)
SUBVIEW_PROMPT_HINT = re.compile(
    r"-(aaa|acl|ospf|isis|bgp|vlan|mpls|ip|user-interface|radius|qos|policy|"
    r"local-user|ssh|snmp-agent|dhcp|nqa|nat)",
    re.IGNORECASE,
)


def describe_view(mode: str, vendor: str) -> str:
    """把提示符翻译成视图名"""
    if not mode or mode == "unknown":
        return "unknown"
    if vendor in COMware_VENDORS:
        if mode.startswith("<"):
            return "user-view"
        if mode.startswith("[~"):
            return "system-view(uncommitted)"
        if mode.startswith("["):
            # [HOST] 是系统视图；[HOST-Vlanif1] / [HOST-GigabitEthernet1/2/0/0] 是接口视图
            body = mode.strip("[]")
            if not IFACE_PROMPT_HINT.search(body):
                if SUBVIEW_PROMPT_HINT.search(body):
                    return "sub-view(" + body.split("-", 1)[1] + ")"
                return "system-view"
            m = IFACE_PROMPT_HINT.search(body)
            return "interface-view(" + body[m.start():].lstrip("-") + ")"
        return "unknown"
    if "(config-if" in mode:
        return "interface-config"
    if "(config" in mode:
        return "global-config"
    if mode.endswith("#"):
        return "privileged"
    if mode.endswith(">"):
        return "user"
    return "unknown"


@dataclass
class TelnetSession:
    """一个 Telnet 会话"""
    session_id: str
    host: str
    port: int
    reader: telnetlib3.TelnetReader
    writer: telnetlib3.TelnetWriter
    vendor: str = "unknown"
    transport: str = "telnet"
    connected_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "host": self.host,
            "port": self.port,
            "vendor": self.vendor,
            "transport": self.transport,
            "connectedAt": self.connected_at.isoformat(),
        }


class _SerialReader:
    """串口后台读线程 → asyncio 队列；对外暴露与 telnetlib3 reader 相同的 read() 协程"""

    def __init__(self, port):
        self._port = port
        self._q: asyncio.Queue = asyncio.Queue()
        self._alive = True
        self._loop = asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        while self._alive:
            try:
                data = self._port.read(512)
            except Exception:
                data = b""
            if data:
                text = data.decode("utf-8", errors="replace")
                self._loop.call_soon_threadsafe(self._q.put_nowait, text)
            else:
                time.sleep(0.02)

    async def read(self, n: int = 4096) -> str:
        if not self._alive:
            return ""
        # 调用方总是用 asyncio.wait_for 包裹本 read，超时会取消并进入其 TimeoutError 分支
        return await self._q.get()

    def stop(self):
        self._alive = False
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, "")
        except Exception:
            pass


class _SerialWriter:
    """串口写适配器：暴露 write(str) / drain() / close()，行为对齐 telnetlib3 writer"""

    def __init__(self, port, reader: _SerialReader):
        self._port = port
        self._reader = reader

    def write(self, text: str):
        try:
            self._port.write(text.encode("utf-8", errors="replace"))
            self._port.flush()
        except Exception as e:
            self._reader.stop()
            raise ConnectionError(f"串口写入失败（可能已拔出）: {e}")

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self._reader.stop()
        try:
            self._port.close()
        except Exception:
            pass


@dataclass
class SerialSession:
    """一个 CONSOLE 串口会话，接口与 TelnetSession 对齐"""
    session_id: str
    host: str          # COM 口名，如 COM3
    port: int          # 波特率
    reader: _SerialReader
    writer: _SerialWriter
    vendor: str = "unknown"
    transport: str = "serial"
    connected_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "host": self.host,
            "port": self.port,
            "vendor": self.vendor,
            "transport": self.transport,
            "connectedAt": self.connected_at.isoformat(),
        }


class _SSHReader:
    """paramiko channel 后台读线程 → asyncio 队列；read() 协程对齐 telnetlib3 reader"""

    def __init__(self, channel):
        self._chan = channel
        self._q: asyncio.Queue = asyncio.Queue()
        self._alive = True
        self._loop = asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        while self._alive:
            try:
                data = self._chan.recv(1024)
            except Exception:
                data = b""
                self._alive = False
            if data:
                text = data.decode("utf-8", errors="replace")
                try:
                    self._loop.call_soon_threadsafe(self._q.put_nowait, text)
                except RuntimeError:
                    break
            else:
                time.sleep(0.02)
        # 通道关闭：投递空串唤醒等待中的 read()
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, "")
        except Exception:
            pass

    async def read(self, n: int = 4096) -> str:
        if not self._alive and self._q.empty():
            return ""
        return await self._q.get()

    def stop(self):
        self._alive = False
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, "")
        except Exception:
            pass


class _SSHWriter:
    """SSH 写适配器：write(str) / drain() / close()，行为对齐 telnetlib3 writer"""

    def __init__(self, channel, reader: _SSHReader, client):
        self._chan = channel
        self._reader = reader
        self._client = client

    def write(self, text: str):
        try:
            self._chan.sendall(text.encode("utf-8", errors="replace"))
        except Exception as e:
            self._reader.stop()
            raise ConnectionError(f"SSH 写入失败（通道已关闭）: {e}")

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self._reader.stop()
        for fn in (self._chan.close, self._client.close):
            try:
                fn()
            except Exception:
                pass


@dataclass
class SSHSession:
    """一个 SSH 会话，接口与 TelnetSession 对齐"""
    session_id: str
    host: str
    port: int
    reader: _SSHReader
    writer: _SSHWriter
    vendor: str = "unknown"
    transport: str = "ssh"
    connected_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "host": self.host,
            "port": self.port,
            "vendor": self.vendor,
            "transport": self.transport,
            "connectedAt": self.connected_at.isoformat(),
        }


def list_serial_ports() -> list[dict]:
    """枚举本机可用串口"""
    if not HAS_SERIAL:
        return []
    out = []
    for p in list_ports.comports():
        out.append({"port": p.device, "description": p.description, "hwid": p.hwid})
    return out


class TelnetSessionManager:
    """Telnet 会话管理器"""

    def __init__(self):
        self.sessions: dict[str, TelnetSession] = {}

    # ---------- 底层读写 ----------

    async def _read_until(self, reader, timeout: float = 3.0) -> str:
        """读到提示符、静默或超时为止"""
        buf = ""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.2)
            except asyncio.TimeoutError:
                if match_prompt(buf):
                    break
                continue
            if not data:
                break
            buf += data
            if match_prompt(buf):
                await asyncio.sleep(0.15)
                try:
                    extra = await asyncio.wait_for(reader.read(4096), timeout=0.1)
                    if extra:
                        buf += extra
                except asyncio.TimeoutError:
                    pass
                break
        return buf

    async def _drain_output(self, reader, writer, quiet: float = 0.5) -> str:
        """丢弃残留输出，直到静默 quiet 秒；遇到 More 自动翻页"""
        buf = ""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        last = loop.time()
        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.2)
            except asyncio.TimeoutError:
                if loop.time() - last >= quiet:
                    break
                continue
            if not data:
                break
            buf += data
            last = loop.time()
            if any(m in data for m in MORE_MARKERS):
                writer.write(" ")
                await writer.drain()
        return buf

    async def _login(self, reader, writer, username: str, password: str) -> str:
        """交互式登录：Username / Password / 首次强制改密"""
        out = await self._read_until(reader, 3.0)

        # 无需登录，已直接到提示符
        if match_prompt(out):
            return out

        if re.search(r'(?i)(username|login)\s*[:：]\s*$', _clean(out).strip()[-120:]):
            writer.write(username + "\r\n")
            await writer.drain()
            out += await self._read_until(reader, 4.0)

        if re.search(r'(?i)password\s*[:：]\s*$', _clean(out).strip()[-120:]):
            writer.write(password + "\r\n")
            await writer.drain()
            out += await self._read_until(reader, 6.0)

        # 首次登录强制改密：不自动改，答 N，避免动设备配置
        tail = _clean(out).strip()[-200:]
        if re.search(r'(?i)(change\s+now|needs?\s+to\s+be\s+changed)', tail):
            writer.write("N\r\n")
            await writer.drain()
            out += await self._read_until(reader, 4.0)

        return out

    # ---------- 连接 ----------

    async def connect(
        self, host: str, port: int = 23, timeout: int = 5000,
        username: str = "", password: str = "",
    ) -> str:
        """建立连接并完成登录、厂商识别、关分页，返回 session_id"""
        try:
            reader, writer = await asyncio.wait_for(
                telnetlib3.open_connection(host, port),
                timeout=timeout / 1000.0,
            )
        except asyncio.TimeoutError:
            raise ConnectionError(f"连接超时: {host}:{port}")
        except Exception as e:
            raise ConnectionError(f"连接失败: {host}:{port} - {e}")

        session_id = str(uuid.uuid4())
        session = TelnetSession(session_id, host, port, reader, writer)
        self.sessions[session_id] = session

        try:
            # 激活终端（应对 Press RETURN to get started）
            for _ in range(3):
                writer.write("\r\n")
                await writer.drain()
                await asyncio.sleep(0.15)

            initial = await self._read_until(reader, 3.0)

            # 需要认证则自动登录
            if username and not match_prompt(initial):
                initial += await self._login(reader, writer, username, password)
            elif not match_prompt(initial):
                # 未提供账号但设备在要账号：明确报错，而不是静默失败
                tail = _clean(initial).strip()[-160:]
                if re.search(r'(?i)(username|login|password)\s*[:：]\s*$', tail):
                    raise ConnectionError(
                        "设备要求认证，请通过 username / password 参数提供账号密码"
                    )

            # 厂商识别 → 退回用户视图 → 关分页 → 清残留（与串口路径共用）
            initial = await self._finish_setup(session, reader, writer, initial)

            return session_id

        except Exception:
            # 失败时清理，避免留下半开的会话
            self.sessions.pop(session_id, None)
            try:
                writer.close()
            except Exception:
                pass
            raise

    async def _finish_setup(self, session, reader, writer, initial: str) -> str:
        """厂商识别 → 探测 → 退回用户视图 → 关闭分页 → 清残留。返回累计输出"""
        # 先用 banner 关键字判断；判断不出且提示符形态像 Comware，
        # 则跑一次只读的 display version 精确识别（在 H3C / Huawei 上都可用）
        session.vendor = detect_vendor_by_keyword(initial)
        if session.vendor == "unknown" and looks_like_comware(initial):
            writer.write("display version\r\n")
            await writer.drain()
            ver = await self._read_until(reader, 6.0)
            initial += ver
            session.vendor = detect_vendor_by_keyword(ver)

        if session.vendor == "unknown":
            # 还认不出，探一次提示符再兜底
            writer.write("\r\n")
            await writer.drain()
            probe = await self._read_until(reader, 2.0)
            initial += probe
            session.vendor = detect_vendor(initial)

        mode = detect_device_mode(initial)
        if is_config_view(mode, session.vendor):
            back_cmd = "return" if session.vendor in COMware_VENDORS else "end"
            writer.write(back_cmd + "\r\n")
            await writer.drain()
            initial += await self._read_until(reader, 3.0)

        # 关闭分页
        paging_cmd = (
            "screen-length 0 temporary"
            if session.vendor in COMware_VENDORS
            else "terminal length 0"
        )
        writer.write(paging_cmd + "\r\n")
        await writer.drain()
        await self._read_until(reader, 2.0)

        await self._drain_output(reader, writer, quiet=0.4)
        return initial

    async def connect_serial(
        self, com_port: str, baudrate: int = 9600,
        username: str = "", password: str = "",
    ) -> str:
        """打开 CONSOLE 串口并完成激活/登录/厂商识别/关分页，返回 session_id"""
        if not HAS_SERIAL:
            raise ConnectionError("pyserial 未安装，无法使用串口连接")

        try:
            sp = serial.Serial(
                port=com_port, baudrate=baudrate, bytesize=8, parity="N",
                stopbits=1, timeout=0.05, write_timeout=2.0,
                xonxoff=False, rtscts=False,
            )
        except Exception as e:
            raise ConnectionError(f"打开串口 {com_port}@{baudrate} 失败: {e}")

        session_id = str(uuid.uuid4())
        reader = _SerialReader(sp)
        writer = _SerialWriter(sp, reader)
        session = SerialSession(session_id, com_port, baudrate, reader, writer)
        self.sessions[session_id] = session

        try:
            # 激活终端（Console 口常处于挂在登录页或已登录状态）
            for _ in range(3):
                writer.write("\r\n")
                await writer.drain()
                await asyncio.sleep(0.25)

            initial = await self._read_until(reader, 3.0)

            if username and not match_prompt(initial):
                initial += await self._login(reader, writer, username, password)
            elif not match_prompt(initial):
                tail = _clean(initial).strip()[-160:]
                if re.search(r'(?i)password\s*[:：]\s*$', tail) and password:
                    # Console 口常见 Password-only 认证（user-interface con 0）
                    writer.write(password + "\r\n")
                    await writer.drain()
                    initial += await self._read_until(reader, 6.0)
                elif re.search(r'(?i)(username|login|password)\s*[:：]\s*$', tail):
                    raise ConnectionError(
                        "Console 口要求认证，请提供 password（或 username/password）"
                    )

            initial = await self._finish_setup(session, reader, writer, initial)
            return session_id

        except Exception:
            self.sessions.pop(session_id, None)
            try:
                writer.close()
            except Exception:
                pass
            raise

    async def connect_ssh(
        self, host: str, port: int = 22, timeout: int = 10000,
        username: str = "", password: str = "",
    ) -> str:
        """建立 SSH 连接（paramiko 交互式 shell）并完成厂商识别/关分页，返回 session_id"""
        if not HAS_SSH:
            raise ConnectionError("paramiko 未安装，无法使用 SSH 连接")
        if not username:
            raise ConnectionError("SSH 需要 username / password 认证")

        timeout_s = timeout / 1000.0

        def _open():
            client = paramiko.SSHClient()
            # 网络设备通常无固定 host key，首次连接自动接受
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    host, port=port, username=username, password=password,
                    timeout=timeout_s, banner_timeout=timeout_s, auth_timeout=timeout_s,
                    allow_agent=False, look_for_keys=False,
                )
            except paramiko.AuthenticationException:
                client.close()
                raise ConnectionError("SSH 认证失败：账号或密码错误，或该账号无 SSH 权限")
            except paramiko.SSHException as e:
                client.close()
                raise ConnectionError(
                    f"SSH 协商失败: {e}（确认设备已开启 stelnet server 且配置了 ssh user）"
                )
            except Exception as e:
                client.close()
                raise ConnectionError(f"SSH 连接失败: {host}:{port} - {e}")
            # 交互式 shell（网络设备不走 exec 通道，必须 invoke_shell）
            channel = client.invoke_shell(term="vt100", width=200, height=60)
            channel.settimeout(0.0)
            return client, channel

        loop = asyncio.get_event_loop()
        try:
            client, channel = await asyncio.wait_for(
                loop.run_in_executor(None, _open), timeout=timeout_s + 5
            )
        except asyncio.TimeoutError:
            raise ConnectionError(f"SSH 连接超时: {host}:{port}")

        session_id = str(uuid.uuid4())
        reader = _SSHReader(channel)
        writer = _SSHWriter(channel, reader, client)
        session = SSHSession(session_id, host, port, reader, writer)
        self.sessions[session_id] = session

        try:
            # 激活终端（部分设备 SSH 登录后需回车才出提示符）
            for _ in range(2):
                writer.write("\r\n")
                await writer.drain()
                await asyncio.sleep(0.15)

            initial = await self._read_until(reader, 3.0)
            initial = await self._finish_setup(session, reader, writer, initial)
            return session_id

        except Exception:
            self.sessions.pop(session_id, None)
            try:
                writer.close()
            except Exception:
                pass
            raise

    # ---------- 执行 ----------

    async def execute(self, session_id: str, command: str, wait_ms: int = 2000) -> str:
        """发送命令并读取回显，识别到提示符或 [Y/N] 即返回"""
        if session_id not in self.sessions:
            raise ValueError(f"会话不存在: {session_id}")

        session = self.sessions[session_id]

        lowered = command.lower().strip()
        for cmd in LONG_RUNNING:
            if lowered.startswith(cmd):
                wait_ms = max(wait_ms, 15000)
                break

        wait_seconds = wait_ms / 1000.0

        # 清残留探测：若上一条命令留下未读输出（尤其 ---- More ---- 挂起状态），
        # 新命令首字符会被 More 当翻页键吃掉（VRP 中 'd' = 下翻半屏）。先排干再发送。
        try:
            probe = await asyncio.wait_for(session.reader.read(4096), timeout=0.15)
        except asyncio.TimeoutError:
            probe = None
        if probe:
            await self._drain_output(session.reader, session.writer, quiet=0.3)

        session.writer.write(command + "\r\n")
        await session.writer.drain()

        output = ""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_data_time = start_time

        while True:
            current = loop.time()
            if current - start_time >= wait_seconds:
                break

            try:
                data = await asyncio.wait_for(session.reader.read(4096), timeout=0.1)
                if not data:
                    continue

                output += data
                last_data_time = current

                # 分页停顿 → 发空格继续
                if any(m in data for m in MORE_MARKERS):
                    session.writer.write(" ")
                    await session.writer.drain()
                    continue

                clean = _clean(output)

                # 设备在等确认，立即返回
                if re.search(INTERACTIVE_PROMPT, clean):
                    await asyncio.sleep(0.15)
                    try:
                        extra = await asyncio.wait_for(session.reader.read(4096), timeout=0.1)
                        if extra:
                            output += extra
                    except asyncio.TimeoutError:
                        pass
                    break

                # 命中提示符 → 命令完成
                if match_prompt(clean):
                    await asyncio.sleep(0.2)
                    try:
                        extra = await asyncio.wait_for(session.reader.read(4096), timeout=0.1)
                        if extra:
                            output += extra
                    except asyncio.TimeoutError:
                        pass
                    break

            except asyncio.TimeoutError:
                if output and (current - last_data_time) >= 1.0:
                    if match_prompt(_clean(output)):
                        break
            except Exception:
                pass

        return _strip_noise(output)

    def list_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    async def disconnect(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            raise ValueError(f"会话不存在: {session_id}")
        session = self.sessions[session_id]
        try:
            session.writer.close()
        except Exception:
            pass
        del self.sessions[session_id]
        return True


session_manager = TelnetSessionManager()


# ============ MCP 工具 ============

@mcp.tool()
async def telnet_connect(
    host: str, port: int = 23, timeout: int = 5000,
    username: str = "", password: str = "",
) -> str:
    """
    建立 Telnet 连接并返回会话ID（兼容 H3C Comware / Huawei VRP / Cisco IOS）

    连接过程会自动：激活终端 → 登录（若提供账号）→ 识别厂商 → 退回用户视图 → 关闭分页。

    Args:
        host: 设备地址
        port: 端口号，默认 23
        timeout: 连接超时（毫秒），默认 5000
        username: 登录用户名，设备免认证时留空
        password: 登录密码

    Returns:
        JSON：sessionId、vendor（h3c/huawei/cisco）、deviceMode（提示符原文）、
        view（user-view / system-view / interface-config 等）
    """
    try:
        session_id = await session_manager.connect(
            host, port, timeout, username=username, password=password
        )
        session = session_manager.sessions[session_id]

        probe = await session_manager.execute(session_id, "", 1500)
        device_mode = detect_device_mode(probe)
        vendor = session.vendor
        if vendor == "unknown":
            vendor = detect_vendor(probe)
            session.vendor = vendor

        return json.dumps({
            "success": True,
            "sessionId": session_id,
            "vendor": vendor,
            "deviceMode": device_mode,
            "view": describe_view(device_mode, vendor),
            "message": "连接成功",
        }, ensure_ascii=False)
    except ConnectionError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def telnet_execute(session_id: str, command: str, wait_ms: int = 2000) -> str:
    """
    在指定会话中执行命令，返回设备原始回显

    已自动处理：--More-- 翻页、提示符识别、视图判断、耗时命令延长等待。
    若返回的 waitingInput 为 true（回显以 [Y/N] 结尾），说明设备在等确认，
    需要再调用一次本工具，command 传 "y" 或 "n"。

    Args:
        session_id: telnet_connect 返回的会话ID
        command: 要下发的命令，例如 "display version"
        wait_ms: 最大等待时间（毫秒），默认 2000；命中提示符会立即返回

    Returns:
        JSON：output（原始回显）、deviceMode、view、waitingInput
    """
    try:
        output = await session_manager.execute(session_id, command, wait_ms)
        session = session_manager.sessions.get(session_id)
        vendor = session.vendor if session else "unknown"
        device_mode = detect_device_mode(output)
        return json.dumps({
            "success": True,
            "output": output if output else "",
            "vendor": vendor,
            "deviceMode": device_mode,
            "view": describe_view(device_mode, vendor),
            "waitingInput": bool(re.search(INTERACTIVE_PROMPT, _clean(output))),
        }, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def telnet_list_sessions() -> str:
    """列出所有活动会话（会话ID、主机、厂商、建立时间）"""
    sessions = session_manager.list_sessions()
    if not sessions:
        return "当前没有活动会话"
    lines = ["活动会话列表:"]
    for s in sessions:
        lines.append(f"- ID: {s['sessionId']}")
        lines.append(f"  主机: {s['host']}:{s['port']}")
        lines.append(f"  厂商: {s.get('vendor', 'unknown')}")
        lines.append(f"  连接时间: {s['connectedAt']}")
    return "\n".join(lines)


@mcp.tool()
async def telnet_disconnect(session_id: str) -> str:
    """
    断开指定会话

    Args:
        session_id: 会话ID

    Returns:
        断开结果
    """
    try:
        await session_manager.disconnect(session_id)
        return f"会话 {session_id} 已断开"
    except ValueError as e:
        return f"错误: {e}"


@mcp.tool()
def serial_list_ports() -> str:
    """列出本机所有可用串口（COM 口），用于 CONSOLE 连接前确认端口名"""
    ports = list_serial_ports()
    if not ports:
        return "未发现可用串口（USB 转 232 未插入或驱动未装）"
    lines = ["本机串口列表:"]
    for p in ports:
        lines.append(f"- {p['port']}: {p['description']}  [{p['hwid']}]")
    return "\n".join(lines)


@mcp.tool()
async def serial_connect(
    com_port: str, baudrate: int = 9600,
    username: str = "", password: str = "",
) -> str:
    """
    通过本机串口（CONSOLE 口）连接设备并返回会话ID

    连接过程与 telnet 相同：激活终端 → 登录（若需要）→ 识别厂商 → 关分页。
    建立的会话可直接用 telnet_execute / telnet_disconnect 操作。

    Args:
        com_port: 串口名，如 COM3（先用 serial_list_ports 查询）
        baudrate: 波特率，默认 9600（华为/H3C/思科 Console 默认都是 9600 8N1）
        username: 登录用户名（Console 多为仅密码认证，可留空）
        password: 登录密码

    Returns:
        JSON：sessionId、vendor、deviceMode、view
    """
    try:
        session_id = await session_manager.connect_serial(
            com_port, baudrate, username=username, password=password
        )
        session = session_manager.sessions[session_id]

        probe = await session_manager.execute(session_id, "", 1500)
        device_mode = detect_device_mode(probe)
        vendor = session.vendor
        if vendor == "unknown":
            vendor = detect_vendor(probe)
            session.vendor = vendor

        return json.dumps({
            "success": True,
            "sessionId": session_id,
            "transport": "serial",
            "comPort": com_port,
            "baudrate": baudrate,
            "vendor": vendor,
            "deviceMode": device_mode,
            "view": describe_view(device_mode, vendor),
            "message": "Console 连接成功",
        }, ensure_ascii=False)
    except ConnectionError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def ssh_connect(
    host: str, port: int = 22, username: str = "", password: str = "",
    timeout: int = 10000,
) -> str:
    """
    通过 SSH 连接设备并返回会话ID（兼容 Huawei VRP / H3C Comware / Cisco IOS）

    与 telnet_connect 的区别：走加密通道 + 账密认证，无需交互式登录。
    连接后自动：识别厂商 → 退回用户视图 → 关闭分页。
    建立的会话可直接用 telnet_execute / telnet_disconnect 操作。

    前提：设备侧已开启 SSH 服务（华为：stelnet server enable + 本地用户 service-type ssh）。

    Args:
        host: 设备地址
        port: SSH 端口，默认 22
        username: 登录用户名（必填）
        password: 登录密码
        timeout: 连接超时（毫秒），默认 10000

    Returns:
        JSON：sessionId、transport=ssh、vendor、deviceMode、view
    """
    try:
        session_id = await session_manager.connect_ssh(
            host, port, timeout, username=username, password=password
        )
        session = session_manager.sessions[session_id]

        probe = await session_manager.execute(session_id, "", 1500)
        device_mode = detect_device_mode(probe)
        vendor = session.vendor
        if vendor == "unknown":
            vendor = detect_vendor(probe)
            session.vendor = vendor

        return json.dumps({
            "success": True,
            "sessionId": session_id,
            "transport": "ssh",
            "host": host,
            "port": port,
            "vendor": vendor,
            "deviceMode": device_mode,
            "view": describe_view(device_mode, vendor),
            "message": "SSH 连接成功",
        }, ensure_ascii=False)
    except ConnectionError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
