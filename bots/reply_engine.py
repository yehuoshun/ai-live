#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回复生成引擎（B 线）。

默认走规则版：关键词命中表 + 兜底模板（随机化，避免同一规则连续用同一句话）。
若环境变量或 config/ 下能找到 LLM key，则自动启用 LLM 回复；任何调用失败都会
静默降级回规则版，绝不因为 LLM 挂掉而漏回。

内置约束：
  * 全局限速：默认每 3 秒最多回 1 条（min_interval）；
  * 同一用户去重：同一用户同一句话在 dedupe_window（默认 60s）内只回一次；
  * 同一用户冷却：user_cooldown（默认 5s），避免一个刷屏用户占满限速；
  * 回复长度上限 max_len（默认 60 字，超了截断加省略号）；
  * 黑名单词过滤：入站弹幕命中不回，出站回复命中整条丢弃。

导入本模块无副作用（不建日志、不读凭据、不发请求）。
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

MAX_REPLY_LEN = 60
DEFAULT_MIN_INTERVAL = 3.0
DEFAULT_DEDUPE_WINDOW = 60.0
DEFAULT_USER_COOLDOWN = 5.0
DEFAULT_CONFIG_DIR = Path(r"D:\DeepseekWork\config")
DEFAULT_GUEST_NAME = "观众"

# 命中即不回（广告 / 引流 / 违规）；大小写不敏感
BLACKLIST_WORDS: tuple[str, ...] = (
    "加群", "私聊", "私我", "领福利", "免费领", "扫码", "优惠码", "代充",
    "刷单", "月入", "兼职", "外挂", "开挂", "赌博", "博彩", "裸聊",
    "加v", "加qq", "加微信", "v我", "带赚",
)

# (关键词组, 候选回复)；按书写顺序匹配，越具体的越靠前
KEYWORD_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("你好", "您好", "hi", "hello", "嗨", "哈喽", "在吗", "晚上好", "早上好", "下午好"),
        (
            "你好呀{name}，欢迎来直播间～",
            "嗨{name}，我在的，今天想聊点什么？",
            "欢迎欢迎，{name}随便聊，我听着呢",
        ),
    ),
    (
        ("666", "牛", "厉害", "好强", "太强", "棒", "赞", "爱了", "好看", "好听"),
        (
            "谢谢{name}！被夸到了，我嘴角压不住了",
            "哈哈{name}过奖，我再努力营业一下",
            "{name}这句夸得我 CPU 都暖了",
        ),
    ),
    (
        ("歌", "bgm", "音乐", "曲子", "唱"),
        (
            "{name}问的是 BGM 吧？这首我也挺喜欢的",
            "歌名我回头确认一下再告诉你{name}",
            "{name}耳朵真尖，这段是我特意挑的",
        ),
    ),
    (
        ("你是谁", "叫什么", "名字", "怎么称呼", "真人", "机器人", "是ai", "是 AI"),
        (
            "我是这间直播间的 AI 分身，{name}可以随便叫我",
            "一个会接弹幕的 AI 主播，{name}多指教",
            "{name}好，我是皮套后面那个 AI，负责陪聊",
        ),
    ),
    (
        ("下播", "再见", "拜拜", "晚安", "走了", "下次见"),
        (
            "{name}慢走～下次开播记得回来",
            "晚安{name}，路上小心",
            "拜拜{name}，我在这儿等你",
        ),
    ),
    (
        ("关注", "点赞", "三连", "投喂"),
        (
            "谢谢{name}，关注了我下次开播你就能收到提醒",
            "{name}这一手太够意思了",
            "收到{name}的支持，我记小本本上了",
        ),
    ),
    (
        ("上班", "工作", "加班", "下班", "打工人"),
        (
            "{name}也别太拼，累了就来直播间坐会儿",
            "打工人共鸣了，{name}今天辛苦了",
            "{name}先喝口水，工作的事明天再说",
        ),
    ),
    (
        ("游戏", "上分", "开黑", "玩什么"),
        (
            "{name}想看我打游戏？我手可能有点抖",
            "游戏我菜，但{name}可以教我",
            "{name}说的这游戏我记下了，回头研究研究",
        ),
    ),
    (
        ("吃", "饿", "外卖", "晚饭", "午饭", "宵夜"),
        (
            "{name}一说我也饿了，可惜我吃不了",
            "{name}记得吃点好的，别老对付",
            "宵夜警告，{name}你这是在馋我",
        ),
    ),
)

