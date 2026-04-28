"""
笔记保存模块 — 将视频分析结果保存为本地 Markdown 文件
支持 ASR+总结 / 视觉分析 两种输出格式
"""
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict


def _fmt_ts(seconds):
    """格式化时间戳为 HH:MM:SS 或 MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class NoteSaver:
    """Markdown 笔记保存器"""

    def __init__(self, config: Dict):
        self.config = config
        output_cfg = config.get('output', {})
        self.default_output_dir = output_cfg.get('output_dir', '')
        self.assets_subdir = output_cfg.get('assets_subdir', '.assets')
        self.title_format = output_cfg.get('default_title_format', '{video_name}_内容分析')

    def save(self, result: Dict, title="", video_path="", video_url="",
             user_note="", output_dir=None) -> str:
        """保存分析结果到 Markdown"""
        out_dir = Path(output_dir or self.default_output_dir)
        if not out_dir.is_absolute():
            out_dir = Path(__file__).parent.parent / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        assets_dir = out_dir / self.assets_subdir
        assets_dir.mkdir(exist_ok=True)

        filename = self._generate_filename(title, video_path)
        filepath = out_dir / filename

        md_content = self._build_markdown(result, title, video_path, video_url, user_note)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)

        print(f"📝 笔记已保存: {filepath}", file=sys.stderr)
        return str(filepath.absolute())

    def _generate_filename(self, title, video_path):
        if title:
            base = title
        elif video_path:
            base = Path(video_path).stem
        else:
            base = f"内容分析_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        base = re.sub(r'[\\/:*?"<>|]', '_', base)
        return f"{base[:80]}.md"

    def _build_markdown(self, result, title, video_path, video_url, user_note):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        mode = result.get('mode', 'unknown')
        summary = result.get('summary')
        transcript = result.get('transcript')
        visual_segments = result.get('visual_segments')
        video_info = result.get('video_info', {})

        lines = []

        # --- YAML front matter ---
        lines.append('---')
        if video_url:
            lines.append(f'video_url: {video_url}')
        if video_path:
            lines.append(f'video_path: {video_path}')
        if video_info:
            if video_info.get('duration_str'):
                lines.append(f'duration: {video_info["duration_str"]}')
            if video_info.get('filename'):
                lines.append(f'source: {video_info["filename"]}')
        lines.append(f'analysis_date: {now}')
        lines.append(f'mode: {mode}')
        lines.append(f'has_summary: {"true" if summary else "false"}')
        if transcript:
            lines.append(f'transcript_segments: {len(transcript.get("segments", []))}')
            lines.append(f'transcript_chars: {len(transcript.get("full_text", ""))}')
        if visual_segments:
            lines.append(f'visual_frames: {len(visual_segments)}')
        lines.append('---')
        lines.append('')

        # --- 标题 ---
        lines.append(f'# {title or "AI 视频内容分析"}')
        lines.append('')

        # --- 用户笔记 ---
        if user_note:
            lines.append('## 📝 我的笔记')
            lines.append('')
            lines.append(user_note)
            lines.append('')

        # --- 总结（最优先展示） ---
        if summary:
            lines.append(summary)
            lines.append('')
            lines.append('---')
            lines.append('')

        # --- 完整转写文本 (ASR 模式) ---
        if transcript and transcript.get('segments'):
            lines.append('## 🎙️ 完整转写文本')
            lines.append('')
            lines.append(f'> 共 **{len(transcript["segments"])}** 个语音片段 | '
                        f'语言: {transcript.get("language", "?")} | '
                        f'时长: {transcript.get("duration", "?")}s')
            lines.append('')
            for seg in transcript['segments']:
                start = seg.get('start', 0)
                end = seg.get('end', 0)
                text = seg.get('text', '').strip()
                ts_start = _fmt_ts(start)
                ts_end = _fmt_ts(end)
                lines.append(f"**[{ts_start} → {ts_end}]** {text}")
                lines.append('')
            lines.append('')

        # --- 关键帧记录 (Visual/Hybrid 模式) ---
        if visual_segments:
            lines.append('## 📸 关键帧记录')
            lines.append('')
            lines.append(f'> 共 {len(visual_segments)} 个关键帧')
            lines.append('')
            for seg in visual_segments:
                time_str = seg.get('time_str', seg.get('time_sec', ''))
                content = seg.get('content', '')
                lines.append(f'### ⏱️ {time_str}')
                lines.append('')
                lines.append(content)
                lines.append('')

        if not summary and not transcript and not visual_segments:
            lines.append('*无分析结果*')
            lines.append('')

        return '\n'.join(lines)


def save_results_to_markdown(data: Dict, config: Dict) -> Dict:
    """
    便捷函数：从消息数据格式保存结果（兼容新旧格式）

    新格式: data 中包含 analysis_result (Dict from analyze())
    旧格式: data 中包含 analysis_results (List of frame results)
    """
    try:
        saver = NoteSaver(config)
        analysis_result = data.get('analysis_result') or data.get('analysis_results')

        if isinstance(analysis_result, dict):
            # 新格式: 直接传给 save()
            result = analysis_result
        elif isinstance(analysis_result, list):
            # 旧格式: 转换为新结构
            result = {
                'mode': 'visual',
                'transcript': None,
                'summary': None,
                'visual_segments': [
                    {
                        'time_sec': r.get('time_sec', 0),
                        'time_str': r.get('time_str', ''),
                        'content': r.get('description', ''),
                    }
                    for r in analysis_result
                ],
                'video_info': {},
            }
        else:
            result = {'mode': 'unknown', 'summary': None, 'transcript': None, 'visual_segments': [], 'video_info': {}}

        filepath = saver.save(
            result=result,
            title=data.get('title', ''),
            video_path=data.get('video_path', ''),
            video_url=data.get('url', data.get('video_url', '')),
            user_note=data.get('note', ''),
            output_dir=data.get('output_dir'),
        )
        return {'success': True, 'filepath': filepath}
    except Exception as e:
        return {'success': False, 'error': str(e)}

