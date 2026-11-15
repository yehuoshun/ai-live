#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把回复文本投给 A 线本地服务：POST http://127.0.0.1:8788/say。

A 线接口契约：Content-Type application/json，body {"text": "...", "voice": 可选}；
返回 200 与 {"ok": true, "id": "..."}；文本上限 300 字（这边先截断，避免被服务端丢）。

行为：每条最多重试 2 次（共 3 次尝试）；全挂则丢弃并记 error（不抛异常、不拖垮收弹幕）。
连不上时打印清晰提示（A 线服务未启动 → 先跑 start.bat）。
"""

from __future__ import annotations

import asyncio
import os

import aiohttp

DEFAULT_SAY_URL = "http://127.0.0.1:8788/say"
DEFAULT_DANMAKU_URL = "http://127.0.0.1:8788/danmaku"
MAX_TEXT_LEN = 300
HINT_SERVICE_DOWN = (
    "A 线服务未启动，请先跑 code/ai-live/start.bat"
    "（或 node server.mjs），它应该监听 127.0.0.1:8788"
)


class SayClient:
    """/say 投递客户端：重试、超时、清晰的失败提示。"""

    def __init__(
        self,
        url: str = DEFAULT_SAY_URL,
        *,
        timeout: float = 5.0,
        retries: int = 2,
        retry_delay: float = 0.6,
        logger=None,
        dry_run: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.url = url or DEFAULT_SAY_URL
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.log = logger
        self.dry_run = bool(dry_run)
        self._session = session
        self._own_session = session is None
        self._down_warned = False
        self.sent_ok = 0
        self.sent_fail = 0
        self.shown_ok = 0
        # A 线的弹幕浮层接口（POST /danmaku {user,text}）：只展示、不念，失败不影响主链路
        self.danmaku_url = DEFAULT_DANMAKU_URL

    # ---- 内部 ---------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    def _log(self, level: str, msg: str, *args) -> None:
        if self.log is not None:
            getattr(self.log, level)(msg, *args)

    def _warn_down(self, exc: Exception, attempt: int, attempts: int) -> None:
        if not self._down_warned:
            self._down_warned = True
            self._log("error", "连不上 /say（%s: %s）", type(exc).__name__, exc)
            self._log("error", ">>> %s", HINT_SERVICE_DOWN)
        else:
            self._log("warning", "/say 仍然连不上（第 %d/%d 次）：%s", attempt, attempts, exc)

    # ---- 对外 ---------------------------------------------------------------
    async def say(self, text: str, voice: str | None = None) -> bool:
        """投递一条回复；返回是否投递成功。不抛异常。"""
        text = (text or "").strip()
        if not text:
            self._log("warning", "/say 收到空文本，跳过")
            return False
        if len(text) > MAX_TEXT_LEN:
            self._log("info", "回复超过 A 线 %d 字上限，先截断：%d 字", MAX_TEXT_LEN, len(text))
            text = text[:MAX_TEXT_LEN]
        if self.dry_run:
            self._log("info", "[dry-run] 未发送 /say：%s", text)
            self.sent_ok += 1
            return True

        payload: dict[str, str] = {"text": text}
        if voice:
            payload["voice"] = voice
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                session = await self._get_session()
                async with session.post(
                    self.url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        self._down_warned = False
                        self.sent_ok += 1
                        self._log("info", "/say 投递成功（第 %d/%d 次）：%s", attempt, attempts, text)
                        return True
                    self._log(
                        "warning",
                        "/say 返回 HTTP %s（第 %d/%d 次）：%s",
                        resp.status,
                        attempt,
                        attempts,
                        body[:200].replace("\n", " "),
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self._warn_down(exc, attempt, attempts)
            except Exception as exc:  # 兜底，别让投递问题打断收弹幕
                self._log("warning", "/say 未知异常（第 %d/%d 次）：%s: %s", attempt, attempts, type(exc).__name__, exc)
            if attempt < attempts:
                await asyncio.sleep(self.retry_delay * attempt)
        self.sent_fail += 1
        self._log("error", "/say 重试 %d 次仍失败，丢弃该条回复：%s", self.retries, text)
        return False

    async def show_danmaku(self, user: str, text: str) -> bool:
        """把一条弹幕投到 A 线浮层（只展示不播报）；失败只记 debug，不重试、不抛异常。"""
        if self.dry_run or not (text or "").strip():
            return False
        payload = {"user": (user or "观众")[:32], "text": (text or "")[:MAX_TEXT_LEN]}
        try:
            session = await self._get_session()
            async with session.post(
                self.danmaku_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status == 200:
                    self.shown_ok += 1
                    return True
                self._log("debug", "/danmaku 返回 HTTP %s", resp.status)
        except Exception as exc:  # 浮层只是锦上添花，绝不打断收弹幕
            self._log("debug", "/danmaku 投递失败（%s: %s）", type(exc).__name__, exc)
        return False

    async def close(self) -> None:
        if self._session is not None and self._own_session and not self._session.closed:
            await self._session.close()
        self._session = None


def default_say_url() -> str:
    return os.environ.get("AI_LIVE_SAY_URL") or DEFAULT_SAY_URL
