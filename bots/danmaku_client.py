#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 线：B 站直播弹幕接入 -> 回复生成 -> 投递 A 线 /say。

链路：
    GET getDanmuInfo?id=<房间号>  取 token + host 列表
      -> wss://<host>:<wss_port>/sub
      -> 立刻发 auth 包（op=7；body: uid=0 / roomid / protover / platform=web / type=2 / key=<token>）
      -> 每 30 秒发 op=2 心跳
      -> 收 op=5 消息（protover=2 时 zlib 压缩；解压后仍可能由多条 16 字节头 + body 组成，
         按 4 字节大端长度切分）
      -> 取 cmd 为 DANMU_MSG 的弹幕：info[1] = 内容，info[2][1] = 用户名
      -> reply_engine 生成回复 -> POST http://127.0.0.1:8788/say

本机没有 websockets / brotli 库，所以：
  * WebSocket 用 aiohttp.ClientSession.ws_connect；
  * 默认 protover=2（zlib，标准库解压，最稳）；protover=3 需要 brotli，探测不到会直接报错提示。

凭据：只从 config/bilibili.txt 读取，日志里只出现「文件 + 字节数」，绝不回显内容。
导入本模块无副作用（不建日志、不读凭据、不发请求、不起事件循环）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import struct
import sys
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

# ---- 允许两种运行方式：python bots/danmaku_client.py 与 python -m bots.danmaku_client
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reply_engine import ReplyEngine, ReplyDecision, build_engine  # noqa: E402
    from say_client import DEFAULT_SAY_URL, SayClient, default_say_url  # noqa: E402
else:
    from .reply_engine import ReplyEngine, ReplyDecision, build_engine  # noqa: E402
    from .say_client import DEFAULT_SAY_URL, SayClient, default_say_url  # noqa: E402

try:  # 有 brotli 就能吃 protover=3；没有也不影响默认的 protover=2
    import brotli as _brotli  # type: ignore
except Exception:  # pragma: no cover - 环境相关
    try:
        import brotlicffi as _brotli  # type: ignore
    except Exception:  # pragma: no cover - 环境相关
        _brotli = None

# ---- 协议常量 ------------------------------------------------------------
HEADER_LEN = 16
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

DANMU_MSG_CMD = "DANMU_MSG"  # 普通弹幕的 cmd 前缀（也可能是 DANMU_MSG:4:0:2:2:2:0）

HEARTBEAT_INTERVAL = 30.0
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0

DANMU_INFO_API = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
# 2026-11 起 getDanmuInfo 不带 wbi 签名会直接吃风控 code=-352（本机实测：带签名 -> code=0）
WBI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_COOKIE_FILE = Path(r"D:\DeepseekWork\config\bilibili.txt")
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "tmp"

_WARNED: set[str] = set()


def _warn_once(log: logging.Logger | None, key: str, msg: str, *args: Any) -> None:
    if key in _WARNED or log is None:
        return
    _WARNED.add(key)
    log.warning(msg, *args)


# ---- 协议编解码 ----------------------------------------------------------
def _wbi_key_from_url(url: Any) -> str:
    """从 wbi_img 的 url 里取文件名主体（不带扩展名）当密钥片段。"""
    name = str(url or "").rsplit("/", 1)[-1]
    return name.split(".")[0]


