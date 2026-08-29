# Eri 自训音色:训练机(GPU)侧任务简报

> 这份简报交给**另一台 GPU 机器上的 Claude Code agent** 执行。训练产物回传后,Eri 服务器(PVE LXC,`/projects/health-hub`)只需换 TTS 引擎地址 + speaker id,代码零改动。

## 0. 目标与交付物

目标:用已备好的数据集训练一个 **Style-Bert-VITS2(JP-Extra)** 日语单说话人模型,导出为 **AivisSpeech 可加载的 `.aivmx`**。

最终要回传给 Eri 服务器的东西:

1. `Eri.aivmx` —— 必需
2. `model_assets/Eri/` 整个目录打包(`*.safetensors` + `config.json` + `style_vectors.npy`)—— 备份,方便以后续训/重导出
3. 一段试听 wav(用下面第 6 步的 AivisSpeech Engine 合成,不是 SBV2 WebUI 合成的)+ `GET /speakers` 返回的 JSON —— 用来确认 style id 与音质
4. 训练用的最终 `config.json` 里的关键超参(epochs / batch / 选用的 step)—— 一句话说明即可

Eri 服务器侧调用契约(必须满足):VOICEVOX 兼容 HTTP API,`POST /audio_query?text=&speaker=<int>` → `POST /synthesis?speaker=<int>` 返回 wav。AivisSpeech Engine 原生满足,speaker 用整数 style id。

## 1. 环境

- NVIDIA GPU,VRAM ≥ 8 GB(6 GB 可用,batch 降到 2);CUDA 可用
- Python **3.10**(Style-Bert-VITS2 官方推荐版本;不要用 3.12+)
- 磁盘 ≥ 20 GB(预训练模型 + BERT + checkpoints)
- 数据集是**你有权使用的声音**(自录 / 授权语料),不要用未授权的真人声音

### 安装(Linux)

```bash
git clone https://github.com/litagin02/Style-Bert-VITS2.git
cd Style-Bert-VITS2
python3.10 -m venv venv && source venv/bin/activate
pip install "torch<2.4" torchaudio --index-url https://download.pytorch.org/whl/cu121   # 与 requirements 里 torch 版本约束对齐,先看 README 当前要求
pip install -r requirements.txt
python initialize.py          # 下载 BERT(deberta-v2-large-japanese-char-wwm 等)+ JP-Extra 预训练权重
```

Windows 直接用仓库根目录的 `Install-Style-Bert-VITS2.bat`(会自建 venv 并跑 initialize)。

> 版本会变,任何命令的参数以 `python xxx.py --help` 和仓库 README / docs 为准;下面的 flag 是截至 v2.6 系的写法。

## 2. 数据集摆放

```
Data/Eri/
├── raw/            # wav 文件(单声道;2–12 秒/条,>14 秒会被截或跳过;44.1 kHz 最佳,其他采样率会自动重采样)
│   ├── 0001.wav
│   └── ...
└── esd.list        # 每行:  文件名|说话人名|语言|文本
```

`esd.list` 示例(语言固定 `JP`,说话人名统一 `Eri`):

```
0001.wav|Eri|JP|おはよう、今日も一日がんばろうね。
0002.wav|Eri|JP|お薬の時間だよ。
```

如果数据集**只有音频没有文本**:

```bash
python slice.py -i <原始长音频目录> -o Data/Eri/raw --min_sec 2 --max_sec 12      # 按静音切片
python transcribe.py --model_name Eri --language ja --model large-v3 \
       --initial_prompt "こんにちは。元気、ですか?ふふっ、私は元気だよ!"   # faster-whisper 转写,自动生成 esd.list
```

转写后**务必抽查** `esd.list`:漏字/错读会直接教坏模型;有明显错误就人工改。

数据量参考:10 分钟能出声,30–60 分钟稳定,情感多样更好。BGM、混响、多人对话的条目要剔掉。

## 3. 预处理

```bash
python preprocess_all.py -m Eri --batch_size 4 -e 100 --use_jp_extra --yomi_error skip
```

一条命令完成:文本前处理(生成 `Data/Eri/config.json`、`train.list`、`val.list`)→ 重采样 → BERT 特征 → style vectors。
`--yomi_error skip` 让读音解析失败的句子被跳过而不是整体中断;跑完看日志里跳过了多少条,超过 5% 就回头修 `esd.list`。

