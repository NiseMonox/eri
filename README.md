# Eri(艾莉)— 家庭 AI 助手 / おうちのAIアシスタント

跑在家庭服务器上的对话式健康秘书。她会提醒你、听你回话、帮你改期、催你、夸你、记账——
通过音箱说话(VOICEVOX)、iPhone 推送(Bark)、Telegram、Siri 快捷指令与你互动。

## 能做什么

- 🌙 **音频**:晚间助眠音循环、早晨渐强闹钟(mpv + ALSA,网页实时调音量)
- 🔊 **语音播报**:服药 / 提醒 / 记体重到点用ずんだもん音色播报;闹钟停止后报时+今日待办;静音时段自动闭嘴
- 💬 **对话式秘书**:说什么她都像聊天一样回你,同时自动识别意图、调用功能(记体重、建提醒、改期/完成/取消、看待办、曲线、周报),一句话里几件事一起办;「手头有点事,下午再去」→ 自动改期并再催;无回应自动追催到底
- 💊 **服药闭环**:提醒 → 点按/语音/按钮确认 → 未确认自动重发 → 超时标漏服(可补确认)
- ⚖️ **体重与体成分**:Withings 秤全自动同步(体重 + 体脂/肌肉/骨量/内脏脂肪/代谢年龄等 12 项),Siri/Telegram/网页手动记录,曲线图与 LLM 点评周报
- 🔔 **提醒**:一次性 / cron 重复;与 iPhone 提醒事项**双向同步**(快捷指令);体重同步到 Apple 健康
- 🧠 **LLM**:DeepSeek function calling(默认 `deepseek-flash`),每句话带上下文(开放事项 + 今后的提醒 + 近 3 天对话 + 长期记忆)决策;LLM 不可用时降级到正则快路径,核心提醒不受影响
- 📲 **推送**:Bark(时效性 / 重要警报可破静音)+ Telegram(双向,inline 按钮)

输出全日语(艾莉口吻),输入中日文都听得懂。

## 技术栈

Python 3.13 · FastAPI · APScheduler(croniter 单引擎)· SQLite(WAL,版本化原子迁移)· mpv ·
VOICEVOX(Docker)· bark-server(Docker)· python-telegram-bot · matplotlib · Jinja2 + Chart.js。
单进程,零前端构建,时间表存 DB、网页改完即生效。

## 快速开始

```bash
git clone <this repo> && cd health-hub
uv sync                        # 依赖
bash scripts/gen_media.sh      # 生成白噪音/占位闹钟音
cp .env.example .env           # 填 API_TOKEN 等
cd deploy/bark && docker compose up -d && cd ../..      # Bark 推送服务
cd deploy/voicevox && docker compose up -d && cd ../..  # 语音引擎(可选)
make deploy                    # systemd 上线(端口 8300)
```

- 网页:`http://<server>:8300`(Token 登录)
- 手机侧配置(Bark App、Telegram bot、Siri 快捷指令、Withings 授权、声卡直通):[deploy/notes.md](deploy/notes.md)
- 测试:`make test`;端到端冒烟:`scripts/e2e_smoke.sh`

## 架构一览

```
app/
├── scheduler/    # schedules 表 → APScheduler(croniter),sweeper(服药重发/提醒追催)
├── services/     # weights / meds 状态机 / reminders 实例化 / conversation 对话大脑 / intents / body_metrics
├── audio/        # manager(会话/渐强/播报共存) / mpv_player / tts(VOICEVOX + 缓存 + 静音)
├── notify/       # Bark + Telegram 统一入口
├── bot/          # Telegram long-polling + 正则兜底(LLM 不可用时)
├── llm/          # DeepSeek API(模型在 /settings 改)
├── ingest/       # Withings OAuth+全量轮询 / Health Auto Export
└── routers/      # API + 网页 + 短链回调
```

## 说明

- 个人项目、单用户、LAN 内使用;API 用单 token,不做多用户
- ずんだもん音声:个人私用无需标注;若公开发布生成音频请按 [VOICEVOX 规约](https://voicevox.hiroshiba.jp/term/) 标注「VOICEVOX:ずんだもん」
- 项目目录名 `health-hub` 是历史沿用,产品名为 Eri

## License

MIT