FALLBACK_TEMPLATES: tuple[str, ...] = (
    "收到～{name}说的这个我记下了",
    "嗯嗯，{name}这话有道理",
    "哈哈，{name}真会聊",
    "{name}说得对，我也是这么想的",
    "这个话题有意思，{name}再多说两句？",
    "我在听呢，{name}继续～",
    "{name}这条我看到了，先给你点个赞",
)

_TRAILING_QUOTES = ('"', "'", "“", "”", "「", "」")


def normalize_text(text: str) -> str:
    """压掉换行 / 多空格，去掉首尾空白。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def truncate_reply(text: str, limit: int = MAX_REPLY_LEN) -> str:
    """按字符数截断（超长加省略号），保证不超过 limit。"""
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def contains_blacklist(text: str, words: Sequence[str] = BLACKLIST_WORDS) -> str | None:
    """返回命中的黑名单词；没命中返回 None。"""
    low = (text or "").lower()
    for word in words:
        if word.lower() in low:
            return word
    return None


@dataclass(frozen=True)
class ReplyDecision:
    """一次回复决策的结果。text 为 None 表示这条弹幕不回。"""

    text: str | None
    reason: str = "ok"
    source: str = "rule"  # rule / llm

    @property
    def skipped(self) -> bool:
        return self.text is None


class ReplyEngine:
    """规则版回复引擎，可选 LLM。"""

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_len: int = MAX_REPLY_LEN,
        dedupe_window: float = DEFAULT_DEDUPE_WINDOW,
        user_cooldown: float = DEFAULT_USER_COOLDOWN,
        blacklist: Sequence[str] = BLACKLIST_WORDS,
        llm: Any = None,
        logger: Any = None,
        rng: random.Random | None = None,
        clock=time.monotonic,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.max_len = max(1, int(max_len))
        self.dedupe_window = max(0.0, float(dedupe_window))
        self.user_cooldown = max(0.0, float(user_cooldown))
        self.blacklist = tuple(blacklist)
        self.llm = llm
        self.log = logger
        self.rng = rng or random.Random()
        self._clock = clock
        self._last_sent_at = float("-inf")
        self._seen: dict[tuple[str, str], float] = {}
        self._user_last: dict[str, float] = {}
        self._last_by_rule: dict[str, str] = {}

    # ---- 闸门（限速 / 去重 / 冷却 / 黑名单） --------------------------------
    def gate(self, user: str, text: str, now: float | None = None) -> str | None:
        """放行返回 None，否则返回拒绝原因（给人看的中文短语）。"""
        now = self._clock() if now is None else now
        clean = normalize_text(text)
        if not clean:
            return "空弹幕"
        hit = contains_blacklist(clean, self.blacklist)
        if hit:
            return "命中黑名单词「%s」" % hit
        key = (user or "", clean.lower())
        if now - self._seen.get(key, float("-inf")) < self.dedupe_window:
            return "同一条弹幕重复（%.0fs 内去重）" % self.dedupe_window
        if now - self._user_last.get(user or "", float("-inf")) < self.user_cooldown:
            return "同一用户冷却中（%.0fs）" % self.user_cooldown
        if now - self._last_sent_at < self.min_interval:
            return "全局限速中（每 %.1fs 最多 1 条）" % self.min_interval
        return None

    # ---- 规则生成 -----------------------------------------------------------
    def rule_reply(self, text: str, name: str = DEFAULT_GUEST_NAME) -> str:
        low = (text or "").lower()
        for keywords, replies in KEYWORD_RULES:
            for keyword in keywords:
                if keyword.lower() in low:
                    return self._pick(keyword, replies).format(name=name)
        return self._pick("__fallback__", FALLBACK_TEMPLATES).format(name=name)

    def _pick(self, key: str, options: Sequence[str]) -> str:
        last = self._last_by_rule.get(key)
        pool = [item for item in options if item != last] or list(options)
        choice = self.rng.choice(pool)
        self._last_by_rule[key] = choice
        return choice

    # ---- 对外主入口 ---------------------------------------------------------
    async def generate(self, user: str, text: str) -> ReplyDecision:
        now = self._clock()
        reason = self.gate(user, text, now)
        if reason:
            return ReplyDecision(None, reason)
        name = normalize_text(user)[:8] or DEFAULT_GUEST_NAME
        reply: str | None = None
        source = "rule"
        if self.llm is not None:
            try:
                reply = await self.llm.reply(name, normalize_text(text))
                if reply:
                    source = "llm"
            except Exception as exc:  # 任何 LLM 故障都降级，不影响直播
                if self.log:
                    self.log.warning("LLM 调用失败（%s: %s），降级为规则回复", type(exc).__name__, exc)
                reply = None
        if not reply:
            reply = self.rule_reply(text, name)
            source = "rule"
        hit = contains_blacklist(reply, self.blacklist)
        if hit:
            if self.log:
                self.log.warning("生成的回复命中黑名单词「%s」，整条丢弃", hit)
            return ReplyDecision(None, "回复命中黑名单词「%s」" % hit)
        reply = truncate_reply(reply, self.max_len)
        self._last_sent_at = now
        self._seen[(user or "", normalize_text(text).lower())] = now
        self._user_last[user or ""] = now
        self._prune(now)
        return ReplyDecision(reply, "ok", source)

    def _prune(self, now: float) -> None:
        if len(self._seen) > 800:
            for key, ts in list(self._seen.items()):
                if now - ts > self.dedupe_window:
                    self._seen.pop(key, None)
        if len(self._user_last) > 800:
            for key, ts in list(self._user_last.items()):
                if now - ts > max(self.user_cooldown, self.dedupe_window):
                    self._user_last.pop(key, None)


# --------------------------------------------------------------------------
# 可选 LLM：OpenAI 兼容 /chat/completions；没有 key 就不启用（自动降级规则版）
# --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你在 B 站直播间扮演一位亲切、简短、有梗的虚拟主播。"
    "请只输出一句中文回复，不超过 40 个字；不要加引号、不要解释、不要换行。"
)

_KEY_ENV_NAMES = ("AI_LIVE_LLM_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
_KEY_FILE_NAMES = ("llm_key.txt", "llm_api_key.txt", "deepseek_key.txt", "openai_key.txt")


def _extract_key(raw: str) -> str:
    """从 key 文件里取出真正的 key：支持纯 key / key=xxx / api_key: xxx。"""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            line = line.split("=", 1)[1].strip()
        elif ":" in line:
            line = line.split(":", 1)[1].strip()
        line = line.strip().strip('"').strip("'")
        if line:
            return line
    return ""


class LLMReplyGenerator:
    """最小可用的 OpenAI 兼容客户端。失败一律返回 None，由引擎降级规则版。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 8.0,
        logger: Any = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.log = logger
        self._session: Any = None

    @classmethod
    def from_env(
        cls,
        *,
        config_dir: Path | None = None,
        logger: Any = None,
    ) -> "LLMReplyGenerator | None":
        for env_name in _KEY_ENV_NAMES:
            value = (os.environ.get(env_name) or "").strip()
            if value:
                if logger:
                    logger.info("检测到环境变量 %s，启用 LLM 回复", env_name)
                return cls(value, logger=logger)
        directory = Path(config_dir) if config_dir else Path(
            os.environ.get("AI_LIVE_CONFIG_DIR") or DEFAULT_CONFIG_DIR
        )
        for file_name in _KEY_FILE_NAMES:
            path = directory / file_name
            try:
                if not path.is_file():
                    continue
                key = _extract_key(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if key:
                if logger:
                    logger.info("已读取 config/%s（%d 字节），启用 LLM 回复", file_name, len(key))
                return cls(key, logger=logger)
        if logger:
            logger.info("未找到 LLM key（环境变量 / config 均无），使用规则回复")
        return None

    async def reply(self, user: str, text: str) -> str | None:
        import aiohttp  # 延迟导入：没有 aiohttp 也能用规则版

        if self._session is None:
            self._session = aiohttp.ClientSession()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "观众【%s】说：%s" % (user, text)},
            ],
            "temperature": 0.9,
            "max_tokens": 80,
        }
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        try:
            async with self._session.post(
                self.base_url + "/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    if self.log:
                        self.log.warning("LLM 接口返回 HTTP %s，降级规则回复", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:
            if self.log:
                self.log.warning("LLM 请求异常（%s: %s），降级规则回复", type(exc).__name__, exc)
            return None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        content = normalize_text(str(content or ""))
        for mark in _TRAILING_QUOTES:
            content = content.strip(mark)
        return content or None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def build_engine(
    *,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    max_len: int = MAX_REPLY_LEN,
    use_llm: bool = True,
    logger: Any = None,
) -> ReplyEngine:
    """按配置装配引擎：能拿到 LLM key 就用 LLM，否则纯规则。"""
    llm = None
    if use_llm:
        llm = LLMReplyGenerator.from_env(logger=logger)
    return ReplyEngine(min_interval=min_interval, max_len=max_len, llm=llm, logger=logger)
