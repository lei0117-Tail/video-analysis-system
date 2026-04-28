"""
视频分析模块 — VLM 帧理解 + LLM 文本总结 双引擎流水线

模型后端配置（via .env）:
  VLM_BACKEND: huggingface | ollama | openai_compatible
  LLM_BACKEND: ollama | openai_compatible

分析模式:
  - 模式1 (ASR优先): 视频音频转写 → LLM 文本总结 → 输出 Markdown
  - 模式2 (VL辅助): 关键帧采样 → VL 内容提取 → 全局总结
  - 模式3 (混合): ASR 转写 + VL 关键帧辅助理解 → 综合总结
  - 模式4 (实时流): 浏览器逐帧推送 → VLM 实时分析 → LLM 增量总结
"""
import base64
import io
import json
import os
import sys
import time
from io import BytesIO
from typing import Dict, List, Optional

import cv2
from PIL import Image

# ── 按需导入，避免未安装时整体崩溃 ──
try:
    import torch
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
    _HF_AVAILABLE = True
except ImportError:
    _HF_AVAILABLE = False

try:
    import ollama as _ollama_sdk
    _OLLAMA_SDK_AVAILABLE = True
except ImportError:
    _OLLAMA_SDK_AVAILABLE = False

try:
    from openai import OpenAI as _OpenAIClient
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

# =====================================================================
# Prompt 模板
# =====================================================================

ASR_SUMMARY_PROMPT = """你是一个专业的视频内容分析师。以下是一个视频的完整语音转写文本（包含时间戳）。

请基于这段文字，生成一份结构化的内容分析报告。要求：
1. 用中文输出
2. 准确提取和整合所有信息
3. 保持客观，不要编造视频中未出现的信息

请按以下格式输出：

## 📋 视频摘要
（3-5句话概括整个视频的核心内容和结论）

## 🔑 核心要点
- 要点1
- 要点2
- 要点3
- ...

## 🏷️ 关键词
关键词1、关键词2、...

## 👤 主要观点/发言（按时间线）
### [时间段] 发言者/场景
该时段的核心内容摘要

---

## 📎 完整转写文本
（保留原始时间戳格式的完整文字记录）"""

STAGE1_CONTENT_PROMPT = """分析这个视频截图，用中文简洁回答：

1. 画面中有哪些文字/字幕/标题？逐字转录。
2. 这帧在讲什么？（1句话概括）
3. 有什么关键数据或名词？

如果画面模糊无法识别，直接说"画面模糊，无法识别具体内容"。不要重复输出。"""

STAGE2_SUMMARY_PROMPT = """你是一个专业的内容总结专家。基于以下视频各时间点的内容分析记录，请生成一份完整的视频内容总结。

要求：
1. 用中文输出
2. 准确提取和整合所有时间点的信息
3. 保持客观，不要编造视频中未出现的信息

请按以下格式输出：

## 📋 视频摘要
（3-5句话概括整个视频的核心内容）

## 🔑 核心要点
- 要点1
- 要点2
- ...

## 🏷️ 关键词
关键词1、关键词2、...

## 📝 详细内容（按时间线）

### ⏱️ [时间段]
该时段的详细内容总结"""


