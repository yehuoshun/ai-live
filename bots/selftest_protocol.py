#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 线离线自检：协议编解码 + 回复引擎闸门。不联网、不读凭据、不写日志。

跑法：python bots/selftest_protocol.py
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from danmaku_client import (  # noqa: E402
    DANMU_MSG_CMD,
    HEADER_LEN,
    _configure_console,
    OP_AUTH,
    OP_MESSAGE,
    make_packet,
    parse_danmaku,
)
from reply_engine import BLACKLIST_WORDS, ReplyEngine, truncate_reply  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, detail))


def test_packets() -> None:
    print("[协议] 打包 / 解包")
    raw = make_packet(OP_AUTH, b'{"uid":0}')
    check("长度字段 = 16 + body", struct.unpack(">I", raw[:4])[0] == HEADER_LEN + len(b'{"uid":0}'))
    check("op 字段可回读", struct.unpack(">I", raw[8:12])[0] == OP_AUTH)

    # protover=2：zlib 压缩后包体里再套 2 条消息体（真机就是这样）
    inner = make_packet(
        OP_MESSAGE,
        json.dumps({"cmd": DANMU_MSG_CMD, "info": [[0, 1, 25], "测试弹幕", [123, "观众甲"]]}).encode("utf-8"),
    ) + make_packet(OP_MESSAGE, json.dumps({"cmd": "INTERACT_WORD"}).encode("utf-8"))
    body = zlib.compress(inner)
    outer = struct.pack(">IHHII", HEADER_LEN + len(body), HEADER_LEN, 2, OP_MESSAGE, 1) + body

    from danmaku_client import iter_packets

    ops = list(iter_packets(outer))
    check("zlib 包能拆成 2 条", len(ops) == 2, "实际 %d" % len(ops))
    check("拆出的 op 都是 5", all(op == OP_MESSAGE for op, _ in ops))
    payload = json.loads(ops[0][1].decode("utf-8"))
    danmaku = parse_danmaku(payload)
    check("info[1] = 弹幕内容", danmaku is not None and danmaku.text == "测试弹幕")
    check("info[2][1] = 用户名", danmaku is not None and danmaku.user == "观众甲")
    check("非 DANMU_MSG 不算弹幕", parse_danmaku({"cmd": "SEND_GIFT"}) is None)


async def test_engine() -> None:
    print("[引擎] 限速 / 去重 / 黑名单 / 截断")
    engine = ReplyEngine(min_interval=3.0, user_cooldown=5.0, max_len=60)
    d1 = await engine.generate("甲", "主播你好")
    check("首条放行", d1.text is not None and d1.text.endswith("。") is False)
    d2 = await engine.generate("乙", "666")
    check("3 秒内第二条被限速", d2.text is None and "限速" in d2.reason, d2.reason)
    engine2 = ReplyEngine(min_interval=0.0, user_cooldown=0.0, dedupe_window=60.0)
    a = await engine2.generate("甲", "同一句话")
    b = await engine2.generate("甲", "同一句话")
    check("同一用户同一句去重", a.text is not None and b.text is None and "去重" in b.reason, b.reason or "")
    engine3 = ReplyEngine(min_interval=0.0, user_cooldown=0.0)
    c = await engine3.generate("甲", "加群领%s" % BLACKLIST_WORDS[0])
    check("黑名单弹幕不回", c.text is None and "黑名单" in c.reason)
    check("超长回复被截断到 60 字", len(truncate_reply("字" * 200, 60)) == 60)
    long_text = await engine3.generate("乙", "这" * 200)
    check("生成结果 <= 60 字", long_text.text is not None and len(long_text.text) <= 60, str(long_text.text))


def main() -> int:
    _configure_console()
    test_packets()
    asyncio.run(test_engine())
    print()
    if FAILS:
        print("自检失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
