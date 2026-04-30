# 🎬 AI Video Analyzer

> 浏览器实时视频智能分析系统 — 在任意网页播放视频，**视觉帧轨**与**音频轨**双轨并行采集，VLM 逐帧理解 + ASR 本地转写 + LLM 增量总结，结果实时展示并持久化到本地。同时支持无浏览器的**本地文件离线分析**模式。

---

## 📖 功能特性

| 功能 | 说明 |
|------|------|
| 👁️ VLM 实时帧分析 | 每 3s 自动截帧，视觉语言模型实时理解画面内容 |
| 🎙️ ASR 音频转写 | faster-whisper 本地转写，每 10s 一段独立入库 |
| 🧠 LLM 增量总结 | 每 N 帧触发一次滚动总结，视觉 + 语音双轨融合 |
| 💾 双轨持久化 | `frames.jsonl` + `audio_segments.jsonl` 收到即落盘，与 LLM/ASR 结果完全解耦 |
| 📏 视频元信息 | `meta.json` 记录 URL、标题、视频时长，首帧收到即写入 |
| 🔄 断点续传 | 服务重启后自动恢复未总结帧，Watchdog 保障总结不丢失 |
| 🌐 通用网页支持 | 支持所有含 `<video>` 标签的页面（B站、YouTube、优酷等） |
| 🔌 多模型后端 | VLM/LLM 均支持本地 HuggingFace、本地 Ollama、远程 OpenAI 兼容 API |
| 📝 本地文件分析 | `analyze_local.py` 无需浏览器，直接在终端分析本地视频 |

---

## 🏗️ 系统架构

### 浏览器实时分析（双轨解耦架构）

```
┌─────────────────────────────────────────────────────────┐
│                  浏览器插件（content.js）                  │
│                                                         │
│  视觉帧轨（每 3s）          音频轨（每 10s）               │
│  captureAndSendFrame()     startAudioTrack()            │
│       │                          │                      │
│       │ type: analyze_frame      │ type: audio_segment  │
└───────┼──────────────────────────┼──────────────────────┘
        │   WebSocket              │
        │   ws://127.0.0.1:19527   │
        ▼                          ▼
┌─────────────────────────────────────────────────────────┐
│              websocket_server.py（本地服务）               │
│                                                         │
│  ① 立即落盘 raw 行           ① 立即落盘 raw 行            │
│     frames.jsonl               audio_segments.jsonl     │
│     （无需等 VLM）              （无需等 ASR）              │
│         │                          │                    │
│  ② VLM 推理（后台线程）      ② ASR 转写（后台线程）         │
│     video_analyzer.py          video_analyzer.py        │
│     analyze_base64_frames()    transcribe_audio_b64()   │
│         │                          │                    │
│  ③ 追加 content 行           ③ 追加 text 行              │
│     frames.jsonl               audio_segments.jsonl     │
│     （含 VLM 分析结果）          audio_transcript.md     │
│         │                                               │
│  ④ 达到阈值 / Watchdog 触发                              │
│     LLM 增量总结（双轨融合）                              │
│     incremental_summary()                               │
│         │                                               │
│  ⑤ 写入 summary.md  →  广播 → 浏览器右侧面板展示           │
└─────────────────────────────────────────────────────────┘
```

**落盘保障机制**：消息到达即写 raw 占位行，VLM/ASR 结果异步追加最终行。即使模型崩溃，时间轴数据永不丢失。

**Watchdog 机制**：每 10s 轮询所有会话，若有待总结帧且空闲超过 20s（截帧中断/视频暂停），无论帧数多少强制触发总结。

### 本地文件离线分析

```
终端命令
    │
    ▼
analyze_local.py
    │
    ├── ASR 引擎（speech_recognizer.py）
    │     faster-whisper 语音转写 → 带时间戳的完整文字记录
    │
    ├── VLM 引擎（video_analyzer.py）  ← 仅 visual / hybrid 模式
    │     关键帧采样 → 逐帧内容描述
    │
    └── LLM 引擎（video_analyzer.py）
          ASR 文字 / VLM 帧描述 → 结构化 Markdown 总结
               └─ note_saver.py → 本地 .md 文件（含 YAML front matter）
```

