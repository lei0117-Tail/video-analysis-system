"""
语音识别模块 — 基于 faster-whisper 的本地 ASR
- 从视频文件提取音频 (ffmpeg)
- 语音转文字 (faster-whisper / Whisper)
- 支持时间戳对齐、分段输出
- 结果可写入缓存文件供后续 LLM 总结
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional


class SpeechRecognizer:
    """本地语音识别器 — 基于 faster-whisper (CTranslate2 加速)"""

    def __init__(self, config: Dict):
        self.config = config
        asr_cfg = config.get('asr', {})

        self.model_size = asr_cfg.get('model_size', 'medium')
        self.device = asr_cfg.get('device', 'auto')
        self.compute_type = asr_cfg.get('compute_type', 'float32')
        self.language = asr_cfg.get('language', 'zh')  # zh=中文 auto=自动检测
        self.cache_dir = Path(asr_cfg.get('cache_dir', ''))

        self._model = None

    def _load_model(self):
        """懒加载 Whisper 模型"""
        if self._model is not None:
            return

        from faster_whisper import WhisperModel

        print(f"🎤 加载 ASR 模型: whisper-{self.model_size} (device={self.device}, compute={self.compute_type})", file=sys.stderr)

        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )
        print(f"✅ ASR 模型就绪", file=sys.stderr)

    def extract_audio(self, video_path: str, output_wav: str = None) -> str:
        """
        从视频文件中提取音频为 WAV 格式

        Args:
            video_path: 视频文件路径
            output_wav: 输出 wav 路径，默认为临时文件

        Returns:
            提取的音频文件路径
        """
        if output_wav is None:
            output_wav = tempfile.mktemp(suffix='.wav', prefix='video_audio_')

        print(f"🔧 提取音频: {video_path} → {output_wav}", file=sys.stderr)

        # 使用 ffmpeg 提取音频: 16kHz mono WAV (Whisper 最优输入格式)
        cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-vn',                    # 不包含视频流
            '-acodec', 'pcm_s16le',   # PCM 16-bit little-endian
            '-ar', '16000',           # 采样率 16kHz
            '-ac', '1',               # 单声道
            output_wav,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 音频提取失败: {result.stderr}")

        # 检查输出文件大小
        file_size = os.path.getsize(output_wav)
        if file_size < 1000:
            raise RuntimeError(f"音频文件过小 ({file_size} bytes)，视频可能没有音频轨道")

        print(f"   ✅ 音频提取完成 ({file_size / 1024 / 1024:.1f} MB)", file=sys.stderr)
        return output_wav

    def transcribe(self, audio_path: str, progress_callback=None) -> List[Dict]:
        """
        对音频文件进行语音识别

        Args:
            audio_path: 音频文件路径 (wav/mp3/m4a等)
            progress_callback: 可选回调 callback(percent, text_fragment)

        Returns:
            识别结果列表，每项包含 {start, end, text}
        """
        self._load_model()

        print(f"🎤 开始语音转写: {audio_path}", file=sys.stderr)
        t0 = time.time()

        segments_info = []
        full_text_parts = []

        # 执行转写
        segments, info = self._model.transcribe(
            audio_path,
            language=self.language if self.language != 'auto' else None,
            beam_size=5,
            vad_filter=True,          # 启用 VAD 语音活动检测
            vad_parameters=dict(
                min_silence_duration_ms=500,   # 最小静音段 500ms
                speech_pad_ms=300,             # 语音前后各补 300ms
            ),
            word_timestamps=True,     # 词级时间戳
            condition_on_previous_text=True,  # 上下文连贯
            initial_prompt="以下是视频的字幕内容。",  # 初始提示词提升中文效果
        )

        duration = info.duration
        language = info.language
        probability = info.language_probability
        print(f"   📋 检测到语言: {language} (置信度: {probability:.2%})", file=sys.stderr)
        print(f"   ⏱️ 音频时长: {duration:.1f}s", file=sys.stderr)

        for segment in segments:
            seg_data = {
                'start': round(segment.start, 2),
                'end': round(segment.end, 2),
                'text': segment.text.strip(),
            }
            segments_info.append(seg_data)
            full_text_parts.append(seg_data['text'])

            # 进度回调
            if progress_callback and duration > 0:
                percent = int((segment.end / duration) * 100)
                progress_callback(percent, seg_data['text'])

        elapsed = time.time() - t0
        total_text = ''.join(full_text_parts)

        print(f"✅ 语音转写完成 ({elapsed:.1f}s)", file=sys.stderr)
        print(f"   📝 共 {len(segments_info)} 个片段, {len(total_text)} 字符", file=sys.stderr)
        print(f"   ⚡ 实时率: {duration/max(elapsed, 0.1):.1f}x", file=sys.stderr)

        return {
            'segments': segments_info,
            'full_text': total_text,
            'language': language,
            'duration': round(duration, 1),
            'elapsed': round(elapsed, 1),
        }

    def transcribe_video(self, video_path: str, progress_callback=None) -> Dict:
        """
        一站式：从视频中提取音频并转写文字

        Args:
            video_path: 视频文件路径
            progress_callback: 进度回调 callback(stage, percent, detail)

        Returns:
            完整的转写结果字典
        """
        # 阶段1: 提取音频
        if progress_callback:
            progress_callback('extract_audio', 10, '正在提取音频...')

        audio_path = self.extract_audio(video_path)

        try:
            # 阶段2: 语音转写
            if progress_callback:
                progress_callback('transcribing', 20, '正在进行语音识别...')

            result = self.transcribe(audio_path, progress_callback=lambda pct, txt: (
                progress_callback('transcribing', 20 + int(pct * 0.7), f'转写中 {pct}%')
                if progress_callback else None
            ))

            result['audio_path'] = audio_path
            result['video_path'] = video_path

            return result

        finally:
            # 清理临时音频文件
            if audio_path.startswith(tempfile.gettempdir()):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    def save_transcript(self, result: Dict, output_path: str = None) -> str:
        """
        将转写结果保存到 JSON 文件（缓存）

        Args:
            result: transcribe() 返回的结果字典
            output_path: 输出路径，默认为 cache_dir 下同名 .json

        Returns:
            保存的文件路径
        """
        if output_path is None:
            # 默认保存到缓存目录或视频同目录
            video_path = result.get('video_path', '')
            if self.cache_dir and self.cache_dir.exists():
                out_dir = self.cache_dir
            elif video_path:
                out_dir = Path(video_path).parent
            else:
                out_dir = Path('.')

            video_name = Path(video_path).stem if video_path else f'transcript_{int(time.time())}'
            output_path = str(out_dir / f'{video_name}_transcript.json')

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"💾 转写结果已保存: {output_path}", file=sys.stderr)
        return output_path

    def load_transcript(self, transcript_path: str) -> Optional[Dict]:
        """从缓存文件加载之前的转写结果"""
        if not os.path.exists(transcript_path):
            return None
        with open(transcript_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def append_to_cache(self, text_chunk: Dict, cache_path: str) -> None:
        """
        追加一段转写文本到缓存文件（浏览器实时模式用）

        Args:
            text_chunk: {'start': float, 'end': float, 'text': str}
            cache_path: 缓存文件路径
        """
        # 确保目录存在
        os.makedirs(os.path.dirname(cache_path) or '.', exist_ok=True)

        # 读取已有内容或创建新结构
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {'segments': [], 'full_text': '', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S')}

        data['segments'].append(text_chunk)
        data['full_text'] += text_chunk['text']
        data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ========== 便捷函数 ==========

def extract_and_transcribe(video_path: str, config: Dict, progress_callback=None) -> Dict:
    """便捷函数：一站式视频语音转写"""
    recognizer = SpeechRecognizer(config)
    return recognizer.transcribe_video(video_path, progress_callback)

