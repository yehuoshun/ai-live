# 贡献指南

感谢有兴趣帮忙。这个项目目标很朴素：**一条能播起来的 AI 直播链路，零外部依赖、单机可跑**。
下面的活都有人要，挑一个开 issue 说一声，避免撞车。

## 先跑起来（5 分钟）

```
git clone https://github.com/yehuoshun/ai-live.git
cd ai-live
node server.mjs          # 或 Windows 双击 start.bat
```

浏览器开 <http://127.0.0.1:8788/puppet.html>，点「开始」解锁声音，点右上角「试说一句」。
需要：Node 22+、Python 3.10+、`pip install edge-tts`。

弹幕线（可选，需要 B 站 cookie 才能连真房间）：

```
python bots/selftest_protocol.py            # 离线自检，不联网
python bots/danmaku_client.py --mock        # 不用真弹幕，回放样例
python bots/danmaku_client.py --room <房间号> --show-danmaku
```

## 想请人帮忙的（按难度排）

1. **Live2D 适配器**（中）：现在皮套是 Canvas 自绘的占位美术。把 `public/puppet.js` 的绘制换成 `pixi-live2d-display` + Cubism 4 模型，口型把 RMS 映射到 `ParamMouthOpenY`，眨眼/呼吸/头部微动交给模型自带动画。代码里已留 `PuppetAdapter` 口子。
2. **长文本自动切句**（易）：现在单条上限 300 字，超了要调用方自己切。在 `server.mjs` 的 `/say` 里按标点切句、串行入队、顺序播放即可。
3. **弹幕回复接本地 LLM**（中）：`bots/reply_engine.py` 现在是规则版（9 组关键词 + 兜底模板），已留 LLM 钩子。任何本机可跑的模型（llama.cpp / Ollama / LM Studio）都行，要求不联网、超时可控、失败自动退回规则版。
4. **口型对齐音素**（难）：现在是音量包络，闭唇音会轻微不同步。做音素级对齐会明显更像人。
5. **OBS 一键配置**（易）：导出一份 `.json` 场景文件（浏览器源 1080×1350 + 透明 + 音频进 OBS），别人导入即用。
6. **更多 TTS 后端**（易）：`server.mjs` 的 `runTts()` 是唯一入口。GPT-SoVITS、IndexTTS、CosyVoice、Kokoro 都能接，保持 `POST /say` 的对外契约不变。

## 约定

- 提交信息用中文，格式 `范围: 改了什么(为什么)`，范围如 `server` / `puppet` / `bots` / `docs` / `chore`；一个 commit 只说一件事。
- 保持**零运行时依赖**：`server.mjs` 只用 Node 内置模块，`bots/` 只用 Python 标准库（`edge-tts` 是唯一外部依赖）。
- 不要提交凭据：B 站 cookie 放本地 `config/bilibili.txt` 或用 `--cookie-file` 指定；`.gitignore` 已排除 `tmp/`、`__pycache__/`、`*.log`。
- 开 PR 前跑一遍：`node server.mjs` 能起；`curl -X POST http://127.0.0.1:8788/say -H "Content-Type: application/json" -d '{"text":"测试"}'` 能出声；`python bots/selftest_protocol.py` 全过。
- 服务只监听 `127.0.0.1`，这条不要改成对外暴露。

## 许可

MIT，见 [LICENSE](LICENSE)。提交 PR 即表示同意以 MIT 方式发布。