---

## 📂 目录结构

```
video-analysis-system/
├── chrome-extension/           # Chrome 浏览器扩展（Manifest V3）
│   ├── manifest.json           # 扩展清单：权限 + 支持站点
│   ├── background.js           # Service Worker：WebSocket 连接管理 + 消息路由
│   ├── content.js              # Content Script：视频检测 + 双轨采集 + 右侧结果面板
│   ├── popup.html / popup.js   # 弹窗控制面板：服务状态 + 一键开始分析
│   └── icons/
│
├── local-service/              # Python 本地服务（AI 推理核心）
│   ├── websocket_server.py     # 主服务：WebSocket + 会话管理 + 双轨调度 + Watchdog
│   ├── video_analyzer.py       # 双引擎推理：VLM 帧理解 + LLM 增量总结 + ASR 转写
│   ├── speech_recognizer.py    # ASR 语音识别（faster-whisper）
│   ├── note_saver.py           # Markdown 笔记生成器（本地文件模式）
│   ├── analyze_local.py        # 独立脚本：终端分析本地视频（无需浏览器）
│   ├── config.json             # 模型与推理配置
│   └── requirements.txt        # Python 依赖
│
├── ana/                        # 分析结果持久化（自动生成）
│   └── <来源>/                 # bilibili / youtube / ...
│       └── <视频ID>/
│           ├── frames.jsonl         # 视觉帧轨：raw 占位 + VLM 分析结果（追加）
│           ├── audio_segments.jsonl # 音频轨：raw 占位 + ASR 转写结果（追加）
│           ├── audio_transcript.md  # 可读音频文字稿（Obsidian 友好）
│           ├── summary.md           # 最新 LLM 增量总结（YAML front matter + Markdown）
│           └── meta.json            # 视频元信息（URL、标题、来源、时长）
│
├── .env                        # 模型后端配置（⚠️ 需手动填写，勿提交到 Git）
├── .env.example                # 配置模板（复制为 .env 后填写）
└── README.md
```

---

## ⚙️ 模型后端配置（`.env`）

复制模板并按需填写：

```bash
cp .env.example .env
```

所有后端切换均在 `.env` 中完成，**无需改代码**。

### VLM — 视觉语言模型（逐帧图像理解）

| `VLM_BACKEND` | 说明 | 适用场景 |
|---|---|---|
| `huggingface` | 本地加载 HuggingFace 模型 | 无网环境 / 追求数据隐私 |
| `ollama` | 调用本地 Ollama 多模态模型 | 已有 Ollama 环境，开箱即用 |
| `openai_compatible` | 远程多模态 API（GPT-4o 等） | 无 GPU，用远程 API |

```ini
# ── 方案A：HuggingFace 本地（需 torch + transformers）──
VLM_BACKEND=huggingface
VLM_HF_MODEL=Qwen/Qwen2-VL-2B-Instruct   # 或 7B 版本

# ── 方案B：Ollama 本地（需 ollama serve 在运行）──
VLM_BACKEND=ollama
VLM_OLLAMA_BASE_URL=http://localhost:11434
VLM_OLLAMA_MODEL=qwen2-vl:7b

# ── 方案C：OpenAI 兼容远程 API ──
VLM_BACKEND=openai_compatible
VLM_API_BASE_URL=https://api.openai.com/v1
VLM_API_KEY=sk-xxxx
VLM_API_MODEL=gpt-4o
```

### LLM — 大语言模型（增量总结 / ASR 文字总结）

| `LLM_BACKEND` | 说明 | 适用场景 |
|---|---|---|
| `ollama` | 本地 Ollama 文本模型 | 已有 Ollama 环境 |
| `openai_compatible` | 远程文本 API（OpenAI / DeepSeek / Moonshot 等） | 快速高质量总结 |

