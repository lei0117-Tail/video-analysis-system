"""
WebSocket Server — AI 视频分析本地服务 v2.0
替代 Chrome Native Messaging，提供更稳定的 ws://127.0.0.1:PORT 接口

新增功能（v2.0）：
- 每帧推理结果实时写入本地文件 ana/<来源>/<视频ID>/frames.jsonl
- 每 SUMMARY_EVERY_N_FRAMES 帧触发一次增量总结，写入 summary.md
- 增量总结推送回浏览器，浏览器侧展示滚动总结（而非单帧原始结果）
"""
import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import websockets
from websockets.server import ServerConnection

# =====================================================================
# 路径设置 — 确保能 import 同级模块
# =====================================================================
sys.path.insert(0, str(Path(__file__).parent))

# 加载项目根目录的 .env 文件（VLM_BACKEND / LLM_BACKEND 等配置）
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / '.env', override=False)

from video_analyzer import VideoAnalyzer

# =====================================================================
# 配置常量
# =====================================================================
# 每积累多少帧触发一次增量总结（可在 config.json 中覆盖）
DEFAULT_SUMMARY_EVERY_N_FRAMES = 5
# 本地存储根目录（相对于 websocket_server.py 所在目录）
ANA_ROOT = Path(__file__).parent.parent / 'ana'

# =====================================================================
# 全局状态
# =====================================================================
_analyzer: VideoAnalyzer = None
_analyzer_lock = threading.Lock()
_vl_loading = False
_start_time = time.time()
_config: dict = {}

# 所有已连接的 WebSocket 客户端
_clients: set[ServerConnection] = set()
_clients_lock = asyncio.Lock()

# 主事件循环（用于从线程中发消息）
_loop: asyncio.AbstractEventLoop = None

# 活跃的视频会话：video_key → VideoSession
_sessions: dict = {}
_sessions_lock = threading.Lock()


def _log(msg: str):
    ts = time.strftime('%H:%M:%S')
    print(f"[{ts}] [WS] {msg}", file=sys.stderr, flush=True)