## 4. 训练

```bash
python train_ms_jp_extra.py --config Data/Eri/config.json --model Data/Eri
```

- checkpoints 落在 `Data/Eri/models/`(`G_<step>.pth` 等),同时会把可用的推理资产同步到 `model_assets/Eri/`(`Eri_e<epoch>_s<step>.safetensors` + `config.json` + `style_vectors.npy`)
- 8 GB 显存 batch 4 没问题;OOM 就把 `Data/Eri/config.json` 里 `train.batch_size` 降到 2
- 100 epoch 对 30 分钟数据通常足够;可开 `tensorboard --logdir Data/Eri/models` 看 loss
- **不是越晚的 step 越好**:过拟合会变得生硬/破音。第 5 步试听后再选

## 5. 试听与选 checkpoint

```bash
python app.py     # Gradio WebUI,选 model_assets/Eri 下不同 safetensors 对比试听
```

用 Eri 常说的句子试:`お薬の時間だよ`、`OK、14時にまた声かけるね`、`体重 62.5 キロ、記録したよ`、`おはよう!今日も一日がんばろうね`。
选一个最自然的 safetensors,其他的可删(减小回传体积)。默认只需要 `Neutral` 一个 style;不用做 style 聚类。

## 6. 导出 AIVMX 并用 AivisSpeech Engine 实测

AivisSpeech 跑的是 ONNX,所以要 safetensors → onnx → aivmx:

```bash
# 6.1 ONNX 导出(SBV2 v2.6+ 自带;若当前版本没有 convert_onnx.py,用 AIVM Generator 网页版走 6.2b)
python convert_onnx.py --model model_assets/Eri            # 生成 model_assets/Eri/*.onnx,--help 看是否要指定具体 safetensors

# 6.2a 命令行打包(aivmlib,Aivis 官方工具)
pip install aivmlib
aivmlib create-aivmx --help                                 # 先看当前参数名
aivmlib create-aivmx \
  --model-architecture "Style-Bert-VITS2 JP-Extra" \
  --hyper-parameters model_assets/Eri/config.json \
  --style-vectors    model_assets/Eri/style_vectors.npy \
  --output           Eri.aivmx \
  model_assets/Eri/<选中的>.onnx
```

6.2b 不装工具的替代:浏览器打开 https://aivm-generator.aivis-project.com/ ,上传 onnx + config.json + style_vectors.npy,元数据填 モデル名 `Eri` / 話者名 `エリ` / 言語 `ja`,下载 `.aivmx`。

### 实测(必做,这就是 Eri 服务器上将来跑的东西)

```bash
docker run --rm -p 10101:10101 ghcr.io/aivis-project/aivisspeech-engine:cpu-latest &   # 有 GPU 也用 cpu 镜像测,和服务器一致
sleep 20
curl -s -F "file=@Eri.aivmx" http://127.0.0.1:10101/aivm_models/install                 # 装模型
curl -s http://127.0.0.1:10101/speakers | python -m json.tool > speakers.json            # 找到 Eri 的 styles[].id(整数)
SPK=<那个整数>
curl -s -X POST "http://127.0.0.1:10101/audio_query?text=お薬の時間だよ。飲んだら教えてね&speaker=$SPK" > q.json
curl -s -X POST "http://127.0.0.1:10101/synthesis?speaker=$SPK" -H 'Content-Type: application/json' -d @q.json -o eri_test.wav
```

`eri_test.wav` 能正常出声、音色对 → 完成。顺手记一下 CPU 上这两步的耗时(服务器是纯 CPU LXC,只是想知道大概几秒)。

## 7. 回传

把以下打包发回:`Eri.aivmx`、`model_assets/Eri/`(zip)、`speakers.json`、`eri_test.wav`、一行超参说明。

Eri 服务器侧接手后的动作(不需要训练机做):新增 `deploy/aivisspeech/docker-compose.yml`(端口 10101,模型持久化卷)→ `POST /aivm_models/install` 装 aivmx → 设置 `tts.engine_url=http://127.0.0.1:10101`、`tts.speaker=<style id>` → 播报即刻换嗓子(TTS 缓存 key 含 speaker,无需清缓存)。