```ini
# ── 方案A：Ollama 本地 ──
LLM_BACKEND=ollama
LLM_OLLAMA_BASE_URL=http://localhost:11434
LLM_OLLAMA_MODEL=qwen2.5:7b

# ── 方案B：OpenAI 兼容远程 API（推荐用于总结）──
LLM_BACKEND=openai_compatible
LLM_API_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_API_MODEL=deepseek-chat
```

### 推荐组合

| 场景 | VLM | LLM |
|---|---|---|
| 💻 全本地（有 CUDA GPU） | `huggingface` Qwen2-VL-7B | `ollama` qwen2.5:7b |
| 🍎 全本地（Apple Silicon M 芯片） | `huggingface` Qwen2-VL-2B (MPS) | `ollama` qwen2.5:3b |
| ☁️ 全远程（无 GPU） | `openai_compatible` gpt-4o-mini | `openai_compatible` deepseek-chat |
| 🔀 混合（省钱省时） | `ollama` qwen2-vl:7b 本地 | `openai_compatible` 远程 LLM |

---

## 🚀 快速开始

### 1. 安装 Python 依赖

```bash
cd local-service
pip install -r requirements.txt
```

> **注意**：`requirements.txt` 已包含 `torch`、`transformers`、`faster-whisper` 等全量依赖。若只使用 Ollama/API 后端不需要本地 GPU 模型，可仅安装轻量依赖：
> ```bash
> pip install python-dotenv openai opencv-python Pillow faster-whisper
> ```

### 2. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，填写 VLM_BACKEND / LLM_BACKEND 及对应参数
```

最简配置示例（Ollama VLM + 远程 LLM）：

```ini
VLM_BACKEND=ollama
VLM_OLLAMA_MODEL=qwen2-vl:7b

LLM_BACKEND=openai_compatible
LLM_API_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_API_MODEL=deepseek-chat
```

### 3a. 启动 WebSocket 服务（浏览器模式）

```bash
cd local-service
python3 websocket_server.py
```

服务默认监听 `ws://127.0.0.1:19527`。启动时打印当前 VLM/LLM 后端及模型名称，**模型在首次收到分析请求时懒加载**（也可在 `config.json` 中设置 `preload_on_start: true` 提前预热）。

### 3b. 本地视频文件分析（无需浏览器）

```bash
cd local-service

# 基本用法（自动读取 .env 和 config.json）
python3 analyze_local.py /path/to/video.mp4

# 指定模式和输出目录
python3 analyze_local.py /path/to/video.mp4 --mode asr --output ./notes --title "财报解读"

# 混合模式 + 添加个人备注
python3 analyze_local.py /path/to/video.mp4 --mode hybrid --note "来自内部培训"
```

**参数说明：**

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `video` | — | 视频文件路径（必填） | — |
| `--mode` | `-m` | `asr` \| `visual` \| `hybrid` | `config.json` 中的 `analysis_mode` |
| `--output` | `-o` | 输出目录 | 视频所在目录 |
| `--title` | `-t` | 笔记标题 | 文件名 |
| `--note` | `-n` | 附加到笔记开头的个人备注 | 空 |
| `--config` | `-c` | config.json 路径 | `local-service/config.json` |

**分析模式：**

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| `asr` | 语音转写 → LLM 结构化总结 | 演讲/课程/新闻（推荐，速度最快） |
| `visual` | VLM 关键帧分析 → 总结 | 无音频 / 静默教程 / 字幕截图类 |
| `hybrid` | ASR + VLM 双引擎 → 综合总结 | 需要最全面分析，不在意耗时 |

### 4. 加载 Chrome 扩展

1. 打开 `chrome://extensions/`
2. 开启**开发者模式**（右上角切换）
3. 点击**加载已解压的扩展程序**，选择 `chrome-extension/` 目录

### 5. 开始浏览器分析

1. 打开 B站/YouTube 等视频页面并开始播放
2. 点击浏览器右上角扩展图标，确认服务状态为 **已连接**
3. 视频播放器上出现 **👁️ AI 分析** 按钮，点击启动
4. 右侧面板实时展示分析进度与滚动总结

---

## 📊 分析结果文件

### 浏览器模式

每个视频的数据保存在 `ana/<来源>/<视频ID>/` 目录下：

