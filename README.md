# 🎬 AI Video Analyzer

> 浏览器实时视频智能分析系统 — 在任意网页播放视频，自动截帧 → VLM 逐帧理解 → LLM 增量总结，结果实时展示并持久化到本地。同时支持无浏览器的**本地文件离线分析**模式。

---

## 📖 功能特性

| 功能 | 说明 |
|------|------|
| 👁️ VLM 实时帧分析 | 播放视频时自动截帧，视觉语言模型实时理解画面内容 |
| 🧠 LLM 增量总结 | 每 N 帧触发一次 LLM 滚动总结，主区域持续更新最新进展 |
| 💾 本地持久化 | 帧数据写入 `frames.jsonl`，总结写入 `summary.md`，支持断点续传 |
| 🌐 通用网页支持 | 支持所有含 `<video>` 标签的页面（B站、YouTube、优酷等） |
| 🔌 多模型后端 | VLM/LLM 均支持本地 HuggingFace、本地 Ollama、远程 OpenAI 兼容 API |
| 🎤 ASR 语音识别 | faster-whisper 本地转写，支持中英文及多语言自动检测 |
| 📝 本地文件分析 | `analyze_local.py` 无需浏览器，直接在终端分析本地视频 |

---

## 🏗️ 系统架构

### 模式一：浏览器实时分析

```
浏览器插件（Chrome Extension）
    │
    │  WebSocket  ws://127.0.0.1:19527
    ▼
本地服务（local-service/websocket_server.py）
    │
    ├── VLM 引擎（video_analyzer.py）
    │     逐帧图像理解 → 写入 frames.jsonl
    │     后端可选：HuggingFace / Ollama / OpenAI 兼容 API
    │
    └── LLM 引擎（video_analyzer.py）
          增量文本总结 → 写入 summary.md → 推送给浏览器
          后端可选：Ollama / OpenAI 兼容 API
```

### 模式二：本地文件离线分析

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
          ASR文字 / VLM帧描述 → 结构化 Markdown 总结
               └─ note_saver.py → 本地 .md 文件
```

### 浏览器模式数据流

```
视频页面
  └─ content.js 截帧（每 3s 一帧）
       └─ background.js 转发 → WebSocket
            └─ websocket_server.py 接收
                 ├─ VideoAnalyzer.analyze_base64_frames()  ← VLM 帧推理
                 │    └─ 写入 ana/<来源>/<视频ID>/frames.jsonl
                 └─ VideoAnalyzer.incremental_summary()   ← LLM 文本总结
                      └─ 写入 ana/<来源>/<视频ID>/summary.md
                           └─ 广播 incremental_summary → 浏览器展示
```

---

## 📂 目录结构

```
video-analysis-system/
├── chrome-extension/           # Chrome 浏览器扩展（Manifest V3）
│   ├── manifest.json           # 扩展清单：权限 + 支持站点
│   ├── background.js           # Service Worker：WebSocket 连接管理 + 消息路由
│   ├── content.js              # Content Script：视频检测 + 截帧 + 右侧结果面板
│   ├── popup.html / popup.js   # 弹窗控制面板：服务状态 + 一键开始分析
│   └── icons/
│
├── local-service/              # Python 本地服务（AI 推理核心）
│   ├── websocket_server.py     # 主服务：WebSocket 服务器 + 会话管理 + 增量总结调度
│   ├── video_analyzer.py       # 双引擎推理：VLM 帧理解 + LLM 文本总结
│   ├── speech_recognizer.py    # ASR 语音识别（faster-whisper）
│   ├── note_saver.py           # Markdown 笔记生成器
│   ├── analyze_local.py        # 独立脚本：终端分析本地视频（无需浏览器）
│   ├── config.json             # 模型与推理配置
│   └── requirements.txt        # Python 依赖
│
├── ana/                        # 分析结果持久化（自动生成）
│   └── <来源>/                 # bilibili / youtube / ...
│       └── <视频ID>/
│           ├── frames.jsonl    # 逐帧分析结果（追加写入）
│           ├── summary.md      # 最新增量总结（覆盖写入）
│           └── meta.json       # 视频元信息
├── .env                        # 模型后端配置（⚠️ 需手动填写，勿提交到 Git）
└── README.md
```

---

## ⚙️ 模型后端配置（`.env`）

编辑项目根目录的 `.env` 文件，**所有后端切换均在此完成，无需改代码**。

### VLM — 视觉语言模型（逐帧图像理解）

| `VLM_BACKEND` | 说明 | 适用场景 |
|---|---|---|
| `huggingface` | 本地加载 HuggingFace 模型（默认） | 无网环境 / 追求数据隐私 |
| `ollama` | 调用本地 Ollama 多模态模型 | 已有 Ollama 环境，开箱即用 |
| `openai_compatible` | 远程多模态 API（GPT-4o 等） | 无 GPU，用远程 API |

```ini
# ── 方案A：HuggingFace 本地（需 torch + transformers） ──
VLM_BACKEND=huggingface
VLM_HF_MODEL=Qwen/Qwen2-VL-2B-Instruct   # 或 7B 版本

# ── 方案B：Ollama 本地（需 ollama serve 在运行） ──
VLM_BACKEND=ollama
VLM_OLLAMA_BASE_URL=http://localhost:11434
VLM_OLLAMA_MODEL=qwen2-vl:7b