# =====================================================================
# URL → 来源 / 视频唯一键 解析
# =====================================================================
def _parse_video_key(url: str, title: str = '') -> tuple[str, str]:
    """
    从 URL 解析 (来源, 视频唯一键)。
    返回 (source, video_key)，均为文件系统安全字符串。

    支持：
    - B 站: bilibili.com/video/BVxxx  → ('bilibili', 'BVxxx')
    - YouTube: youtube.com/watch?v=xxx → ('youtube', 'xxx')
    - 其他: 取 hostname + path 片段
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''

        if 'bilibili' in host:
            # /video/BV1xx... 或 /bangumi/play/ep...
            m = re.search(r'/(BV[a-zA-Z0-9]+|av\d+|ep\d+|ss\d+)', parsed.path)
            vid = m.group(1) if m else _safe_name(parsed.path)
            return 'bilibili', vid

        if 'youtube' in host or 'youtu.be' in host:
            from urllib.parse import parse_qs
            if 'youtu.be' in host:
                vid = parsed.path.strip('/')
            else:
                qs = parse_qs(parsed.query)
                vid = qs.get('v', ['unknown'])[0]
            return 'youtube', vid

        if 'iqiyi' in host:
            m = re.search(r'/(v_[a-zA-Z0-9]+)', parsed.path)
            vid = m.group(1) if m else _safe_name(parsed.path)
            return 'iqiyi', vid

        if 'youku' in host:
            m = re.search(r'/v_show/id_([a-zA-Z0-9==]+)', parsed.path)
            vid = m.group(1) if m else _safe_name(parsed.path)
            return 'youku', vid

        # 通用：hostname_path_hash
        source = _safe_name(host.split('.')[0] if host else 'local')
        vid = _safe_name(parsed.path)[:40] or _safe_name(title)[:40] or 'unknown'
        return source, vid

    except Exception:
        safe_title = _safe_name(title)[:50] or 'unknown'
        return 'unknown', safe_title


def _safe_name(s: str) -> str:
    """转为文件系统安全字符串（保留字母数字下划线横线）"""
    return re.sub(r'[^\w\-]', '_', s).strip('_')


# =====================================================================
# VideoSession — 每个视频的分析会话
# =====================================================================
class VideoSession:
    """
    管理单个视频的实时分析状态：
    - 缓冲已分析的帧，每 N 帧触发增量总结
    - 实时写入 frames.jsonl（追加模式）
    - 每次总结写入 summary.md（覆盖最新）
    - 总结完成后广播给浏览器
    """

    def __init__(self, video_key: str, source: str, url: str, title: str,
                 summary_every_n: int = DEFAULT_SUMMARY_EVERY_N_FRAMES):
        self.video_key = video_key
        self.source = source
        self.url = url
        self.title = title
        self.summary_every_n = summary_every_n

        # 存储路径
        self.dir = ANA_ROOT / source / video_key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames_file = self.dir / 'frames.jsonl'
        self.summary_file = self.dir / 'summary.md'
        self.meta_file = self.dir / 'meta.json'

        # 写入元信息
        self._write_meta()

        # 状态
        self.all_frames: list = []          # 本次会话所有帧
        self.unsummarized_frames: list = [] # 上次总结后新增的帧
        self.prev_summary: str = ''         # 上次总结文本
        self.summary_count: int = 0         # 已触发总结次数
        self.summarizing: bool = False      # 是否正在总结中（避免并发）
        self._lock = threading.Lock()

        # 如果之前有 summary，加载作为 prev_summary（断点续传）
        if self.summary_file.exists():
            try:
                self.prev_summary = self.summary_file.read_text(encoding='utf-8')
                _log(f"[{source}/{video_key}] 加载历史总结 ({len(self.prev_summary)} chars)")
            except Exception:
                pass

        _log(f"[{source}/{video_key}] 会话创建，存储目录: {self.dir}")

    def _write_meta(self):
        meta = {
            'url': self.url,
            'title': self.title,
            'source': self.source,
            'video_key': self.video_key,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        try:
            self.meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            _log(f"写入 meta 失败: {e}")

    def add_frame_result(self, frame_results: list) -> bool:
        """
        添加一批帧的分析结果。
        返回 True 表示达到阈值、需要触发总结。
        """
        with self._lock:
            for r in frame_results:
                self.all_frames.append(r)
                self.unsummarized_frames.append(r)
                # 实时追加写入 frames.jsonl
                try:
                    with open(self.frames_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(r, ensure_ascii=False) + '\n')
                except Exception as e:
                    _log(f"写入 frames.jsonl 失败: {e}")

            should_summarize = (
                not self.summarizing and
                len(self.unsummarized_frames) >= self.summary_every_n
            )
            if should_summarize:
                self.summarizing = True
            return should_summarize

    def pop_unsummarized(self) -> list:
        """取出待总结帧列表（清空缓冲）"""
        with self._lock:
            frames = list(self.unsummarized_frames)
            self.unsummarized_frames = []
            return frames

    def finish_summary(self, new_summary: str):
        """总结完成，更新状态并写入文件"""
        with self._lock:
            self.prev_summary = new_summary
            self.summary_count += 1
            self.summarizing = False

        try:
            # 写入 summary.md（覆盖最新总结，保留历史版本追加）
            summary_content = (
                f"# {self.title or self.video_key}\n\n"
                f"> 来源: {self.source} | URL: {self.url}\n"
                f"> 最后更新: {time.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"已分析 {len(self.all_frames)} 帧 | 第 {self.summary_count} 次总结\n\n"
                f"---\n\n{new_summary}\n"
            )
            self.summary_file.write_text(summary_content, encoding='utf-8')
            _log(f"[{self.source}/{self.video_key}] 总结已写入 {self.summary_file}")
        except Exception as e:
            _log(f"写入 summary.md 失败: {e}")

    def abort_summary(self):
        """总结失败，解锁"""
        with self._lock:
            self.summarizing = False


# =====================================================================
# 向所有客户端广播消息
# =====================================================================
async def _broadcast(message: dict):
    if not _clients:
        return
    data = json.dumps(message, ensure_ascii=False)
    disconnected = set()
    for ws in list(_clients):
        try:
            await ws.send(data)
        except Exception:
            disconnected.add(ws)
    for ws in disconnected:
        _clients.discard(ws)


def broadcast_from_thread(message: dict):
    """从线程中安全地广播消息"""
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast(message), _loop)


# =====================================================================
# 模型加载（线程中执行）
# =====================================================================
def _ensure_analyzer():
    """懒加载 VL 分析器（线程安全）"""
    global _analyzer, _vl_loading

    need_load = False
    with _analyzer_lock:
        if _analyzer is None and not _vl_loading:
            _vl_loading = True
            need_load = True

    if need_load:
        vlm_desc, llm_desc = _get_model_desc()
        _log(f"⏳ 正在加载模型（首次懒加载）...")
        _log(f"   VLM: {vlm_desc}")
        _log(f"   LLM: {llm_desc}")
        broadcast_from_thread({'type': 'model_loading', 'model': 'vl', 'status': 'loading'})
        t0 = time.time()
        try:
            analyzer = VideoAnalyzer(_config)
        except Exception as e:
            with _analyzer_lock:
                _vl_loading = False
            _log(f"❌ 模型加载失败: {type(e).__name__}: {e}")
            raise
        elapsed = time.time() - t0
        with _analyzer_lock:
            _analyzer = analyzer
            _vl_loading = False
        _log(f"✅ 模型加载成功 (耗时 {elapsed:.1f}s)")
        _log(f"   VLM: {vlm_desc}  ← 就绪")
        _log(f"   LLM: {llm_desc}  ← 就绪")
        broadcast_from_thread({
            'type': 'model_loaded', 'model': 'vl',
            'elapsed_sec': round(elapsed, 1),
        })
    else:
        while _vl_loading:
            time.sleep(0.2)


# =====================================================================
# 获取或创建视频会话
# =====================================================================
def _get_or_create_session(url: str, title: str) -> VideoSession:
    source, video_key = _parse_video_key(url, title)
    key = f"{source}/{video_key}"
    with _sessions_lock:
        if key not in _sessions:
            summary_every_n = _config.get('summary', {}).get('every_n_frames', DEFAULT_SUMMARY_EVERY_N_FRAMES)
            _sessions[key] = VideoSession(
                video_key=video_key,
                source=source,
                url=url,
                title=title,
                summary_every_n=summary_every_n,
            )
        return _sessions[key]


# =====================================================================
# 消息处理路由
# =====================================================================
async def _handle_message(ws: ServerConnection, message: dict):
    msg_type = message.get('type', '')

    if msg_type == 'ping':
        await ws.send(json.dumps({'type': 'pong', 'timestamp': time.time()}))

    elif msg_type == 'get_status':
        resp = _build_status()
        await ws.send(json.dumps(resp, ensure_ascii=False))

    elif msg_type == 'preload_vl':
        # 异步预加载 VL 模型
        if _analyzer is not None:
            await ws.send(json.dumps({
                'type': 'preload_vl_done', 'vl_ready': True, 'elapsed_sec': 0,
            }))
            return
        def do_preload():
            t0 = time.time()
            try:
                _ensure_analyzer()
                elapsed = time.time() - t0
                broadcast_from_thread({
                    'type': 'preload_vl_done', 'vl_ready': True,
                    'elapsed_sec': round(elapsed, 1),
                })
            except Exception as e:
                elapsed = time.time() - t0
                _log(f"VL 预加载失败: {e}")
                broadcast_from_thread({
                    'type': 'preload_vl_done', 'vl_ready': False,
                    'error': str(e), 'elapsed_sec': round(elapsed, 1),
                })
        threading.Thread(target=do_preload, daemon=True, name='PreloadVL').start()
        await ws.send(json.dumps({'type': 'preload_vl_queued'}))

    elif msg_type == 'analyze_frame':
        data = message.get('data', {})
        frames = data.get('frames', [])
        url = data.get('video_url', '')
        title = data.get('title', '')
        if not frames:
            await ws.send(json.dumps({'type': 'frame_analysis_error', 'error': '没有收到帧数据'}))
            return

        # 立即确认收帧（不再直接返回帧分析结果，改为会话管理）
        await ws.send(json.dumps({'type': 'frame_received', 'frame_count': len(frames)}))

        # 获取或创建视频会话
        session = _get_or_create_session(url, title)

        def run_inference():
            try:
                _ensure_analyzer()
                # 推理当前帧
                results = _analyzer.analyze_base64_frames(frames)
                # 补充来自请求的元信息
                for i, r in enumerate(results):
                    if i < len(frames):
                        r.setdefault('time_sec', frames[i].get('timestamp', 0))
                        r.setdefault('time_str', _fmt_ts(r['time_sec']))

                _log(f"[{session.source}/{session.video_key}] 帧推理完成: {len(results)} 帧 "
                     f"| 总计: {len(session.all_frames) + len(results)} | "
                     f"待总结: {len(session.unsummarized_frames) + len(results)}")

                # 写入会话，判断是否触发总结
                should_summarize = session.add_frame_result(results)

                # 推送单帧原始结果给浏览器（实时显示）
                broadcast_from_thread({
                    'type': 'frame_analysis_result',
                    'results': results,
                    'video_key': session.video_key,
                    'source': session.source,
                })

                # 达到阈值，在新线程触发增量总结（不阻塞帧推理线程）
                if should_summarize:
                    threading.Thread(
                        target=_do_incremental_summary,
                        args=(session,),
                        daemon=True,
                        name=f'Summary-{session.video_key}',
                    ).start()

            except Exception as e:
                _log(f"帧分析失败: {e}")
                traceback.print_exc(file=sys.stderr)
                broadcast_from_thread({'type': 'frame_analysis_error', 'error': str(e)})

        threading.Thread(target=run_inference, daemon=True, name='FrameAnalyzer').start()

    elif msg_type == 'shutdown':
        _log("收到 shutdown 请求")
        await ws.send(json.dumps({'type': 'shutdown_ack'}))

    else:
        _log(f"未知消息类型: {msg_type}")


def _do_incremental_summary(session: VideoSession):
    """
    在后台线程执行增量总结，完成后广播结果。
    """
    frames_to_summarize = session.pop_unsummarized()
    if not frames_to_summarize:
        session.abort_summary()
        return

    _log(f"[{session.source}/{session.video_key}] 开始增量总结 "
         f"({len(frames_to_summarize)} 新帧, 第 {session.summary_count + 1} 次)...")

    # 通知浏览器：总结开始
    broadcast_from_thread({
        'type': 'summary_progress',
        'video_key': session.video_key,
        'source': session.source,
        'status': 'summarizing',
        'frame_count': len(frames_to_summarize),
        'total_frames': len(session.all_frames),
        'summary_index': session.summary_count + 1,
    })

    t0 = time.time()
    try:
        new_summary = _analyzer.incremental_summary(
            new_frames=frames_to_summarize,
            prev_summary=session.prev_summary or None,
            video_title=session.title,
            total_frames_seen=len(session.all_frames),
        )
        elapsed = round(time.time() - t0, 1)

        if new_summary:
            session.finish_summary(new_summary)
            _log(f"[{session.source}/{session.video_key}] 总结完成 ({elapsed}s)")

            # 广播总结结果给浏览器
            broadcast_from_thread({
                'type': 'incremental_summary',
                'video_key': session.video_key,
                'source': session.source,
                'summary': new_summary,
                'summary_index': session.summary_count,
                'total_frames': len(session.all_frames),
                'elapsed_sec': elapsed,
                'save_path': str(session.summary_file),
                # 时间范围（最早帧到最新帧）
                'time_range': {
                    'start': frames_to_summarize[0].get('time_str', '?'),
                    'end': frames_to_summarize[-1].get('time_str', '?'),
                },
            })
        else:
            session.abort_summary()
            _log(f"[{session.source}/{session.video_key}] 总结返回空，跳过")

    except Exception as e:
        session.abort_summary()
        _log(f"[{session.source}/{session.video_key}] 增量总结失败: {e}")
        traceback.print_exc(file=sys.stderr)
        broadcast_from_thread({
            'type': 'summary_error',
            'video_key': session.video_key,
            'source': session.source,
            'error': str(e),
        })


def _fmt_ts(seconds) -> str:
    """秒数转 MM:SS 或 HH:MM:SS"""
    try:
        s = float(seconds)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"
    except Exception:
        return '00:00'


def _build_status() -> dict:
    import socket
    return {
        'type': 'status',
        'pid': os.getpid(),
        'python': sys.executable,
        'hostname': socket.gethostname(),
        'vl_loaded': _analyzer is not None,
        'vl_loading': _vl_loading,
        'vl_model': _config.get('model', {}).get('name', '?'),
        'device': _config.get('model', {}).get('device', 'auto'),
        'uptime_sec': round(time.time() - _start_time, 1),
        'version': '4.0-ws-v2',
        'connected': True,
        'active_sessions': len(_sessions),
        'ana_root': str(ANA_ROOT),
    }


# =====================================================================
# WebSocket 连接处理
# =====================================================================
async def _on_connect(ws: ServerConnection):
    remote = ws.remote_address
    _log(f"客户端连接: {remote}")
    async with _clients_lock:
        _clients.add(ws)

    await ws.send(json.dumps(_build_status(), ensure_ascii=False))

    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
                await _handle_message(ws, message)
            except json.JSONDecodeError:
                _log(f"JSON 解析错误: {raw[:100]}")
            except Exception as e:
                _log(f"消息处理异常: {e}")
                traceback.print_exc(file=sys.stderr)
    except websockets.exceptions.ConnectionClosed as e:
        _log(f"客户端断开: {remote} ({e.code} {e.reason})")
    finally:
        async with _clients_lock:
            _clients.discard(ws)
        _log(f"客户端已移除: {remote}, 剩余连接: {len(_clients)}")


# =====================================================================
# 启动预热
# =====================================================================
def _start_warmup_if_needed():
    if not _config.get('preload_on_start', False):
        return

    def warmup():
        time.sleep(2.0)
        _log("🔥 开始后台预热 VL 模型...")
        try:
            _ensure_analyzer()
            _log("🔥 预热完成，VL 模型已就绪")
        except Exception as e:
            _log(f"🔥 预热失败: {e}")

    threading.Thread(target=warmup, daemon=True, name='WarmupVL').start()


# =====================================================================
# 主入口
# =====================================================================
def _get_model_desc() -> str:
    """读取 .env 中的后端配置，生成人类可读的模型描述字符串。"""
    vlm_backend = os.environ.get('VLM_BACKEND', 'huggingface')
    llm_backend = os.environ.get('LLM_BACKEND', 'ollama')

    if vlm_backend == 'huggingface':
        vlm_model = os.environ.get('VLM_HF_MODEL', 'Qwen/Qwen2-VL-2B-Instruct')
        vlm_desc = f"HuggingFace 本地 → {vlm_model}"
    elif vlm_backend == 'ollama':
        vlm_base = os.environ.get('VLM_OLLAMA_BASE_URL', 'http://localhost:11434')
        vlm_model = os.environ.get('VLM_OLLAMA_MODEL', 'qwen2-vl:7b')
        vlm_desc = f"Ollama ({vlm_base}) → {vlm_model}"
    elif vlm_backend == 'openai_compatible':
        vlm_base = os.environ.get('VLM_API_BASE_URL', 'https://api.openai.com/v1')
        vlm_model = os.environ.get('VLM_API_MODEL', 'gpt-4o')
        vlm_desc = f"OpenAI兼容 ({vlm_base}) → {vlm_model}"
    else:
        vlm_desc = f"未知后端: {vlm_backend}"

    if llm_backend == 'ollama':
        llm_base = os.environ.get('LLM_OLLAMA_BASE_URL', 'http://localhost:11434')
        llm_model = os.environ.get('LLM_OLLAMA_MODEL', 'qwen2.5:7b')
        llm_desc = f"Ollama ({llm_base}) → {llm_model}"
    elif llm_backend == 'openai_compatible':
        llm_base = os.environ.get('LLM_API_BASE_URL', 'https://api.openai.com/v1')
        llm_model = os.environ.get('LLM_API_MODEL', 'gpt-4o-mini')
        llm_desc = f"OpenAI兼容 ({llm_base}) → {llm_model}"
    else:
        llm_desc = f"未知后端: {llm_backend}"

    return vlm_desc, llm_desc


def run_server(config: dict, host: str = '127.0.0.1', port: int = 19527):
    global _config, _loop

    _config = config
    summary_n = config.get('summary', {}).get('every_n_frames', DEFAULT_SUMMARY_EVERY_N_FRAMES)

    vlm_desc, llm_desc = _get_model_desc()
    preload = config.get('preload_on_start', False)

    _log("="*55)
    _log(f"  AI 视频分析服务  —  ws://{host}:{port}")
    _log("-"*55)
    _log(f"  PID          : {os.getpid()}")
    _log(f"  VLM (帧理解) : {vlm_desc}")
    _log(f"  LLM (文本总结): {llm_desc}")
    _log(f"  增量总结     : 每 {summary_n} 帧触发")
    _log(f"  本地存储     : {ANA_ROOT}")
    _log(f"  启动预热     : {'是' if preload else '否（首次请求时懒加载）'}")
    _log("="*55)

    async def main():
        global _loop
        _loop = asyncio.get_event_loop()
        _start_warmup_if_needed()
        async with websockets.serve(_on_connect, host, port,
                                    ping_interval=20, ping_timeout=60,
                                    max_size=50 * 1024 * 1024):
            _log(f"✅ WebSocket 服务已就绪，等待连接... ws://{host}:{port}")
            await asyncio.Future()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("服务已停止")


if __name__ == '__main__':
    import json as _json
    config_path = Path(__file__).parent / 'config.json'
    with open(config_path) as f:
        cfg = _json.load(f)
    run_server(cfg)