**`meta.json`** — 视频元信息，首帧到达即写入：
```json
{
  "url": "https://www.bilibili.com/video/BVxxxxx",
  "title": "视频标题",
  "source": "bilibili",
  "video_key": "BVxxxxx",
  "created_at": "2026-04-30 10:00:00",
  "duration_sec": 312.4,
  "duration_str": "05:12"
}
```

**`frames.jsonl`** — 视觉帧轨，每帧两行（raw 占位 + 分析结果）：
```jsonl
{"time_sec": 12.5, "time_str": "00:12", "content": "", "raw": true}
{"time_sec": 12.5, "time_str": "00:12", "content": "画面显示股票K线图，标注当前价格 23.45..."}
```

**`audio_segments.jsonl`** — 音频轨，每段两行（raw 占位 + ASR 结果）：
```jsonl
{"seq": 1, "start_sec": 0.0, "end_sec": 10.0, "time_str": "00:00~00:10", "text": "", "raw": true}
{"seq": 1, "start_sec": 0.0, "end_sec": 10.0, "time_str": "00:00~00:10", "text": "今天我们来聊一聊..."}
```

**`audio_transcript.md`** — 可读音频文字稿（Obsidian 兼容）：
```markdown
# 音频文字稿：视频标题

> 来源: bilibili | 创建: 2026-04-30 10:00:00

---

**[00:00~00:10]** 今天我们来聊一聊...
```

**`summary.md`** — 最新 LLM 增量总结（含 YAML front matter，Obsidian 兼容）：
```markdown
---
title: "视频标题"
source: "https://..."
created: "2026-04-30"
tags:
  - video-notes
  - bilibili
---

### 话题一
- **关键词** 具体描述...
```

### 本地文件模式

保存为单个 Markdown 文件，包含 YAML front matter + 结构化摘要 + 完整转写文本（输出路径由 `--output` 指定）。

---

## 🔧 高级配置（`config.json`）

```json
{
  "model": {
    "name": "Qwen/Qwen2-VL-2B-Instruct",
    "device": "auto",
    "max_new_tokens": 512,
    "summary_max_tokens": 2048
  },
  "video": {
    "target_fps": 0.2,
    "batch_size": 4,
    "frame_resolution": [512, 512]
  },
  "asr": {
    "model_size": "medium",
    "device": "auto",
    "compute_type": "float32",
    "language": "zh"
  },
  "summary": {
    "every_n_frames": 3
  },
  "analysis_mode": "asr",
  "preload_on_start": true
}
```

| 参数 | 说明 |
|------|------|
| `model.device` | `auto` 自动选择 MPS/CUDA/CPU，或手动指定 `cpu`/`cuda`/`mps` |
| `video.target_fps` | 截帧频率（帧/秒），越小速度越快但信息密度越低 |
| `summary.every_n_frames` | 每积累 N 帧触发一次 LLM 增量总结（Watchdog 兜底，不足 N 帧也会在空闲 20s 后触发） |
| `asr.model_size` | Whisper 模型大小：`tiny`/`base`/`small`/`medium`/`large-v3` |
| `asr.language` | 转写语言：`zh`/`en`/`auto`（`auto` 自动检测） |
| `analysis_mode` | 本地文件分析的默认模式，可被 `--mode` 命令行参数覆盖 |
| `preload_on_start` | `true` 服务启动时立即预热模型；`false` 首次请求时懒加载 |

---

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| 浏览器扩展 | Chrome Extension Manifest V3 |
| 前后端通信 | WebSocket（`ws://127.0.0.1:19527`） |
| VLM 帧推理 | Qwen2-VL（HuggingFace）/ Ollama / OpenAI 兼容 API |
| LLM 文本总结 | Ollama / OpenAI 兼容 API |
| ASR 语音识别 | faster-whisper（本地，支持多语言） |
| 持久化存储 | 本地 JSONL + Markdown（双写，独立于推理结果） |
| 音频采集 | MediaRecorder API（`video.captureStream()`） |
| 音频格式转换 | ffmpeg（webm/opus → 16kHz mono WAV） |






