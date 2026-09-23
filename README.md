# Eri(艾莉)— 家庭 AI 助手 / おうちのAIアシスタント

跑在家庭服务器上的对话式健康秘书。她会提醒你、听你回话、帮你改期、催你、夸你、记账——
通过音箱说话(VOICEVOX)、iPhone 推送(Bark)、Telegram、Siri 快捷指令、语音(按住说话)与你互动。

## 能做什么

- 🌙 **音频**:晚间助眠音循环、早晨渐强闹钟(mpv + ALSA,网页实时调音量;对话里一句话就能停、放、调音量);播报打断后从原处接着放
- 🔊 **语音播报**:服药 / 提醒 / 记体重到点用音箱播报(默认ずんだもん,可换成 Style-Bert-VITS2 自训音色);闹钟停止后报时+今日待办;静音时段自动闭嘴
- 💬 **对话式秘书**:说什么她都像聊天一样回你,同时自动识别意图、调用功能(记体重、建提醒、改期/完成/取消、看待办、曲线、周报),一句话里几件事一起办;「手头有点事,下午再去」→ 自动改期并再催;无回应自动追催到底
- 🎙️ **语音入口**:Telegram 语音消息,以及自己的 Windows / iPhone 客户端按住说话(`POST /api/voice/turn`)→ 本机识别(SenseVoice,中日英自动判别;按停顿切段,一句中文一句日语也认得出)→ 和文字同一个大脑 → 回复在家里音箱念(静音时段也念)。闹钟响着说「止めて」就停,DeepSeek 挂了也能停
- 💊 **服药闭环**:提醒 → 点按/语音/按钮确认 → 未确认自动重发 → 超时标漏服(可补确认)
- ⚖️ **体重与体成分**:Withings 秤全自动同步(体重 + 体脂/肌肉/骨量/内脏脂肪/代谢年龄等 12 项),Siri/Telegram/网页手动记录,曲线图与 LLM 点评周报;称体重提醒(「体重」类 routine)当天称过就不响,上秤后一同步自动算完成、不用再点确认
- 🔔 **提醒**:一次性 / 每天 / 每周几 / **以完成为准每 N 天**(比如隔天做一次的拉伸:忘了第二天接着提醒,做完才隔开),对话里一句话就能设;与 iPhone 提醒事项**双向同步**(快捷指令);体重同步到 Apple 健康
- 🧠 **LLM**:DeepSeek function calling(默认 `deepseek-flash`),每句话带上下文(开放事项 + 今后的提醒 + 最近的对话原文 + 长期记忆)决策;LLM 不可用时降级到正则快路径,核心提醒不受影响
- 🗂️ **长期记忆**:每天 04:00 把前一天的对话整理进记忆库(抽取 → 找相似旧条目 → 新增/更新/取代,只增不删、每批可撤销,另写一篇日记);对话时只带核心档案 + 近日安排 + 本地向量检索出的相关记忆(Ollama bge-m3),库再大注入量也不变;「记住/忘掉/查一下」随时生效
- 📲 **推送**:Bark(时效性 / 重要警报可破静音)+ Telegram(双向,inline 按钮)

输出全日语(艾莉口吻),输入中日文都听得懂。

## 技术栈

Python 3.13 · FastAPI · APScheduler(croniter 单引擎)· SQLite(WAL,版本化原子迁移)· mpv ·
VOICEVOX / AivisSpeech(Docker)· bark-server(Docker)· Ollama + bge-m3(Docker,记忆向量)· sherpa-onnx(SenseVoice + Silero VAD,语音识别)· python-telegram-bot · matplotlib · numpy · Jinja2 + Chart.js。
主服务单进程(语音识别另起一个小进程 eri-stt,崩了也不连累提醒),零前端构建,时间表存 DB、网页改完即生效。系统依赖:mpv、ffmpeg。

## 快速开始

```bash
git clone <this repo> && cd health-hub
uv sync                        # 依赖
bash scripts/gen_media.sh      # 生成白噪音/占位闹钟音
cp .env.example .env           # 填 API_TOKEN 等
cd deploy/bark && docker compose up -d && cd ../..      # Bark 推送服务
cd deploy/voicevox && docker compose up -d && cd ../..  # 语音引擎(可选)
cd deploy/aivisspeech && docker compose up -d && cd ../..  # 自训音色引擎(可选,放模型的步骤见 compose 注释)
cd deploy/ollama && docker compose up -d && docker exec ollama ollama pull bge-m3 && cd ../..  # 记忆向量(可选,不开则不检索记忆)
make deploy                    # systemd 上线(端口 8300)
make deploy-stt                # 语音识别 eri-stt(可选;先下载约 230MB 模型,不装则语音入口不可用)
```

- 网页:`http://<server>:8300`(Token 登录)
- 手机侧配置(Bark App、Telegram bot、Siri 快捷指令、Withings 授权、声卡直通):写在本机的 `deploy/notes.md`(含内网地址等个人环境信息,不进仓库)
- 测试:`make test`;端到端冒烟:`scripts/e2e_smoke.sh`

## 架构一览

```
stt/              # eri-stt:语音识别进程(SenseVoice + VAD,127.0.0.1:8310,OpenAI 兼容接口)
app/
├── scheduler/    # schedules 表 → APScheduler(croniter),sweeper(服药重发/提醒追催)
├── services/     # weights / routines / reminders 实例化 / conversation 对话大脑 / memories 记忆库 + consolidate 每日整理 / voice 语音一轮 / intents / body_metrics
├── audio/        # manager(会话/渐强/播报共存) / mpv_player / tts(VOICEVOX 兼容引擎 + 缓存 + 静音) / stt(识别客户端)
├── notify/       # Bark + Telegram 统一入口
├── bot/          # Telegram long-polling(文字 + 语音消息)+ 正则兜底(LLM 不可用时)
├── llm/          # DeepSeek API(模型在 /settings 改)+ embed(本地 Ollama 向量)
├── ingest/       # Withings OAuth+全量轮询 / Health Auto Export
└── routers/      # API + 网页 + 短链回调
```

## 说明

- 个人项目、单用户、LAN 内使用;API 用单 token,不做多用户
- ずんだもん音声:个人私用无需标注;若公开发布生成音频请按 [VOICEVOX 规约](https://voicevox.hiroshiba.jp/term/) 标注「VOICEVOX:ずんだもん」
- 项目目录名 `health-hub` 是历史沿用,产品名为 Eri

## License

MIT