class VideoAnalyzer:
    """
    视频分析器 — 双引擎架构（VLM 帧理解 + LLM 文本总结）

    VLM 后端（逐帧图像理解）：
      huggingface      — 本地 HuggingFace 模型（Qwen2-VL 等）
      ollama           — 本地 Ollama 多模态模型（qwen2-vl 等）
      openai_compatible — 远程多模态 API（GPT-4o、vLLM 等）

    LLM 后端（增量总结 / ASR 总结）：
      ollama           — 本地 Ollama 文本模型（qwen2.5 等）
      openai_compatible — 远程文本 API（OpenAI / DeepSeek / Moonshot 等）
    """

    def __init__(self, config: Dict):
        self.config = config
        self._load_video_config()
        
        # ── VLM 后端初始化 ──
        self._vlm_backend = os.getenv('VLM_BACKEND', 'huggingface').strip()
        print(f"  ├─ VLM 后端: {self._vlm_backend}", file=sys.stderr)
        t0 = time.time()
        try:
            self._init_vlm()
        except Exception as e:
            print(f"  └─ ❌ VLM 初始化失败 [{type(e).__name__}]: {e}", file=sys.stderr)
            raise
        print(f"  ├─ ✅ VLM 初始化完成 (耗时 {time.time()-t0:.1f}s)", file=sys.stderr)

        # ── LLM 后端初始化 ──
        self._llm_backend = os.getenv('LLM_BACKEND', 'ollama').strip()
        print(f"  ├─ LLM 后端: {self._llm_backend}", file=sys.stderr)
        t1 = time.time()
        try:
            self._init_llm()
        except Exception as e:
            print(f"  └─ ❌ LLM 初始化失败 [{type(e).__name__}]: {e}", file=sys.stderr)
            raise
        print(f"  └─ ✅ LLM 初始化完成 (耗时 {time.time()-t1:.1f}s)", file=sys.stderr)

        # ASR 引擎（懒加载）
        self._asr = None

    def _get_asr(self):
        """懒加载 ASR 引擎"""
        if self._asr is None:
            from speech_recognizer import SpeechRecognizer
            self._asr = SpeechRecognizer(self.config)
        return self._asr

    # ===================================================================
    # 初始化 — VLM 后端
    # ===================================================================

    def _init_vlm(self):
        """根据 VLM_BACKEND 初始化对应的视觉模型后端"""
        model_cfg = self.config.get('model', {})
        self.max_new_tokens = model_cfg.get('max_new_tokens', 512)
        self.summary_max_tokens = min(model_cfg.get('summary_max_tokens', 2048), 2048)

        if self._vlm_backend == 'huggingface':
            self._init_vlm_huggingface(model_cfg)
        elif self._vlm_backend == 'ollama':
            self._init_vlm_ollama()
        elif self._vlm_backend == 'openai_compatible':
            self._init_vlm_openai()
        else:
            raise ValueError(f"未知 VLM_BACKEND: {self._vlm_backend}，可选: huggingface / ollama / openai_compatible")

    def _init_vlm_huggingface(self, model_cfg: dict):
        """加载本地 HuggingFace 模型"""
        if not _HF_AVAILABLE:
            raise ImportError("VLM_BACKEND=huggingface 需要安装 torch 和 transformers (pip install torch transformers)")

        model_name = os.getenv('VLM_HF_MODEL') or model_cfg.get('name', 'Qwen/Qwen2-VL-2B-Instruct')

        # 选择推理设备
        preferred = model_cfg.get('device', 'auto')
        if preferred == 'auto':
            if torch.backends.mps.is_available():
                self.device = 'mps'
            elif torch.cuda.is_available():
                self.device = 'cuda'
            else:
                self.device = 'cpu'
        else:
            self.device = preferred

        torch_dtype = torch.float32
        if self.device == 'cuda' and model_cfg.get('torch_dtype') == 'float16':
            torch_dtype = torch.float16

        print(f"     模型路径 : {model_name}", file=sys.stderr)
        print(f"     推理设备 : {self.device}  (dtype={torch_dtype})", file=sys.stderr)
        print(f"     ⏳ 正在从磁盘/缓存加载权重，首次加载可能需要数分钟...", file=sys.stderr)

        self.processor = AutoProcessor.from_pretrained(model_name, trust_remote_code=True)
        self.hf_model = Qwen2VLForConditionalGeneration.from_pretrained(
            model_name,
            torch_dtype=torch_dtype,
            device_map='auto' if self.device in ('cuda', 'mps') else None,
            trust_remote_code=True,
        )
        if self.device in ('mps', 'cuda'):
            self.hf_model = self.hf_model.to(self.device)
        self.hf_model.eval()
        print(f"     ✅ HuggingFace 模型加载完成  device={self.device}  dtype={torch_dtype}", file=sys.stderr)
        
    def _init_vlm_ollama(self):
        """初始化 Ollama VLM 客户端（无需预加载，调用时实时请求）"""
        self._vlm_ollama_base = os.getenv('VLM_OLLAMA_BASE_URL', 'http://localhost:11434').rstrip('/')
        self._vlm_ollama_model = os.getenv('VLM_OLLAMA_MODEL', 'qwen2-vl:7b')
        print(f"     服务地址 : {self._vlm_ollama_base}", file=sys.stderr)
        print(f"     模型名称 : {self._vlm_ollama_model}", file=sys.stderr)
        print(f"     ℹ️  Ollama 客户端就绪（调用时实时发起 HTTP 请求，无预加载）", file=sys.stderr)

    def _init_vlm_openai(self):
        """初始化 OpenAI 兼容 VLM 客户端"""
        if not _OPENAI_AVAILABLE:
            raise ImportError("VLM_BACKEND=openai_compatible 需要安装 openai 库: pip install openai")
        base_url = os.getenv('VLM_API_BASE_URL', 'https://api.openai.com/v1')
        api_key = os.getenv('VLM_API_KEY', 'sk-placeholder')
        key_hint = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 12 else '(已设置)'
        self._vlm_openai_client = _OpenAIClient(base_url=base_url, api_key=api_key)
        self._vlm_openai_model = os.getenv('VLM_API_MODEL', 'gpt-4o')
        print(f"     API 地址 : {base_url}", file=sys.stderr)
        print(f"     模型名称 : {self._vlm_openai_model}", file=sys.stderr)
        print(f"     API Key  : {key_hint}", file=sys.stderr)
        print(f"     ℹ️  OpenAI 兼容客户端就绪（按调用计费，无预加载）", file=sys.stderr)

    # ===================================================================
    # 初始化 — LLM 后端
    # ===================================================================

    def _init_llm(self):
        """根据 LLM_BACKEND 初始化纯文本 LLM 客户端"""
        if self._llm_backend == 'ollama':
            self._llm_ollama_base = os.getenv('LLM_OLLAMA_BASE_URL', 'http://localhost:11434').rstrip('/')
            self._llm_ollama_model = os.getenv('LLM_OLLAMA_MODEL', 'qwen2.5:7b')
            print(f"     服务地址 : {self._llm_ollama_base}", file=sys.stderr)
            print(f"     模型名称 : {self._llm_ollama_model}", file=sys.stderr)
            print(f"     ℹ️  Ollama LLM 就绪（调用时实时发起 HTTP 请求）", file=sys.stderr)
        elif self._llm_backend == 'openai_compatible':
            if not _OPENAI_AVAILABLE:
                raise ImportError("LLM_BACKEND=openai_compatible 需要安装 openai 库: pip install openai")
            base_url = os.getenv('LLM_API_BASE_URL', 'https://api.openai.com/v1')
            api_key = os.getenv('LLM_API_KEY', 'sk-placeholder')
            key_hint = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 12 else '(已设置)'
            self._llm_openai_client = _OpenAIClient(base_url=base_url, api_key=api_key)
            self._llm_openai_model = os.getenv('LLM_API_MODEL', 'gpt-4o-mini')
            print(f"     API 地址 : {base_url}", file=sys.stderr)
            print(f"     模型名称 : {self._llm_openai_model}", file=sys.stderr)
            print(f"     API Key  : {key_hint}", file=sys.stderr)
            print(f"     ℹ️  OpenAI 兼容 LLM 就绪（按调用计费）", file=sys.stderr)
        else:
            raise ValueError(f"未知 LLM_BACKEND: {self._llm_backend}，可选: ollama / openai_compatible")

    def _load_video_config(self):
        vc = self.config['video']
        self.target_fps = vc.get('target_fps', 0.5)
        self.batch_size = vc.get('batch_size', 4)
        self.frame_resolution = tuple(vc.get('frame_resolution', [512, 512]))
        summary_cfg = vc.get('summary_mode', {})
        self.keyframe_interval_sec = summary_cfg.get('keyframe_interval_sec', 5)
        self.enable_final_summary = summary_cfg.get('enable_final_summary', True)

    # ===================================================================
    # 公开 API — 主入口
    # ===================================================================
        
    def analyze(self, video_path: str, mode: str = 'asr', progress_callback=None) -> Dict:
        """
        分析视频文件 — 自动选择模式
        
        Args:
            video_path: 视频文件路径
            mode: 'asr' | 'visual' | 'hybrid'
            progress_callback: callback(stage, percent, detail)
        
        Returns:
            {
                'mode': str,
                'transcript': dict or None,   # ASR 转写结果
                'summary': str or None,         # 最终总结
                'visual_segments': list or None, # VL 帧分析结果
                'video_info': dict,
            }
        """
        mode = mode or self.config.get('analysis_mode', 'asr')
        
        video_info = self._get_video_info(video_path)
        
        if mode == 'asr':
            return self._analyze_asr(video_path, video_info, progress_callback)
        elif mode == 'visual':
            return self._analyze_visual(video_path, video_info, progress_callback)
        elif mode == 'hybrid':
            return self._analyze_hybrid(video_path, video_info, progress_callback)
        else:
            raise ValueError(f"未知模式: {mode}")
            
    def analyze_from_transcript(self, transcript: Dict, progress_callback=None) -> Dict:
        """
        基于已有的转写文本进行 LLM 总结（浏览器模式：先收集完文字再总结）

        Args:
            transcript: speech_recognizer 返回的转写结果
            progress_callback: 进度回调
        
        Returns:
            { 'summary': str, ... }
        """
        full_text = transcript.get('full_text', '')
        segments = transcript.get('segments', [])
        
        if not full_text.strip():
            return {'summary': None, 'error': '转写文本为空'}
                
        if progress_callback:
            progress_callback('summarizing', 50, '正在生成内容总结...')

        summary = self._llm_summarize_asr(transcript)

        return {
            'summary': summary,
            'segment_count': len(segments),
            'char_count': len(full_text),
        }
                    
    # ===================================================================
    # 模式1: ASR 优先模式（推荐）
    # ===================================================================

    def _analyze_asr(self, video_path: str, video_info: Dict, cb=None) -> Dict:
        """纯 ASR 模式：音频转写 → LLM 总结"""
        asr = self._get_asr()
        
        # 阶段1: 音频提取 + 语音转写
        if cb:
            cb('extract_audio', 5, '🔧 正在提取音频...')
        if cb:
            cb('transcribing', 10, '🎤 正在进行语音识别...')
        
        transcript = asr.transcribe_video(
            video_path,
            progress_callback=lambda stage, pct, detail: (
                cb(stage, pct, detail) if cb else None
            )
        )

        # 阶段2: LLM 总结
        if cb:
            cb('summarizing', 80, '🧠 正在生成内容总结...')

        summary = self._llm_summarize_asr(transcript)
                
        if cb:
            cb('done', 100, '✅ 分析完成!')
                        
        return {
            'mode': 'asr',
            'transcript': transcript,
            'summary': summary,
            'visual_segments': None,
            'video_info': video_info,
        }
                    
    # ===================================================================
    # 模式2: 纯视觉模式
    # ===================================================================
                    
    def _analyze_visual(self, video_path: str, video_info: Dict, cb=None) -> Dict:
        """纯视觉模式：关键帧采样 → VL 分析 → 总结"""
        segments, first_frame = self._extract_keyframes(video_path, video_info, cb)
                
        summary = None
        if self.enable_final_summary and segments:
            if cb:
                cb('summarizing', 80, '🧠 正在生成视觉总结...')
            meaningful = [s for s in segments if len(s.get('content', '').replace(' ', '')) > 20]
            if meaningful:
                summary = self._generate_visual_summary(meaningful, video_info, first_frame)
                        
        if cb:
            cb('done', 100, '✅ 分析完成!')
                
        return {
            'mode': 'visual',
            'transcript': None,
            'summary': summary,
            'visual_segments': segments,
            'video_info': video_info,
        }
        
    # ===================================================================
    # 模式3: 混合模式
    # ===================================================================
            
    def _analyze_hybrid(self, video_path: str, video_info: Dict, cb=None) -> Dict:
        """混合模式：ASR 转写 + VL 关键帧辅助 → 综合总结"""
        asr = self._get_asr()
        
        # 并行执行 ASR 和 VL（串行简化版：先 ASR 再 VL）
        # 阶段1: ASR
        if cb:
            cb('asr', 5, '🎤 正在转写语音...')
        transcript = asr.transcribe_video(video_path)
            
        # 阶段2: VL 关键帧（可选，取少量帧辅助）
        if cb:
            cb('visual', 50, '📸 正在提取关键帧...')
        vl_segments, first_frame = self._extract_keyframes_few(video_path, video_info, max_frames=6)

        # 阶段3: 综合 LLM 总结
        if cb:
            cb('summarizing', 85, '🧠 正在生成综合总结...')

        summary = self._llm_summarize_hybrid(transcript, vl_segments, video_info, first_frame)

        if cb:
            cb('done', 100, '✅ 分析完成!')

        return {
            'mode': 'hybrid',
            'transcript': transcript,
            'summary': summary,
            'visual_segments': vl_segments,
            'video_info': video_info,
        }

    # ===================================================================
    # LLM 总结方法
    # ===================================================================

    def _llm_summarize_asr(self, transcript: Dict) -> str:
        """基于 ASR 转写文本做 LLM 总结"""
        full_text = transcript.get('full_text', '')
        segments = transcript.get('segments', [])
        duration = transcript.get('duration', '?')
        language = transcript.get('language', '')

        # 构建带时间戳的上下文
        context_parts = [
            f"视频时长: {duration}s, 语言: {language}",
            f"共 {len(segments)} 个语音片段\n",
            "= 语音转写文本（含时间戳）=\n",
        ]

        for seg in segments:
            start = seg.get('start', 0)
            end = seg.get('end', 0)
            text = seg.get('text', '').strip()
            context_parts.append(f"[{self._format_timestamp(start)}-{self._format_timestamp(end)}] {text}\n")
            
        full_context = ''.join(context_parts)
        
        # 截断到合理长度
        max_chars = 8000
        if len(full_context) > max_chars:
            header = '\n'.join(context_parts[:4])
            full_context = header + '\n... (中间部分省略) ...\n\n' + full_context[-(max_chars - len(header) - 200):]

        prompt = ASR_SUMMARY_PROMPT + "\n\n---\n\n" + full_context

        return self._call_llm(prompt, max_tokens=self.summary_max_tokens)

    def _llm_summarize_hybrid(self, transcript: Dict, vl_segments: List[Dict],
                                video_info: Dict, anchor_image=None) -> str:
        """综合 ASR 转写 + VL 关键帧信息做总结"""
        # ASR 部分
        asr_text = transcript.get('full_text', '')[:4000]

        # VL 部分
        vl_text = ''
        for seg in vl_segments[:6]:
            ts = seg.get('time_str', '')
            content = seg.get('content', '')[:200]
            vl_text += f"[{ts}] {content}\n"

        prompt = f"""你是一个专业的内容总结专家。请综合以下两种来源的信息，生成一份完整的视频内容分析报告：

来源1 - 语音转写（演讲者说的内容）：
{asr_text}

来源2 - 画面关键帧描述（屏幕上显示的文字和数据）：
{vl_text}

请按以下格式输出：

## 📋 视频摘要
（3-5句话概括）

## 🔑 核心要点
- 要点列表

## 🏷️ 关键词
关键词列表

## 📝 详细内容
分时间段详细总结"""

        return self._call_llm(prompt, max_tokens=self.summary_max_tokens, image=anchor_image)

    def _call_llm(self, text_prompt: str, max_tokens: int = 1024, image=None) -> Optional[str]:
        """
        统一 LLM/VLM 调用入口。
        - image 为 None 时：使用 LLM 后端做纯文本推理（增量总结 / ASR 总结）
        - image 不为 None 时：使用 VLM 后端做图文推理（视频总结辅助）
        """
        if image is not None:
            # 需要图像的调用走 VLM 后端
            return self._vlm_infer_text(text_prompt, image, max_tokens)
        else:
            # 纯文本调用走 LLM 后端
            return self._call_text_llm(text_prompt, max_tokens)

    def _call_text_llm(self, prompt: str, max_tokens: int = 1024) -> Optional[str]:
        """调用纯文本 LLM 做总结（增量总结 / ASR 总结）"""
        try:
            if self._llm_backend == 'ollama':
                return self._llm_ollama_chat(prompt, max_tokens)
            elif self._llm_backend == 'openai_compatible':
                return self._llm_openai_chat(prompt, max_tokens)
        except Exception as e:
            print(f"⚠️ LLM 推理失败 ({e})", file=sys.stderr)
        return None

    def _llm_ollama_chat(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Ollama HTTP API 纯文本推理"""
        import urllib.request
        payload = json.dumps({
            'model': self._llm_ollama_model,
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False,
            'options': {'num_predict': max_tokens},
        }).encode()
        req = urllib.request.Request(
            f"{self._llm_ollama_base}/api/chat",
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data['message']['content'].strip()

    def _llm_openai_chat(self, prompt: str, max_tokens: int) -> Optional[str]:
        """OpenAI 兼容接口纯文本推理"""
        resp = self._llm_openai_client.chat.completions.create(
            model=self._llm_openai_model,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()

    def _vlm_infer_text(self, prompt: str, image, max_tokens: int) -> Optional[str]:
        """带图像的 VLM 推理（用于视觉总结辅助，保持兼容旧 _call_llm 行为）"""
        try:
            if self._vlm_backend == 'huggingface':
                return self._hf_infer_single(image, prompt, max_tokens)
            elif self._vlm_backend in ('ollama', 'openai_compatible'):
                # 将 PIL Image 转 base64
                b64 = self._pil_to_base64(image)
                if self._vlm_backend == 'ollama':
                    return self._ollama_vlm_single(prompt, b64, max_tokens)
                else:
                    return self._openai_vlm_single(prompt, b64, max_tokens)
        except Exception as e:
            print(f"⚠️ VLM 带图推理失败 ({e})", file=sys.stderr)
        return None

    # ===================================================================
    # 视觉分析方法（复用之前的逻辑）
    # ===================================================================

    def _get_video_info(self, video_path: str) -> Dict:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration_sec = total_frames / fps
        cap.release()
        return {
            'filename': os.path.basename(video_path),
            'total_frames': total_frames,
            'fps': round(fps, 1),
            'duration_sec': round(duration_sec, 1),
            'duration_str': self._format_timestamp(duration_sec),
        }

    def _extract_keyframes(self, video_path: str, video_info: Dict, cb=None):
        """提取所有关键帧并分析（用于 visual 模式）"""
        cap = cv2.VideoCapture(video_path)
        fps = video_info['fps']
        total_frames = video_info['total_frames']

        frame_interval = max(1, int(fps * self.keyframe_interval_sec))
        sampled_count = (total_frames // frame_interval) + 1
        print(f"   📸 关键帧采样: 每 {self.keyframe_interval_sec}s 一帧, 预计 ~{sampled_count} 帧", file=sys.stderr)

        segments = []
        frame_batch = []
        time_batch = []
        idx = 0
        processed = 0
        first_frame_pil = None

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if idx % frame_interval == 0:
                time_sec = idx / fps
                processed_frame = self._preprocess_frame(frame)
                frame_batch.append(processed_frame)
                time_batch.append(time_sec)
                if first_frame_pil is None:
                    first_frame_pil = processed_frame

                if len(frame_batch) >= self.batch_size:
                    batch_results = self._analyze_frame_batch_content(frame_batch, time_batch)
                    segments.extend(batch_results)
                    processed += len(frame_batch)
                    if cb:
                        cb('visual', int(processed / max(sampled_count, 1) * 70), f"📸 内容提取中 {processed}/{sampled_count}")
                    frame_batch = []
                    time_batch = []
            idx += 1

        if frame_batch:
            batch_results = self._analyze_frame_batch_content(frame_batch, time_batch)
            segments.extend(batch_results)

        cap.release()
        print(f"✅ 关键帧分析完成: {len(segments)} 帧", file=sys.stderr)
        return segments, first_frame_pil

    def _extract_keyframes_few(self, video_path: str, video_info: Dict, max_frames=6):
        """只提取少量关键帧（用于 hybrid 模式的辅助）"""
        cap = cv2.VideoCapture(video_path)
        fps = video_info['fps'] or 30.0
        total_frames = video_info['total_frames']
        duration = video_info['duration_sec'] or (total_frames / fps)

        interval = max(1, int(total_frames / max_frames))
        frames = []
        timestamps = []
        first_frame = None
        idx = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if idx % interval == 0:
                pf = self._preprocess_frame(frame)
                frames.append(pf)
                timestamps.append(idx / fps)
                if first_frame is None:
                    first_frame = pf
                if len(frames) >= max_frames:
                    break
            idx += 1

        cap.release()

        results = self._analyze_frame_batch_content(frames, timestamps) if frames else []
        return results, first_frame

    def _analyze_frame_batch_content(self, frames, timestamps):
        n = len(frames)
        if n == 0:
            return []
        prompts = [STAGE1_CONTENT_PROMPT] * n
        descriptions = self._model_infer(frames, prompts)
        results = []
        for ts, desc in zip(timestamps, descriptions):
            results.append({
                'time_sec': round(ts, 1),
                'time_str': self._format_timestamp(ts),
                'content': desc,
            })
        return results

    def _generate_visual_summary(self, segments, video_info, first_frame=None):
        """视觉模式的阶段2总结"""
        meaningful = [s for s in segments if len(s.get('content', '').replace(' ', '')) > 20]
        if not meaningful:
            return None

        context_parts = [f"视频时长: {video_info.get('duration_str', '?')}\n"]
        context_parts.append(f"有效帧数: {len(meaningful)}/{len(segments)}\n\n")
        for seg in meaningful:
            context_parts.append(f"[{seg['time_str']}] {seg['content'].strip()}\n\n")

        full_context = ''.join(context_parts)[:6000]
        final_prompt = STAGE2_SUMMARY_PROMPT + "\n\n---\n\n" + full_context

        try:
            anchor = first_frame or Image.new('RGB', (512, 512), color=(64, 64, 64))
            return self._call_llm(final_prompt, max_tokens=self.summary_max_tokens, image=anchor)
        except Exception as e:
            print(f"⚠️ 视觉总结失败 ({e})", file=sys.stderr)
            return None

    # ===================================================================
    # 浏览器帧分析 API（保持兼容）
    # ===================================================================

    def analyze_base64_frames(self, frames_data: List[Dict]) -> List[Dict]:
        """分析来自浏览器的 base64 编码帧（实时模式）"""
        frames = []
        timestamps = []
        for fd in frames_data:
            img = self._decode_base64_image(fd['base64'])
            img = self._preprocess_pil_image(img)
            frames.append(img)
            timestamps.append(fd.get('timestamp', time.time()))
        return self._analyze_frame_batch_content(frames, timestamps)

    def analyze_single_frame(self, base64_data: str, prompt: str = None) -> str:
        """分析单帧图像"""
        img = self._decode_base64_image(base64_data)
        img = self._preprocess_pil_image(img)
        if prompt is None:
            prompt = STAGE1_CONTENT_PROMPT
        result = self._model_infer([img], [prompt])
        return result[0] if result else ""

    def incremental_summary(self, new_frames: list, prev_summary: str = None,
                             video_title: str = '', total_frames_seen: int = 0) -> str:
        """
        增量总结：基于最新一批帧的分析结果 + 上次总结，生成新的滚动总结。

        Args:
            new_frames: 最新一批帧的分析结果列表，每项: {time_str, time_sec, content}
            prev_summary: 上一次的总结文本（首次为 None）
            video_title: 视频标题（用于上下文）
            total_frames_seen: 截至目前共分析了多少帧（用于进度提示）

        Returns:
            新的总结字符串（Markdown 格式）
        """
        # 过滤掉无内容的帧
        meaningful = [f for f in new_frames if len(f.get('content', '').replace(' ', '')) > 10]
        if not meaningful:
            return prev_summary or ''

        t_start = meaningful[0].get('time_str', '?')
        t_end = meaningful[-1].get('time_str', '?')

        # ── 构建帧摘要（每帧只取前200字，避免超出上下文）
        new_lines = []
        for f in meaningful:
            raw = f.get('content', '').strip()
            # 截取前 200 字，避免帧内容过长撑爆上下文
            short = raw[:200] + ('...' if len(raw) > 200 else '')
            new_lines.append(f"[{f.get('time_str', '?')}] {short}")
        new_text = '\n'.join(new_lines)

        # ── prev_summary 截断（保留最近 600 字，避免 prompt 过长）
        prev_ctx = ''
        if prev_summary:
            prev_ctx = prev_summary[-600:] if len(prev_summary) > 600 else prev_summary

        if prev_ctx:
            prompt = (
                "你是专业视频内容分析师。用中文把以下[已有总结]和[新增帧]整合成一份新总结。\n\n"
                f"已有总结：\n{prev_ctx}\n\n"
                f"新增{len(meaningful)}帧（{t_start}~{t_end}）：\n{new_text}\n\n"
                "输出格式（直接填写内容，不要输出方括号说明）：\n\n"
                "## 📋 当前进度摘要\n"
                f"[视频从开头到{t_end}的核心内容，2-4句]\n\n"
                "## 🔑 核心要点（累计）\n"
                "[列举3-6条具体要点，每条以- 开头]\n\n"
                f"## 📝 最新内容（{t_start} - {t_end}）\n"
                f"[{t_start}到{t_end}新增画面的具体描述，2-4句]\n\n"
                "总结："
            )
        else:
            prompt = (
                "你是专业视频内容分析师。用中文总结以下视频画面。\n\n"
                f"视频：{video_title or '未知'}\n"
                f"画面（{t_start}~{t_end}，{len(meaningful)}帧）：\n{new_text}\n\n"
                "输出格式（直接填写内容，不要输出方括号说明）：\n\n"
                "## 📋 当前进度摘要\n"
                f"[视频开头到{t_end}的核心内容，2-4句]\n\n"
                "## 🔑 核心要点\n"
                "[列举3-6条具体要点，每条以- 开头]\n\n"
                f"## 📝 最新内容（{t_start} - {t_end}）\n"
                "[这段画面的具体描述，2-4句]\n\n"
                "总结："
            )

        result = self._call_llm(prompt, max_tokens=768)
        if not result:
            return prev_summary or ''

        # 清理：如果模型重复输出了 "总结：" 前缀，去掉
        result = result.lstrip()
        if result.startswith('总结：') or result.startswith('总结:'):
            result = result[3:].lstrip()

        return result

    # ===================================================================
    # 内部工具方法
    # ===================================================================

    def _preprocess_frame(self, cv2_frame):
        rgb = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        return self._preprocess_pil_image(pil_img)

    def _preprocess_pil_image(self, pil_img):
        w, h = self.frame_resolution
        return pil_img.resize((w, h), Image.Resampling.LANCZOS)

    def _decode_base64_image(self, b64_str):
        img_bytes = base64.b64decode(b64_str)
        return Image.open(BytesIO(img_bytes)).convert('RGB')

    # ===================================================================
    # VLM 帧推理实现（各后端）
    # ===================================================================

    def _model_infer(self, images, texts):
        """批量 VLM 推理入口，根据后端分发"""
        if self._vlm_backend == 'huggingface':
            return self._hf_batch_infer(images, texts)
        elif self._vlm_backend == 'ollama':
            return self._ollama_batch_infer(images, texts)
        elif self._vlm_backend == 'openai_compatible':
            return self._openai_batch_infer(images, texts)
        return [f"[不支持的 VLM 后端: {self._vlm_backend}]"] * len(images)

    # ── HuggingFace ──

    def _hf_batch_infer(self, images, texts):
        """HuggingFace 批量推理"""
        messages_per_image = [
            [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': t}]}]
            for t in texts
        ]
        try:
            texts_processed = [
                self.processor.apply_chat_template(msg, tokenize=False, add_generation_prompt=True)
                for msg in messages_per_image
            ]
            inputs = self.processor(
                text=texts_processed, images=images, return_tensors='pt', padding=True
            ).to(self.device)
            with torch.no_grad():
                output_ids = self.hf_model.generate(
                    **inputs, max_new_tokens=self.max_new_tokens, do_sample=False
                )
            generated_ids = [
                out_ids[len(in_ids):]
                for in_ids, out_ids in zip(inputs.input_ids, output_ids)
            ]
            return self.processor.batch_decode(
                generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )
        except Exception as e:
            print(f"⚠️ HF 批量推理失败 ({e}), 切换逐帧...", file=sys.stderr)
            results = []
            for img, text in zip(images, texts):
                try:
                    results.append(self._hf_infer_single(img, text, self.max_new_tokens))
                except Exception as e2:
                    results.append(f"[分析错误: {e2}]")
            return results

    def _hf_infer_single(self, image, text, max_tokens=None):
        """HuggingFace 单帧推理"""
        if max_tokens is None:
            max_tokens = self.max_new_tokens
        messages = [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': text}]}]
        text_processed = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(
            text=[text_processed], images=[image], return_tensors='pt', padding=True
        ).to(self.device)
        with torch.no_grad():
            output_ids = self.hf_model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        generated = output_ids[0][inputs.input_ids.shape[1]:]
        return self.processor.decode(generated, skip_special_tokens=True, clean_up_tokenization_spaces=True)

    # ── Ollama ──

    def _ollama_batch_infer(self, images, texts):
        """Ollama 逐帧推理（不支持真正的批处理）"""
        results = []
        for img, text in zip(images, texts):
            try:
                b64 = self._pil_to_base64(img)
                results.append(self._ollama_vlm_single(text, b64, self.max_new_tokens))
            except Exception as e:
                results.append(f"[Ollama 推理错误: {e}]")
        return results

    def _ollama_vlm_single(self, prompt: str, image_b64: str, max_tokens: int) -> str:
        """Ollama VLM 单次推理（HTTP API）"""
        import urllib.request
        payload = json.dumps({
            'model': self._vlm_ollama_model,
            'messages': [{'role': 'user', 'content': prompt, 'images': [image_b64]}],
            'stream': False,
            'options': {'num_predict': max_tokens},
        }).encode()
        req = urllib.request.Request(
            f"{self._vlm_ollama_base}/api/chat",
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data['message']['content'].strip()

    # ── OpenAI 兼容 ──

    def _openai_batch_infer(self, images, texts):
        """OpenAI 兼容 VLM 逐帧推理"""
        results = []
        for img, text in zip(images, texts):
            try:
                b64 = self._pil_to_base64(img)
                results.append(self._openai_vlm_single(text, b64, self.max_new_tokens))
            except Exception as e:
                results.append(f"[API 推理错误: {e}]")
        return results

    def _openai_vlm_single(self, prompt: str, image_b64: str, max_tokens: int) -> str:
        """OpenAI 兼容 VLM 单次推理"""
        resp = self._vlm_openai_client.chat.completions.create(
            model=self._vlm_openai_model,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}},
                    {'type': 'text', 'text': prompt},
                ],
            }],
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()

    # ── 辅助工具 ──

    @staticmethod
    def _pil_to_base64(pil_img: Image.Image) -> str:
        """PIL Image → base64 JPEG 字符串"""
        buf = io.BytesIO()
        pil_img.convert('RGB').save(buf, format='JPEG', quality=85)
        return base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def _format_timestamp(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