def wbi_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in WBI_MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def wbi_sign(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    """给请求参数补 wts + w_rid（B 站 wbi 签名，参数按 key 排序后 md5）。"""
    signed = dict(params)
    signed["wts"] = int(time.time())
    items = sorted(signed.items())
    query = urllib.parse.urlencode(
        [(k, "".join(ch for ch in str(v) if ch not in "!'()*")) for k, v in items]
    )
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def make_packet(op: int, body: bytes = b"", *, protover: int = 1, seq: int = 1) -> bytes:
    """拼一个 B 站弹幕协议包：4B 长度 + 2B 头长 + 2B 版本 + 4B 操作码 + 4B 序号 + body。"""
    header = struct.pack(">IHHII", HEADER_LEN + len(body), HEADER_LEN, protover, op, seq)
    return header + body


def iter_packets(payload: bytes, log: logging.Logger | None = None) -> Iterator[tuple[int, bytes]]:
    """把一段字节流切成 (op, body)；protover=2/3 的包体先解压再递归切分。"""
    offset = 0
    total = len(payload)
    while offset + HEADER_LEN <= total:
        packet_len, header_len, protover, op, _seq = struct.unpack(
            ">IHHII", payload[offset : offset + HEADER_LEN]
        )
        if packet_len < HEADER_LEN or offset + packet_len > total:
            if log is not None:
                log.debug("包长度异常，停止解析本段（offset=%d, packet_len=%d, total=%d）", offset, packet_len, total)
            return
        body = payload[offset + header_len : offset + packet_len]
        offset += packet_len
        if protover == 2:
            try:
                body = zlib.decompress(body)
            except zlib.error as exc:
                _warn_once(log, "zlib", "zlib 解压失败（%s），跳过该包", exc)
                continue
            yield from iter_packets(body, log)
        elif protover == 3:
            if _brotli is None:
                _warn_once(
                    log,
                    "brotli",
                    "收到 protover=3（brotli）数据但没装 brotli/brotlicffi，无法解压；"
                    "请用 --protover 2（默认）或 pip install brotli",
                )
                continue
            try:
                body = _brotli.decompress(body)
            except Exception as exc:  # brotli 各种实现异常类型不同
                _warn_once(log, "brotli-err", "brotli 解压失败（%s: %s），跳过该包", type(exc).__name__, exc)
                continue
            yield from iter_packets(body, log)
        else:
            yield op, body


@dataclass(frozen=True)
class Danmaku:
    """一条普通弹幕。"""

    user: str
    text: str

    def __str__(self) -> str:
        return "%s：%s" % (self.user, self.text)


def parse_danmaku(payload: dict[str, Any]) -> Danmaku | None:
    """从 op=5 的 JSON 里取 DANMU_MSG；不是弹幕（或格式不认识）返回 None。"""
    if not isinstance(payload, dict):
        return None
    cmd = str(payload.get("cmd") or "")
    if cmd.split(":")[0] != DANMU_MSG_CMD:
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 2:
        return None
    text = str(info[1] or "").strip()
    if not text:
        return None
    user = ""
    try:
        user = str(info[2][1] or "").strip()
    except (IndexError, TypeError):
        user = ""
    return Danmaku(user=user or "观众", text=text)


# ---- 日志 -----------------------------------------------------------------
def setup_logging(
    log_dir: Path | str = DEFAULT_LOG_DIR,
    *,
    verbose: bool = False,
    prefix: str = "danmaku",
) -> tuple[logging.Logger, Path]:
    """建 tmp/danmaku-YYYYMMDD.log 并返回 logger。只在 main() 里调用。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / ("%s-%s.log" % (prefix, time.strftime("%Y%m%d")))
    logger = logging.getLogger("ai-live.danmaku")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger, log_path
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8", errors="replace")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(file_handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger, log_path


def _configure_console() -> None:
    """管道输出用 UTF-8（避免 cp936 乱码），终端保持原编码但把不可编码字符替换掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---- 凭据 -----------------------------------------------------------------
def load_cookie(path: Path | str = DEFAULT_COOKIE_FILE, log: logging.Logger | None = None) -> str | None:
    """读 config/bilibili.txt。只记录文件与字节数，绝不记录内容。"""
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except FileNotFoundError:
        if log:
            log.warning("未找到凭据文件 %s，将以游客身份连接（部分房间可能收不到弹幕）", file_path)
        return None
    except OSError as exc:
        if log:
            log.warning("读取凭据文件失败（%s: %s），以游客身份连接", type(exc).__name__, exc)
        return None
    lines = [line.strip() for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith("#")]
    if not lines:
        if log:
            log.warning("凭据文件 %s 为空（%d 字节），以游客身份连接", file_path, len(raw))
        return None
    cookie = " ".join(lines)
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[len("cookie:") :].strip()
    if log:
        log.info("已读取 config/%s（%d 字节）", file_path.name, len(raw))
    return cookie or None


# ---- 弹幕客户端 -----------------------------------------------------------
class BiliDanmakuClient:
    """getDanmuInfo -> ws 鉴权 -> 心跳 -> 收包；断线指数退避重连（上限 60s）。"""

    def __init__(
        self,
        room_id: str | int,
        cookie: str | None = None,
        *,
        protover: int = 2,
        logger: logging.Logger | None = None,
        on_danmaku: Callable[[Danmaku], Any] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        session: Any = None,
    ) -> None:
        self.room_id = str(room_id)
        self.cookie = cookie
        self.protover = int(protover)
        self.log = logger or logging.getLogger("ai-live.danmaku")
        self.on_danmaku = on_danmaku
        self.heartbeat_interval = float(heartbeat_interval)
        self._session = session
        self._stopped = False
        self._wbi_mixin: str | None = None
        self._backoff = MIN_BACKOFF
        self.stats = {"danmaku": 0, "messages": 0, "reconnects": 0}

    # ---- HTTP：拿 token / host ---------------------------------------------
    def _api_headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": referer or "https://live.bilibili.com/%s" % self.room_id,
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _ensure_wbi_mixin(self, session: Any) -> str | None:
        """取 wbi 密钥（nav 接口的 img_key/sub_key）。拿不到就返回 None，退化为不签名。"""
        import aiohttp

        if self._wbi_mixin:
            return self._wbi_mixin
        try:
            async with session.get(
                WBI_NAV_API,
                headers=self._api_headers(referer="https://www.bilibili.com/"),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                payload = await resp.json(content_type=None)
        except Exception as exc:  # 网络抖动 / 接口变更都不致命
            self.log.warning("取 wbi 密钥失败（%s: %s），本次不带签名请求", type(exc).__name__, exc)
            return None
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_key = _wbi_key_from_url(wbi_img.get("img_url"))
        sub_key = _wbi_key_from_url(wbi_img.get("sub_url"))
        if not img_key or not sub_key:
            self.log.warning("nav 接口没返回 wbi_img（未登录也可能正常），本次不带签名请求")
            return None
        self._wbi_mixin = wbi_mixin_key(img_key, sub_key)
        self.log.info("已取得 wbi 签名密钥（img/sub 各 %d 位），getDanmuInfo 将带 w_rid", len(img_key))
        return self._wbi_mixin

    async def _fetch_danmu_info(self, session: Any) -> dict[str, Any]:
        import aiohttp

        headers = self._api_headers()
        base_params: dict[str, Any] = {"id": self.room_id, "type": 0, "web_location": "444.8"}
        mixin = await self._ensure_wbi_mixin(session)
        last_code: Any = None
        last_msg: Any = None
        for attempt in (1, 2):
            params = wbi_sign(base_params, mixin) if mixin else dict(base_params)
            try:
                async with session.get(
                    DANMU_INFO_API,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            except aiohttp.ClientError as exc:
                raise RuntimeError("请求 getDanmuInfo 失败（%s: %s）" % (type(exc).__name__, exc)) from exc
            if status != 200:
                raise RuntimeError("getDanmuInfo 返回 HTTP %s" % status)
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise RuntimeError("getDanmuInfo 返回的不是 JSON") from exc
            code = payload.get("code")
            if code == 0:
                data = payload.get("data") or {}
                real_room = data.get("room_id")
                if real_room:
                    self.room_id = str(real_room)
                self.log.info(
                    "getDanmuInfo OK：token %d 字符，host %d 个",
                    len(str(data.get("token") or "")),
                    len(data.get("host_list") or []),
                )
                return data
            last_code, last_msg = code, payload.get("message")
            if code == -352 and attempt == 1:
                # -352 = 风控：多半是 wbi 密钥过期，刷新后重试一次
                self.log.warning("getDanmuInfo 返回 -352（风控校验失败），刷新 wbi 密钥后重试一次")
                self._wbi_mixin = None
                mixin = await self._ensure_wbi_mixin(session)
                continue
            break
        hint = ""
        if str(last_code) == "-352":
            hint = "；-352 通常是 IP 被风控或 cookie 过期，请在浏览器里重新登录 B 站后把新 cookie 写到 config/bilibili.txt"
        raise RuntimeError("getDanmuInfo 接口报错 code=%s message=%s%s" % (last_code, last_msg, hint))

    async def _resolve_room_id(self, session: Any) -> None:
        """把短号换成真实房间号（auth body 里用真实号更稳）；失败就用原号。"""
        import aiohttp

        try:
            headers = {"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/"}
            async with session.get(
                ROOM_INFO_API,
                params={"room_id": self.room_id},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                payload = await resp.json(content_type=None)
            if payload.get("code") == 0:
                real = (payload.get("data") or {}).get("room_id")
                if real and str(real) != self.room_id:
                    self.log.info("房间号 %s -> 真实房间号 %s", self.room_id, real)
                    self.room_id = str(real)
        except Exception as exc:  # 换号失败不影响主流程
            self.log.debug("解析真实房间号失败（%s），沿用 %s", exc, self.room_id)

    # ---- 单次连接 -----------------------------------------------------------
    async def _send_auth(self, ws: Any, token: str) -> None:
        body = {
            "uid": 0,
            "roomid": int(self.room_id) if str(self.room_id).isdigit() else self.room_id,
            "protover": self.protover,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        await ws.send_bytes(make_packet(OP_AUTH, json.dumps(body).encode("utf-8")))

    async def _heartbeat(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                if getattr(ws, "closed", False):
                    return
                await ws.send_bytes(make_packet(OP_HEARTBEAT, b""))
                self.log.debug("心跳已发送")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.debug("心跳线程退出（%s: %s）", type(exc).__name__, exc)

    async def _handle_packet(self, op: int, body: bytes) -> None:
        if op == OP_AUTH_REPLY:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                payload = {}
            code = payload.get("code")
            if code not in (0, None):
                raise RuntimeError("弹幕服务器鉴权失败 code=%s" % code)
            self.log.info("鉴权成功（code=%s），开始接收弹幕", code)
            self._backoff = MIN_BACKOFF
            return
        if op == OP_HEARTBEAT_REPLY:
            popularity = int.from_bytes(body, "big") if body else 0
            self.log.debug("心跳回执，人气值 %s", popularity)
            return
        if op != OP_MESSAGE:
            self.log.debug("收到未处理的操作码 op=%s（%d 字节）", op, len(body))
            return
        self.stats["messages"] += 1
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            self.log.debug("op=5 包体不是 JSON，跳过（%d 字节）", len(body))
            return
        danmaku = parse_danmaku(payload)
        if danmaku is None:
            return
        self.stats["danmaku"] += 1
        self.log.info("弹幕 %s", danmaku)
        if self.on_danmaku is not None:
            result = self.on_danmaku(danmaku)
            if asyncio.iscoroutine(result):
                await result

    async def _handle_binary(self, data: bytes) -> None:
        for op, body in iter_packets(data, self.log):
            await self._handle_packet(op, body)

    async def _serve(self, ws: Any, token: str) -> None:
        import aiohttp

        await self._send_auth(ws, token)
        self.log.info("已发送鉴权包（roomid=%s, protover=%s）", self.room_id, self.protover)
        heartbeat = asyncio.create_task(self._heartbeat(ws), name="heartbeat")
        self.stats["connected"] = self.stats.get("connected", 0) + 1
        try:
            while True:
                msg = await ws.receive()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_binary(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    self.log.debug("收到文本帧 %d 字节：%s", len(msg.data), str(msg.data)[:120])
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    self.log.warning(
                        "服务端关闭连接：close_code=%s data=%s（常见：鉴权被拒 / 房间号不存在 / IP 风控）",
                        ws.close_code,
                        msg.data,
                    )
                    raise ConnectionError("WebSocket 已关闭（close_code=%s）" % ws.close_code)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self.log.warning("WebSocket 异常（%s）：%s", msg.type.name, ws.exception())
                    raise ConnectionError("WebSocket 异常（%s）" % msg.type.name)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_once(self, session: Any) -> None:
        import aiohttp

        await self._resolve_room_id(session)
        info = await self._fetch_danmu_info(session)
        token = info.get("token") or ""
        if not token:
            raise RuntimeError("getDanmuInfo 没返回 token（直播间可能未开播或房间号错误）")
        hosts = [
            (item.get("host"), int(item.get("wss_port") or 443))
            for item in (info.get("host_list") or [])
            if item.get("host")
        ]
        if not hosts:
            raise RuntimeError("getDanmuInfo 没返回可用 host")
        last_error: Exception | None = None
        for host, port in hosts[:3]:
            url = "wss://%s:%d/sub" % (host, port)
            self.log.info("连接弹幕服务器 %s", url)
            try:
                async with session.ws_connect(
                    url,
                    heartbeat=None,
                    compress=0,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as ws:
                    await self._serve(ws, token)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self.log.warning("host %s 连接中断（%s: %s）", host, type(exc).__name__, exc)
        raise RuntimeError("所有弹幕 host 都连不上：%s" % last_error)

    # ---- 对外主循环 ---------------------------------------------------------
    async def run(self, session: Any = None) -> None:
        import aiohttp

        own_session = session is None and self._session is None
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        try:
            while not self._stopped:
                started = time.monotonic()
                try:
                    await self._run_once(self._session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log.warning("连接出问题：%s: %s", type(exc).__name__, exc)
                if self._stopped:
                    break
                if time.monotonic() - started >= 10.0:
                    self._backoff = MIN_BACKOFF
                wait = min(self._backoff, MAX_BACKOFF)
                self.stats["reconnects"] += 1
                self.log.info("%.0f 秒后重连（指数退避，上限 %.0f 秒）", wait, MAX_BACKOFF)
                await asyncio.sleep(wait)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
        finally:
            if own_session and self._session is not None:
                await self._session.close()
                self._session = None

    def stop(self) -> None:
        self._stopped = True


# ---- 回复编排 -------------------------------------------------------------
async def handle_danmaku(
    danmaku: Danmaku,
    *,
    engine: ReplyEngine,
    say: SayClient,
    log: logging.Logger,
) -> bool:
    """一条弹幕的完整处理链路：生成回复 -> 投递 /say。真实与 mock 共用。"""
    decision = await engine.generate(danmaku.user, danmaku.text)
    if decision.text is None:
        log.info("跳过 %s：%s", danmaku.user, decision.reason)
        return False
    log.info("回复 %s（%s）：%s", danmaku.user, decision.source, decision.text)
    return await say.say(decision.text)


MOCK_DANMAKU: tuple[tuple[str, str], ...] = (
    ("小电视", "主播你好，今天播什么呀？"),
    ("早八人", "这首歌叫什么名字"),
    ("弹幕护体", "6666666"),
    ("弹幕护体", "6666666"),
    ("路人甲", "加群领福利，私我发码"),
)


async def run_mock(args: argparse.Namespace, log: logging.Logger) -> int:
    """离线回放：不连网络，走与真机完全相同的回复 + 投递链路。"""
    log.info("MOCK 模式：回放 %d 条内置样例弹幕（不连 B 站）", len(MOCK_DANMAKU))
    engine = build_engine(
        min_interval=args.min_interval,
        max_len=args.max_len,
        use_llm=not args.no_llm,
        logger=log,
    )
    say = SayClient(
        args.say_url,
        retries=args.say_retries,
        dry_run=args.dry_run,
        logger=log,
    )
    sent = skipped = 0
    try:
        for index, (user, text) in enumerate(MOCK_DANMAKU, 1):
            danmaku = Danmaku(user=user, text=text)
            log.info("[mock %d/%d] 收到弹幕 %s", index, len(MOCK_DANMAKU), danmaku)
            ok = await handle_danmaku(danmaku, engine=engine, say=say, log=log)
            if ok:
                sent += 1
            else:
                skipped += 1
            if index < len(MOCK_DANMAKU):
                # dry-run 没有真实 /say 的耗时，这里补足限速间隔，保证演示节奏与真机一致
                gap = args.mock_interval if not args.dry_run else max(args.mock_interval, args.min_interval)
                await asyncio.sleep(max(0.0, gap))
    finally:
        await say.close()
        close_llm = getattr(engine.llm, "close", None)
        if close_llm is not None:
            await close_llm()
    log.info("MOCK 结束：投递成功 %d 条，跳过/失败 %d 条", sent, skipped)
    if say.sent_fail:
        log.warning("其中有 %d 条投递失败（看上面 /say 的报错）", say.sent_fail)
    return 0


async def run_live(args: argparse.Namespace, log: logging.Logger) -> int:
    """真机模式：连 B 站弹幕 -> 队列 -> 逐条回复投递。"""
    room = str(args.room).strip()
    if not room:
        log.error("真机模式必须给房间号：--room <房间号>（或先跑 --mock 离线联调）")
        return 2
    cookie = load_cookie(args.cookie_file, log)
    engine = build_engine(
        min_interval=args.min_interval,
        max_len=args.max_len,
        use_llm=not args.no_llm,
        logger=log,
    )
    say = SayClient(
        args.say_url,
        retries=args.say_retries,
        dry_run=args.dry_run,
        logger=log,
    )
    queue: asyncio.Queue[Danmaku] = asyncio.Queue(maxsize=200)

    pending_show: set[asyncio.Task] = set()

    async def on_danmaku(danmaku: Danmaku) -> None:
        if args.show_danmaku:
            # 浮层展示走独立 task，不阻塞弹幕解析；积压太多就放弃展示
            if len(pending_show) < 20:
                task = asyncio.create_task(say.show_danmaku(danmaku.user, danmaku.text))
                pending_show.add(task)
                task.add_done_callback(pending_show.discard)
        if queue.full():
            log.warning("回复队列已满，丢弃一条弹幕：%s", danmaku.user)
            return
        await queue.put(danmaku)

    async def worker() -> None:
        while True:
            danmaku = await queue.get()
            try:
                await handle_danmaku(danmaku, engine=engine, say=say, log=log)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("处理弹幕出错（%s: %s）", type(exc).__name__, exc)
            finally:
                queue.task_done()

    client = BiliDanmakuClient(
        room,
        cookie,
        protover=args.protover,
        logger=log,
        on_danmaku=on_danmaku,
    )
    worker_task = asyncio.create_task(worker(), name="reply-worker")
    log.info("目标直播间：%s（A 线 /say：%s）", room, args.say_url)
    duration = float(getattr(args, "duration", 0.0) or 0.0)
    try:
        if duration > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client.run(), timeout=duration)
            log.info("已跑满 --duration %.0f 秒，主动断开连接（冒烟测试）", duration)
        else:
            await client.run()
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await say.close()
        close_llm = getattr(engine.llm, "close", None)
        if close_llm is not None:
            await close_llm()
        log.info(
            "收尾统计：弹幕 %d 条，消息包 %d 个，投递成功 %d 条 / 失败 %d 条",
            client.stats["danmaku"],
            client.stats["messages"],
            say.sent_ok,
            say.sent_fail,
        )
    return 0


# ---- CLI ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="danmaku_client.py",
        description="B 站直播弹幕 -> AI 回复 -> A 线 /say（B 线）",
    )
    parser.add_argument("--room", help="B 站直播间房间号（真机模式必填；短号/长号都行）")
    parser.add_argument("--mock", action="store_true", help="离线联调：回放内置样例弹幕，不连网络")
    parser.add_argument(
        "--say-url",
        default=default_say_url(),
        help="A 线播报接口（默认 %s）" % DEFAULT_SAY_URL,
    )
    parser.add_argument(
        "--cookie-file",
        default=os.environ.get("AI_LIVE_COOKIE_FILE") or str(DEFAULT_COOKIE_FILE),
        help="凭据文件路径（默认 config/bilibili.txt，只读、不回显）",
    )
    parser.add_argument(
        "--protover",
        type=int,
        default=int(os.environ.get("AI_LIVE_PROTOVER") or 2),
        choices=(0, 2, 3),
        help="弹幕协议版本：2=zlib（默认最稳），3=brotli（需 brotli 库），0=明文",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help="全局回复最小间隔秒数（真机默认 3.0；mock 默认 0.5，否则 5 条样例大多被限速吞掉）",
    )
    parser.add_argument("--max-len", type=int, default=60, help="回复字数上限（默认 60）")
    parser.add_argument("--say-retries", type=int, default=2, help="/say 失败重试次数（默认 2）")
    parser.add_argument("--mock-interval", type=float, default=0.2, help="mock 回放间隔秒数（默认 0.2）")
    parser.add_argument("--dry-run", action="store_true", help="只打印回复，不真的 POST /say")
    parser.add_argument(
        "--show-danmaku",
        action="store_true",
        help="把每条弹幕额外投到 A 线浮层 POST /danmaku（只展示不播报；需 A 线支持该接口）",
    )
    parser.add_argument("--no-llm", action="store_true", help="即使找到 key 也强制走规则回复")
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="真机模式跑多少秒后主动退出（默认 0=一直跑；冒烟测试用）",
    )
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="日志目录（默认 code/ai-live/tmp）")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    log, log_path = setup_logging(args.log_dir, verbose=args.verbose)
    if args.min_interval is None:
        # mock 只是回放演示，按真机的 3 秒限速会把 5 条样例吞掉 4 条
        args.min_interval = 0.5 if args.mock else 3.0
    log.info("B 线弹幕机器人启动（日志：%s）", log_path)
    log.info(
        "模式：%s｜protover=%s｜限速 %.1fs｜回复上限 %d 字｜LLM=%s",
        "mock（离线回放）" if args.mock else "live（真机）",
        args.protover,
        args.min_interval,
        args.max_len,
        "关闭" if args.no_llm else "自动探测",
    )
    if args.protover == 3 and _brotli is None:
        log.error("protover=3 需要 brotli/brotlicffi 库，本机没装；请用 --protover 2（默认）")
        return 2
    if args.mock:
        return await run_mock(args, log)
    return await run_live(args, log)


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        logging.getLogger("ai-live.danmaku").info("收到 Ctrl+C，已干净退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
