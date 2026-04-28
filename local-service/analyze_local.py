#!/usr/bin/env python3
"""
本地视频分析独立脚本 — v2.1
不依赖 Chrome 扩展，直接在终端分析本地视频文件并保存为 Markdown。

分析模式:
  asr    — 语音转写 → LLM 结构化总结（推荐，速度快，信息最全）
  visual — 关键帧采样 → VLM 逐帧分析 → 总结（适合无音频视频）
  hybrid — ASR + VLM 双模型综合分析（最全面，耗时最长）

用法:
  cd local-service
  source ../.venv/bin/activate   # 激活虚拟环境
  python analyze_local.py <视频路径> [选项]

示例:
  python analyze_local.py ../test.mp4
  python analyze_local.py ../test.mp4 --mode hybrid --title "财报解读"
  python analyze_local.py ../test.mp4 --output ./notes --title "AI 每日速览" --mode asr
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── 确保能导入同目录模块 ──
sys.path.insert(0, str(Path(__file__).parent))

# ── 从项目根目录加载 .env（模型后端配置） ──
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / '.env', override=False)

from video_analyzer import VideoAnalyzer
from note_saver import NoteSaver


# =====================================================================
# 启动信息展示
# =====================================================================

def _print_banner(args, config: dict):
    """打印启动信息：展示当前后端配置和模型详情"""
    vlm_backend = os.environ.get('VLM_BACKEND', 'huggingface')
    llm_backend = os.environ.get('LLM_BACKEND', 'ollama')
    asr_cfg = config.get('asr', {})

    # ── VLM 描述 ──
    if vlm_backend == 'huggingface':
        vlm_model = os.environ.get('VLM_HF_MODEL', config.get('model', {}).get('name', 'Qwen/Qwen2-VL-2B-Instruct'))
        vlm_desc = f"HuggingFace 本地  →  {vlm_model}"
        vlm_note = "(首次运行需从磁盘/HuggingFace 加载，可能耗时数分钟)"
    elif vlm_backend == 'ollama':
        vlm_base = os.environ.get('VLM_OLLAMA_BASE_URL', 'http://localhost:11434')
        vlm_model = os.environ.get('VLM_OLLAMA_MODEL', 'qwen2-vl:7b')
        vlm_desc = f"Ollama ({vlm_base})  →  {vlm_model}"
        vlm_note = "(需确保 ollama serve 正在运行)"
    elif vlm_backend == 'openai_compatible':
        vlm_base = os.environ.get('VLM_API_BASE_URL', 'https://api.openai.com/v1')
        vlm_model = os.environ.get('VLM_API_MODEL', 'gpt-4o')
        vlm_desc = f"OpenAI 兼容 ({vlm_base})  →  {vlm_model}"
        vlm_note = "(按调用计费)"
    else:
        vlm_desc = f"未知后端: {vlm_backend}"
        vlm_note = ""

    # ── LLM 描述 ──
    if llm_backend == 'ollama':
        llm_base = os.environ.get('LLM_OLLAMA_BASE_URL', 'http://localhost:11434')
        llm_model = os.environ.get('LLM_OLLAMA_MODEL', 'qwen2.5:7b')
        llm_desc = f"Ollama ({llm_base})  →  {llm_model}"
    elif llm_backend == 'openai_compatible':
        llm_base = os.environ.get('LLM_API_BASE_URL', 'https://api.openai.com/v1')
        llm_model = os.environ.get('LLM_API_MODEL', 'gpt-4o-mini')
        llm_desc = f"OpenAI 兼容 ({llm_base})  →  {llm_model}"
    else:
        llm_desc = f"未知后端: {llm_backend}"

    mode_desc = {
        'asr': '语音转写 → LLM 总结',
        'visual': 'VLM 关键帧分析 → 总结',
        'hybrid': 'ASR + VLM 双模型综合',
    }.get(args.mode, args.mode)

    print("=" * 62)
    print("  🎬  AI 视频内容分析工具 v2.1  —  本地模式")
    print("-" * 62)
    print(f"  视频文件  : {args.video}")
    print(f"  输出目录  : {args.output or '(与视频同目录)'}")
    print(f"  标题      : {args.title or '(自动取文件名)'}")
    print(f"  分析模式  : {args.mode}  —  {mode_desc}")
    print("-" * 62)
    if args.mode in ('asr', 'hybrid'):
        print(f"  ASR 引擎  : faster-whisper-{asr_cfg.get('model_size', 'medium')}")
        print(f"             语言={asr_cfg.get('language', 'auto')}  "
              f"设备={asr_cfg.get('device', 'auto')}  "
              f"精度={asr_cfg.get('compute_type', 'float32')}")
    if args.mode in ('visual', 'hybrid'):
        print(f"  VLM 引擎  : {vlm_desc}")
        if vlm_note:
            print(f"              {vlm_note}")
    print(f"  LLM 引擎  : {llm_desc}")
    print("=" * 62)
    print()


# =====================================================================
# 进度回调
# =====================================================================

def _make_progress_callback():
    """返回一个打印进度条的回调函数"""
    stage_map = {
        'extract_audio': '🔧 提取音频',
        'transcribing':  '🎤 语音识别',
        'summarizing':   '🧠 LLM 总结',
        'visual':        '📸 帧分析  ',
        'done':          '✅ 完成    ',
    }
    bar_len = 32

    def on_progress(stage, percent, detail):
        filled = int(bar_len * percent / 100)
        bar = '█' * filled + '░' * (bar_len - filled)
        label = stage_map.get(stage, f'{stage:<10}')
        print(f"\r  [{bar}] {percent:3d}%  {label}  {detail[:40]}", end='', flush=True)

    return on_progress


# =====================================================================
# 结果打印
# =====================================================================

def _print_result(result: dict, init_time: float, analysis_time: float):
    """打印分析完成后的摘要统计"""
    transcript = result.get('transcript')
    visual_segments = result.get('visual_segments')
    summary = result.get('summary')

    print(f"\n\n{'=' * 62}")
    print("  ✅  分析完成")
    print(f"  模式          : {result.get('mode', '?')}")
    if transcript:
        segs = transcript.get('segments', [])
        chars = len(transcript.get('full_text', ''))
        lang = transcript.get('language', '?')
        elapsed_asr = transcript.get('elapsed', '?')
        print(f"  ASR 转写      : {len(segs)} 片段 / {chars} 字  语言={lang}  耗时={elapsed_asr}s")
    if visual_segments:
        print(f"  VLM 关键帧    : {len(visual_segments)} 帧")
    print(f"  总结生成      : {'✅ 已生成' if summary else '❌ 跳过'}")
    print(f"  模型初始化    : {init_time:.1f}s")
    print(f"  内容分析      : {analysis_time:.1f}s")
    print(f"  总耗时        : {init_time + analysis_time:.1f}s")


# =====================================================================
# 命令行入口
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='analyze_local',
        description='AI 视频内容分析 — 本地文件模式（无需浏览器）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
分析模式说明:
  asr     语音转写 → LLM 结构化总结（推荐；速度快，信息最全）
  visual  VLM 关键帧分析 → 总结（适合静默视频 / 教程截图类内容）
  hybrid  ASR + VLM 双引擎综合分析（最全面，耗时最长）

.env 配置示例 (项目根目录 .env):
  VLM_BACKEND=huggingface    # huggingface | ollama | openai_compatible
  LLM_BACKEND=openai_compatible
  LLM_API_BASE_URL=https://api.deepseek.com/v1
  LLM_API_KEY=sk-xxxx
  LLM_API_MODEL=deepseek-chat
        """,
    )
    parser.add_argument('video', help='视频文件路径（支持 mp4/avi/mkv/mov 等）')
    parser.add_argument('--mode', '-m',
                        choices=['asr', 'visual', 'hybrid'],
                        default=None,
                        help='分析模式（默认使用 config.json 中的 analysis_mode）')
    parser.add_argument('--output', '-o',
                        default='',
                        metavar='DIR',
                        help='输出目录（默认与视频同目录）')
    parser.add_argument('--title', '-t',
                        default='',
                        help='笔记标题（默认使用文件名）')
    parser.add_argument('--config', '-c',
                        default=str(Path(__file__).parent / 'config.json'),
                        metavar='PATH',
                        help='config.json 路径（默认 local-service/config.json）')
    parser.add_argument('--note', '-n',
                        default='',
                        metavar='TEXT',
                        help='附加到笔记开头的个人备注')
    return parser