# ── 方案C：OpenAI 兼容远程 API ──
VLM_BACKEND=openai_compatible
VLM_API_BASE_URL=https://api.openai.com/v1
VLM_API_KEY=sk-xxxx
VLM_API_MODEL=gpt-4o
```

### LLM — 大语言模型（增量总结 / ASR 总结）

| `LLM_BACKEND` | 说明 | 适用场景 |
|---|---|---|
| `ollama` | 本地 Ollama 文本模型 | 已有 Ollama 环境 |
| `openai_compatible` | 远程文本 API（OpenAI / DeepSeek / Moonshot 等） | 快速高质量总结 |

```ini
# ── 方案A：Ollama 本地 ──
LLM_BACKEND=ollama
LLM_OLLAMA_BASE_URL=http://localhost:11434
LLM_OLLAMA_MODEL=qwen2.5:7b

# ── 方案B：OpenAI 兼容远程 API（推荐用于总结） ──
LLM_BACKEND=openai_compatible
LLM_API_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_API_MODEL=deepseek-chat
```

### 推荐组合

| 场景 | VLM | LLM |
|---|---|---|
| 💻 全本地（有 GPU） | `huggingface` Qwen2-VL-7B | `ollama` qwen2.5:7b |
| 💻 全本地（无 GPU / M 芯片） | `huggingface` Qwen2-VL-2B (MPS) | `ollama` qwen2.5:3b |
| ☁️ 全远程（无 GPU） | `openai_compatible` gpt-4o-mini | `openai_compatible` deepseek-chat |
| 🔀 混合（省钱） | `huggingface` 本地 VLM | `openai_compatible` 远程 LLM |

---

## 🚀 快速开始

### 1. 安装 Python 依赖

```bash
cd local-service
pip install -r requirements.txt
```

> HuggingFace 模式额外需要：`pip install torch transformers`
> ASR 模式额外需要：`pip install faster-whisper`

### 2. 配置 `.env`

复制模板并按需填写：

```bash
# 最简配置（Ollama VLM + 远程 LLM）
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

服务默认监听 `ws://127.0.0.1:19527`。启动时会打印当前 VLM/LLM 后端和模型名称，模型在**首次收到分析请求时**懒加载。

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
|------|------|------|------|
| `video` | — | 视频文件路径（必填） | — |
| `--mode` | `-m` | `asr` \| `visual` \| `hybrid` | config.json 中的 `analysis_mode` |
| `--output` | `-o` | 输出目录 | 视频所在目录 |
| `--title` | `-t` | 笔记标题 | 文件名 |
| `--note` | `-n` | 附加到笔记开头的个人备注 | 空 |
| `--config` | `-c` | config.json 路径 | `local-service/config.json` |

**分析模式：**

| 模式 | 流程 | 适用场景 |
|------|------|------|
| `asr` | 语音转写 → LLM 结构化总结 | 演讲/课程/新闻（推荐，速度最快） |
| `visual` | VLM 关键帧分析 → 总结 | 无音频 / 静默教程 / 字幕截图类 |
| `hybrid` | ASR + VLM 双引擎 → 综合总结 | 需要最全面分析，不在意耗时 |

### 4. 加载 Chrome 扩展

1. 打开 `chrome://extensions/`
2. 开启**开发者模式**
3. 点击**加载已解压的扩展程序**，选择 `chrome-extension/` 目录

### 5. 开始浏览器分析

1. 打开 B站/YouTube 等视频页面，开始播放
2. 点击浏览器右上角扩展图标，确认服务状态为**已连接**
3. 点击**开始 AI 分析**，右侧面板实时展示分析结果

---

## 📊 分析结果

### 浏览器模式

每个视频的分析结果保存在 `ana/<来源>/<视频ID>/` 目录下：

- **`frames.jsonl`** — 每帧的 VLM 分析结果，追加写入：
  ```json
  {"time_sec": 12.5, "time_str": "00:12", "content": "画面显示..."}
  ```

- **`summary.md`** — 最新的 LLM 增量总结，包含进度摘要、核心要点、最新内容

- **`meta.json`** — 视频元信息（URL、标题、来源、创建时间）

### 本地文件模式

保存为单个 Markdown 文件，包含 YAML front matter + 结构化摘要 + 完整转写文本。

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
    "every_n_frames": 5
  },
  "analysis_mode": "asr",
  "preload_on_start": false
}
```

| 参数 | 说明 |
|------|------|
| `model.device` | `auto` 自动选择 MPS/CUDA/CPU，或手动指定 |
| `video.target_fps` | 截帧频率（帧/秒），越小越快但信息越少 |
| `summary.every_n_frames` | 每积累 N 帧触发一次 LLM 增量总结 |
| `asr.model_size` | whisper 模型大小：`tiny`/`base`/`small`/`medium`/`large-v3` |
| `asr.language` | 转写语言，`zh`/`en`/`auto`（auto 会自动检测） |
| `analysis_mode` | 本地分析默认模式，可被 `--mode` 命令行参数覆盖 |
| `preload_on_start` | `true` 服务启动时立即预热模型，`false` 首次请求时懒加载 |

---

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| 浏览器扩展 | Chrome Extension Manifest V3 |
| 前后端通信 | WebSocket（ws://127.0.0.1:19527） |
| VLM 帧推理 | Qwen2-VL（HuggingFace）/ Ollama / OpenAI 兼容 API |
| LLM 文本总结 | Ollama / OpenAI 兼容 API |
| ASR 语音识别 | faster-whisper（本地，支持多语言） |
| 持久化存储 | 本地 JSONL + Markdown 文件 |






