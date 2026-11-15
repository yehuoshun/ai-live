# bots/ —— B 线：B 站弹幕接入 + 回复生成 + 投递 A 线

这条线负责把**直播间的真实弹幕**变成**皮套能念出来的回复**：收弹幕 → 生成回复 → POST 给 A 线 `/say`。

## 文件

| 文件 | 职责 |
|---|---|
| `danmaku_client.py` | 主入口。拿 token（getDanmuInfo，带 wbi 签名）→ 连 ws → 鉴权 → 心跳 → 解包 → 解析 `DANMU_MSG` → 回复 → 投递；断线指数退避重连（1→60s）；`--mock` 离线回放 |
| `reply_engine.py` | 规则回复引擎：关键词命中表 + 兜底模板（随机化），带 3 秒全局限速、同用户同句去重、同用户冷却、60 字上限、黑名单过滤；预留 LLM 接口（有 key 就走 LLM，失败自动降级规则） |
| `say_client.py` | 投递 A 线：`POST /say`（重试 2 次后丢弃），可选 `POST /danmaku`（浮层只展示不念）；连不上时打印「A 线服务未启动，请先跑 start.bat」 |
| `selftest_protocol.py` | 离线自检（不联网、不读凭据）：包结构/解包/解析 + 限速/去重/黑名单/截断 |

## 跑法

```powershell
# 离线联调（不连 B 站，回放 5 条内置弹幕，走完整回复 + 投递链路）
python bots\danmaku_client.py --mock

# 只看链路不真的投递
python bots\danmaku_client.py --mock --dry-run

# 真机（房间号必填；凭据默认读 D:\DeepseekWork\config\bilibili.txt）
python bots\danmaku_client.py --room 123456

# 真机冒烟 30 秒后自动退出、并把弹幕送到皮套浮层
python bots\danmaku_client.py --room 123456 --duration 30 --show-danmaku

# 协议层自检
python bots\selftest_protocol.py
```

常用参数：`--room`、`--mock`、`--say-url`、`--cookie-file`、`--protover {0,2,3}`、`--min-interval`、`--max-len`、`--say-retries`、`--mock-interval`、`--duration`、`--show-danmaku`、`--dry-run`、`--no-llm`、`--log-dir`、`--verbose`。

日志：`code/ai-live/tmp/danmaku-YYYYMMDD.log`（`tmp/` 已被 .gitignore 覆盖）。Ctrl+C 干净退出。

## 与 A 线的接口

| 方法 | 路径 | body | 说明 |
|---|---|---|---|
| POST | `/say` | `{"text":"...","voice":"可选"}` | 进 TTS 播报队列，返回 `200 {"ok":true,"id":"..."}`，文本上限 300 字 |
| POST | `/danmaku` | `{"user":"昵称","text":"弹幕"}` | 皮套右侧浮层展示，**不进 TTS**；`--show-danmaku` 才启用 |

## B 站弹幕协议要点（实现依据）

1. `GET https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id=<房间号>&type=0&web_location=444.8`
   **必须带 wbi 签名**（`wts` + `w_rid`，密钥取自 `/x/web-interface/nav` 的 `wbi_img.img_url/sub_url` 文件名）。
   不带签名实测直接吃风控 `code=-352`；带上后 `code=0`、拿到 token 与 host_list。
   `-352` 时本实现会自动刷新密钥重试一次，仍失败则提示「IP 被风控或 cookie 过期」。
2. 连 `wss://<host>:<wss_port>/sub`（host_list 里前 3 个逐个试），**5 秒内**发鉴权包：
   op=7，body `{"uid":0,"roomid":<房间号>,"protover":2,"platform":"web","type":2,"key":"<token>"}`；否则服务端 5 秒后断开。
3. 之后每 30 秒发 op=2 心跳（body 空）；收 op=5 消息。
4. **protover=2（zlib）**：本机没装 brotli/brotlicffi，用标准库 `zlib.decompress` 解压最稳；
   解压后再按 **4 字节大端长度 + 16 字节头** 循环切分，一个包里可能有多条消息。
   `--protover 3`（brotli）需要另装 brotli，未装时启动直接报错退出。
5. `DANMU_MSG`：`cmd` 可能是 `DANMU_MSG:4:0:2:2:2:0`，按 `:` 前段匹配；内容 `info[1]`，用户名 `info[2][1]`。
6. 凭据：cookie 只从 `config/bilibili.txt` 读（单行、`;` 分隔的 cookie 串），代码与日志只记录文件名和字节数，不回显内容。

## 已通过的离线验收（2026-11-15）

- `python -c "import bots.danmaku_client"`：无副作用（不建日志、不连网）。
- `python bots/selftest_protocol.py`：13 项全 ok。
- `python bots/danmaku_client.py --mock`（A 线在跑）：投递 3 条、去重 1 条、黑名单 1 条。
- `--say-url http://127.0.0.1:8799/say`（死端口）：打印「A 线服务未启动，请先跑 start.bat」，重试 2 次后丢弃，退出码 0 不崩栈。

## 已知风险

- **真机 ws 连接在本机当前网络下被服务端重置**（2026-11-15 实测）：握手成功、`getDanmuInfo` 也成功，但客户端一发出任何帧（auth/心跳/纯文本/空帧）就在 ~0.1 秒内被 `1006` 断开；不发任何帧则活到服务端 5 秒鉴权超时。同一条链路里普通公网 websocket 回声正常。
  结论：包格式与代码没问题，是这条出口（中国移动家宽/4G 出口 IP）在 B 站弹幕 ws 上被拦。
  下一步：换一条网络（如手机热点 → 有线宽带）复测同一命令；仍不行就刷新浏览器 cookie 再试。
- 未做：发送到直播间的发言（只念给皮套听，不往 B 站发弹幕）；多房间并发；LLM key 尚未配置。