def main():
    parser = build_parser()

    # 兼容旧的位置参数调用方式：analyze_local.py <video> [output] [title] [mode]
    # 如果首个非选项参数看起来不像 --flag，且后续参数也不是 --flag，则按旧风格解析
    if len(sys.argv) >= 2 and not sys.argv[1].startswith('-'):
        # 尝试按新格式解析；若参数数量 ≤ 5 且全部不含 -- 则可能是旧式调用
        old_style = all(not a.startswith('-') for a in sys.argv[1:])
        if old_style and len(sys.argv) <= 5:
            args = argparse.Namespace(
                video=sys.argv[1],
                output=sys.argv[2] if len(sys.argv) > 2 else '',
                title=sys.argv[3] if len(sys.argv) > 3 else '',
                mode=sys.argv[4] if len(sys.argv) > 4 else None,
                config=str(Path(__file__).parent / 'config.json'),
                note='',
            )
        else:
            args = parser.parse_args()
    else:
        args = parser.parse_args()

    # ── 验证文件 ──
    video_path = Path(args.video)
    if not video_path.exists():
        print(f"❌ 文件不存在: {video_path}", file=sys.stderr)
        sys.exit(1)
    if not video_path.is_file():
        print(f"❌ 路径不是文件: {video_path}", file=sys.stderr)
        sys.exit(1)

    # ── 加载 config.json ──
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 模式：命令行 > config.json > 默认 asr
    mode = args.mode or config.get('analysis_mode', 'asr')

    # ── 打印启动信息 ──
    args.mode = mode  # 统一存回 args 供 _print_banner 使用
    _print_banner(args, config)

    # ── 初始化分析器（含模型加载） ──
    print("⏳ 正在初始化模型...\n")
    t0 = time.time()
    try:
        analyzer = VideoAnalyzer(config)
    except Exception as e:
        print(f"\n❌ 模型初始化失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("   请检查 .env 配置及依赖是否安装正确。", file=sys.stderr)
        sys.exit(1)
    init_time = time.time() - t0
    print(f"\n✅ 模型就绪（耗时 {init_time:.1f}s）\n")

    # ── 分析视频 ──
    print("▶️  开始分析视频...\n")
    t1 = time.time()
    try:
        result = analyzer.analyze(
            str(video_path),
            mode=mode,
            progress_callback=_make_progress_callback(),
        )
    except Exception as e:
        print(f"\n❌ 分析失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    analysis_time = time.time() - t1

    # ── 打印统计 ──
    _print_result(result, init_time, analysis_time)

    # ── 保存 Markdown ──
    saver = NoteSaver(config)
    title = args.title or video_path.stem
    output_dir = args.output or None

    try:
        filepath = saver.save(
            result=result,
            title=title,
            video_path=str(video_path),
            user_note=args.note,
            output_dir=output_dir,
        )
    except Exception as e:
        print(f"\n❌ 保存失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 62}")
    print(f"  📝 笔记已保存:")
    print(f"     {filepath}")
    print(f"{'=' * 62}\n")


if __name__ == '__main__':
    main()

