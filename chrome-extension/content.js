/**
 * Content Script — v4.0 (纯 VL 实时帧分析)
 *
 * 功能：
 * 1. 检测页面中的 video 元素
 * 2. 在视频播放器上叠加 AI 分析按钮
 * 3. 点击后实时截帧 → VL 推理 → 结果回显到右侧面板
 *
 * 使用流程:
 *   1. 终端启动: cd local-service && python3 websocket_server.py
 *   2. 浏览器 popup 点击"连接服务"
 *   3. 视频页面点击"👁️ AI 分析"
 *   4. 播放视频，每 3s 自动截帧 → VL 推理 → 右侧展示结果
 */
(function() {
  'use strict';

  // ========== 状态 ==========
  let isAnalyzing = false;
  let captureInterval = null;
  let nativeConnected = true;
  let isSendingFrame = false;   // 帧发送锁（上一帧未处理完时跳过）
  let contextInvalidated = false;
  let currentVideo = null;      // 当前正在分析的 video 元素（preload_vl_done 回调时需要）
  const CAPTURE_INTERVAL_MS = 3000;
  const FRAME_QUALITY = 0.7;

  // ========== 安全消息封装 ==========
  // retryCount: 内部重试次数（Service Worker 重启后自动重试一次）
  function safeSendMessage(msg, callback, retryCount) {
    if (contextInvalidated) {
      if (callback) callback(null);
      return;
    }
    retryCount = retryCount || 0;
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) {
          const err = chrome.runtime.lastError.message || '';
          // SW 可能刚被浏览器唤醒/重启，此时上下文暂时失效，等 600ms 后重试一次
          if ((err.includes('Could not establish connection') || err.includes('Receiving end does not exist')) && retryCount < 2) {
            console.log(`[AI Video] SW 可能刚重启，${600}ms 后重试 (${retryCount + 1}/2)...`);
            setTimeout(() => safeSendMessage(msg, callback, retryCount + 1), 600);
            return;
          }
          if (err.includes('invalidated') || err.includes('destroyed')) {
            contextInvalidated = true;
            appendError('扩展上下文已失效，请刷新页面 (F5) 后重试');
            stopAnalysis();
            if (callback) callback(null);
            return;
          }
          // 其他错误，原样回调
          console.warn('[AI Video] sendMessage error:', err);
          if (callback) callback(null);
          return;
        }
        if (callback) callback(resp);
      });
    } catch (e) {
      const errMsg = e.message || '';
      if (errMsg.includes('invalidated') || errMsg.includes('destroyed')) {
        contextInvalidated = true;
        appendError('扩展上下文已失效，请刷新页面 (F5) 后重试');
        stopAnalysis();
      }
      if (callback) callback(null);
    }
  }

  // ========== 初始化 ==========
  function init() {
    console.log('[AI Video] Content Script v4.0 初始化 (VL Mode)');
    addAnalysisButton();

    const observer = new MutationObserver(() => {
      if (!document.getElementById('ai-analyze-btn')) addAnalysisButton();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
      console.log(`[AI Video] 📩 收到消息: type="${request.type}"`);

      switch (request.type) {

        case 'preload_vl_done':
          // VL 模型异步加载完成（在后台线程），现在可以开始截帧
          if (!isAnalyzing) {
            // 用户已在等待期间停止了分析
            sendResponse({ success: true });
            break;
          }
          if (request.vl_ready) {
            console.log(`[AI Video] ✅ VL 模型已就绪 (${request.elapsed_sec}s)，开始截帧`);
            vlModelReady = true;
            updateStatus('⏳ 分析中...', true);
            showProgress(1, '截取首帧...');
            const videoEl = currentVideo || findVideoElement();
            if (videoEl) {
              // 视觉帧轨
              captureAndSendFrame(videoEl);
              captureInterval = setInterval(() => captureAndSendFrame(videoEl), CAPTURE_INTERVAL_MS);
              // 音频轨（独立循环，与截帧解耦）
              startAudioTrack(videoEl);
              console.log('[AI Video] 实时帧分析已启动 (间隔=' + CAPTURE_INTERVAL_MS + 'ms)，音频轨独立运行');
            } else {
              appendError('找不到视频元素，请刷新页面重试');
              stopAnalysis();
            }
          } else {
            appendError('VL 模型加载失败: ' + (request.error || '未知错误'));
            stopAnalysis();
          }
          sendResponse({ success: true });
          break;

        case 'native_disconnected':
          nativeConnected = false;
          updateStatus('🔴 已断开', true);
          if (isSendingFrame) {
            isSendingFrame = false;
          }
          sendResponse({ success: true });
          break;

        case 'frame_received':
          nativeConnected = true;
          updateStatus('⚡ VL推理中...', true);
          sendResponse({ success: true });
          break;

        case 'toggle_analysis':
          toggleAnalysis();
          sendResponse({ success: true });
          break;

        case 'get_video_info':
          sendResponse(getVideoInfo());
          break;

        case 'frame_analysis_result':
          nativeConnected = true;
          // 帧原始结果追加到折叠区（主要展示区留给增量总结）
          appendRawFrames(request.results);
          updateStatus('⏳ 分析中...', true);
          isSendingFrame = false;
          lastFrameData = null;
          reconnectRetryCount = 0;
          sendResponse({ success: true });
          break;

        case 'frame_analysis_error':
          nativeConnected = true;
          appendError(request.error);
          isSendingFrame = false;
          lastFrameData = null;
          reconnectRetryCount = 0;
          sendResponse({ success: true });
          break;

        // ---- 增量总结（核心新功能）----
        case 'incremental_summary':
          nativeConnected = true;
          appendIncrementalSummary(request);
          updateStatus('⏳ 分析中...', true);
          sendResponse({ success: true });
          break;

        case 'summary_progress':
          if (request.status === 'summarizing') {
            updateStatus('📝 生成总结...', true);
            showProgress(
              Math.min((request.summary_index || 1) * 20, 90),
              `正在生成第 ${request.summary_index} 次总结（已分析 ${request.total_frames} 帧）...`
            );
          }
          sendResponse({ success: true });
          break;

        case 'summary_error':
          appendError('总结生成失败: ' + (request.error || '未知错误'));
          sendResponse({ success: true });
          break;

        case 'notes_saved':
          if (request.success) {
            alert('✅ 笔记保存成功！\n' + (request.filepath || ''));
          } else {
            alert('❌ 保存失败：' + (request.error || '未知错误'));
          }
          sendResponse({ success: true });
          break;

        case 'analysis_error':
        case 'error':
          appendError(request.error);
          sendResponse({ success: true });
          break;

        default:
          sendResponse({ success: false });
      }
      return true;
    });

    console.log('[AI Video] ✅ Content Script 初始化完成');
  }

  // ========== 视频检测 ==========
  function findVideoElement() {
    const videos = document.querySelectorAll('video');
    if (videos.length === 0) return null;
    if (videos.length === 1) return videos[0];
    let largest = videos[0], maxArea = 0;
    for (const v of videos) {
      const area = v.videoWidth * v.videoHeight;
      if (area > maxArea) { maxArea = area; largest = v; }
    }
    return largest;
  }

  function findVideoContainer(video) {
    if (!video) return null;
    let el = video.parentElement;
    for (let i = 0; i < 5 && el; i++) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 300 && rect.height > 200) return el;
      el = el.parentElement;
    }
    return video.parentElement || document.body;
  }

  // ========== UI: 分析按钮 ==========
  function addAnalysisButton() {
    const video = findVideoElement();
    if (!video || document.getElementById('ai-analyze-btn')) return;

    const container = findVideoContainer(video);
    if (!container) return;

    if (window.getComputedStyle(container).position === 'static') {
      container.style.position = 'relative';
    }

    const btn = document.createElement('button');
    btn.id = 'ai-analyze-btn';
    btn.textContent = '👁️ AI 分析';
    btn.style.cssText = `
      position: absolute; top: 10px; right: 10px; z-index: 2147483647;
      padding: 8px 16px; background: rgba(102, 126, 234, 0.9); color: white;
      border: none; border-radius: 20px; cursor: pointer; font-size: 14px;
      font-weight: bold; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
      transition: all 0.2s ease; backdrop-filter: blur(4px);
    `;
    btn.addEventListener('mouseenter', () => {
      btn.style.transform = 'scale(1.05)';
      btn.style.boxShadow = '0 4px 12px rgba(0,0,0,0.4)';
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = 'scale(1)';
      btn.style.boxShadow = '0 2px 6px rgba(0,0,0,0.3)';
    });
    btn.addEventListener('click', toggleAnalysis);

    container.appendChild(btn);
    console.log('[AI Video] 分析按钮已添加');
  }

  // ========== UI: 结果面板 ==========
  function createResultPanel() {
    removeResultPanel();

    const panel = document.createElement('div');
    panel.id = 'ai-analysis-panel';
    panel.style.cssText = `
      position: fixed; right: 0; top: 0; width: 440px; max-width: 92vw;
      height: 100vh; background: rgba(255,255,255,0.97);
      box-shadow: -4px 0 20px rgba(0,0,0,0.15); z-index: 2147483647;
      overflow-y: auto; display: flex; flex-direction: column;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    `;

    panel.innerHTML = `
      <!-- 顶栏 -->
      <div style="padding: 14px 16px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;">
        <h3 style="margin: 0; font-size: 15px;">👁️ AI 实时分析</h3>
        <div style="display: flex; gap: 8px; align-items: center;">
          <span id="ai-status" style="font-size: 11px; opacity: 0.9;">● 就绪</span>
          <button id="ai-close-panel" style="background: rgba(255,255,255,0.2); border: none; color: white; cursor: pointer; font-size: 17px; padding: 2px 8px; border-radius: 50%;">✕</button>
        </div>
      </div>

      <!-- 进度条 -->
      <div id="progress-section" style="display:none; padding: 10px 16px; background: #f5f7fa; flex-shrink: 0;">
        <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
          <span id="progress-stage" style="font-size:12px; color:#555;">准备中...</span>
          <span id="progress-pct" style="font-size:12px; font-weight:600; color:#667eea;">0%</span>
        </div>
        <div style="height:5px; background:#e0e0e0; border-radius:3px; overflow:hidden;">
          <div id="progress-bar" style="height:100%; width:0%; background:linear-gradient(90deg,#667eea,#764ba2); transition:width 0.4s;"></div>
        </div>
      </div>

      <!-- 📝 增量总结区（主展示区）-->
      <div id="summary-section" style="flex: 1; overflow-y: auto; display: flex; flex-direction: column;">

        <!-- 占位提示（总结到来前显示）-->
        <div id="summary-placeholder" style="padding: 20px 16px; color: #999; font-size: 13px; text-align: center; line-height: 1.8;">
          <div style="font-size: 28px; margin-bottom: 8px;">✨</div>
          <div>正在实时分析视频帧...</div>
          <div style="font-size: 12px; margin-top: 6px;">每 <strong>5 帧</strong>自动生成一次滚动总结</div>
          <div style="font-size: 12px; color: #bbb;">原始帧结果将在下方折叠展示</div>
        </div>

        <!-- 总结卡片容器 -->
        <div id="summary-content" style="padding: 12px 16px; display: none; flex-direction: column; gap: 12px;"></div>

        <!-- 原始帧折叠区 -->
        <div id="raw-frames-section" style="margin: 0 16px 12px; border: 1px solid #e8eaf0; border-radius: 8px; overflow: hidden; display: none;">
          <button id="raw-frames-toggle" style="
            width: 100%; padding: 8px 14px; background: #f5f7fa; border: none;
            cursor: pointer; text-align: left; font-size: 12px; color: #667eea;
            font-weight: 600; display: flex; justify-content: space-between; align-items: center;
          ">
            <span>📋 原始帧记录 (<span id="raw-frames-count">0</span> 帧)</span>
            <span id="raw-frames-arrow" style="font-size: 10px;">▶ 展开</span>
          </button>
          <div id="raw-frames-list" style="display:none; padding: 8px; max-height: 300px; overflow-y: auto;"></div>
        </div>
      </div>

      <!-- 底部操作栏 -->
      <div style="padding: 12px 16px; border-top: 1px solid #eee; flex-shrink: 0;">
        <textarea id="note-input" placeholder="📝 输入笔记内容（可选）..." style="width: 100%; min-height: 52px; padding: 8px 10px; border: 1px solid #ddd; border-radius: 8px; resize: vertical; font-size: 13px; box-sizing: border-box; color: #333;"></textarea>
        <div style="display: flex; gap: 8px; margin-top: 8px;">
          <button id="save-note" style="flex: 1; padding: 9px; background: #2196F3; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px;">💾 保存笔记</button>
          <button id="stop-analysis" style="padding: 9px 18px; background: #f44336; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px;">⏹ 停止</button>
        </div>
      </div>

      <style>
        #ai-analysis-panel .summary-card {
          background: #f8f9ff; border: 1px solid #dde2f8;
          border-radius: 10px; padding: 14px; animation: slideIn 0.3s ease;
        }
        #ai-analysis-panel .summary-card .summary-header {
          display: flex; justify-content: space-between; align-items: center;
          margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #e8eaf0;
        }
        #ai-analysis-panel .summary-card .summary-badge {
          font-size: 11px; background: #667eea; color: white;
          padding: 2px 8px; border-radius: 10px; font-weight: 600;
        }
        #ai-analysis-panel .summary-card .summary-meta {
          font-size: 11px; color: #999;
        }
        #ai-analysis-panel .summary-card .summary-body {
          font-size: 13px; line-height: 1.7; color: #333;
        }
        #ai-analysis-panel .summary-card .summary-body h1,
        #ai-analysis-panel .summary-card .summary-body h2,
        #ai-analysis-panel .summary-card .summary-body h3 {
          margin: 8px 0 4px; font-size: 13px; color: #444;
        }
        #ai-analysis-panel .summary-card .summary-body li { margin: 2px 0; }
        #ai-analysis-panel .summary-card .summary-save-hint {
          font-size: 11px; color: #aaa; margin-top: 8px; padding-top: 6px;
          border-top: 1px solid #eee;
        }
        #ai-analysis-panel .raw-frame-item {
          padding: 6px 8px; border-bottom: 1px solid #f0f0f0;
          font-size: 11px; color: #666; line-height: 1.5;
        }
        #ai-analysis-panel .raw-frame-item:last-child { border-bottom: none; }
        #ai-analysis-panel .raw-frame-ts {
          font-weight: 600; color: #667eea; margin-right: 6px;
        }
        #ai-analysis-panel .error-card {
          margin-bottom: 10px; padding: 10px 12px; background: #fff3e0;
          border-left: 3px solid #ff9800; border-radius: 0 8px 8px 0;
          font-size: 12px; color: #e65100;
        }
        @keyframes slideIn {
          from { opacity: 0; transform: translateY(6px); }
          to { opacity: 1; transform: translateY(0); }
        }
      </style>
    `;

    document.body.appendChild(panel);

    document.getElementById('ai-close-panel').addEventListener('click', () => {
      stopAnalysis(); removeResultPanel();
    });
    document.getElementById('save-note').addEventListener('click', saveToObsidian);
    document.getElementById('stop-analysis').addEventListener('click', stopAnalysis);

    // 原始帧折叠展开
    document.getElementById('raw-frames-toggle').addEventListener('click', () => {
      const list = document.getElementById('raw-frames-list');
      const arrow = document.getElementById('raw-frames-arrow');
      const isHidden = list.style.display === 'none';
      list.style.display = isHidden ? 'block' : 'none';
      arrow.textContent = isHidden ? '▼ 收起' : '▶ 展开';
    });
  }

  function removeResultPanel() {
    const panel = document.getElementById('ai-analysis-panel');
    if (panel) panel.remove();
  }

  // ========== 核心逻辑：切换分析状态 ==========
  function toggleAnalysis() {
    if (isAnalyzing) {
      stopAnalysis();
    } else {
      startVisualAnalysis();
    }
  }

  function startVisualAnalysis() {
    const video = findVideoElement();
    if (!video) {
      alert('未找到视频元素！请确保页面上有视频在播放。');
      return;
    }

    currentVideo = video;  // 保存引用，供 preload_vl_done 回调使用
    isAnalyzing = true;
    contextInvalidated = false;
    const btn = document.getElementById('ai-analyze-btn');
    if (btn) {
      btn.textContent = '⏹ 停止分析';
      btn.style.background = 'rgba(244, 67, 54, 0.9)';
    }

    createResultPanel();
    clearContent();

    console.log(`[AI Video] 👁️ 启动实时帧分析 | 视频: ${video.videoWidth}x${video.videoHeight}, 时长=${video.duration.toFixed(1)}s`);

    nativeConnected = true;
    isSendingFrame = false;
    vlModelReady = false;
    updateStatus('⏳ 连接中...', true);
    showProgress(0, '等待 WebSocket 服务...');

    // 先尝试连接（connect_native 内部会判断是否已连接，干幂等安全）
    // Service Worker 随时可能被浏览器休眠/重启，导致 isConnected 重置，
    // 因此每次启动分析前都主动尝试连接，确保 WebSocket 处于 OPEN 状态
    updateStatus('🔗 连接服务中...', true);
    safeSendMessage({ type: 'connect_native' }, (resp) => {
      if (!resp?.connected) {
        appendError(
          '无法连接 WebSocket 服务，请先在终端运行：<br>' +
          '<code>cd local-service && python3 websocket_server.py</code><br>' +
          '然后在扩展 Popup 中点击「连接服务」'
        );
        stopAnalysis();
        return;
      }
      _doPreloadVL();
    });

    function _doPreloadVL() {
      console.log('[AI Video] ✅ 服务已连接，开始预加载 VL 模型');
      updateStatus('⏳ 预加载 VL 模型...', true);
      showProgress(0, '正在预加载 VL 模型（首次较慢）...');

      // preload_vl 是异步操作：background 只同步返回 {sent:true}，
      // 真正结果通过 onMessage 的 preload_vl_done 推送过来
      safeSendMessage({ type: 'preload_vl' }, (resp) => {
        if (!resp || !resp.sent) {
          appendError('预加载请求发送失败，WebSocket 连接可能已断开，请刷新页面重试');
          stopAnalysis();
          return;
        }
        console.log('[AI Video] ⏳ VL 预加载请求已发送，等待模型加载完成...');
        updateStatus('⏳ 模型加载中...', true);
        showProgress(10, '等待 Qwen2-VL 模型加载（首次约 10~30s）...');
      });
    }
  }

  function stopAnalysis() {
    isAnalyzing = false;
    if (captureInterval) {
      clearInterval(captureInterval);
      captureInterval = null;
    }
    isSendingFrame = false;
    stopAudioTrack();
    const btn = document.getElementById('ai-analyze-btn');
    if (btn) {
      btn.textContent = '👁️ AI 分析';
      btn.style.background = 'rgba(102, 126, 234, 0.9)';
    }
    updateStatus('● 已停止', false);
    hideProgress();
    console.log('[AI Video] 分析已停止');
  }

  // ========== 帧捕获与发送 ==========
  let frameSeq = 0;
  let lastFrameData = null;
  let reconnectRetryCount = 0;
  let vlModelReady = false;

  // =====================================================================
  // 音频轨 — 独立持续录制，与截帧完全解耦
  // 每 AUDIO_SEGMENT_MS 毫秒录一段，发送 audio_segment 消息到 Python
  // =====================================================================
  const AUDIO_SEGMENT_MS = 10000;   // 每段 10 秒（可调）

  let _audioStream = null;          // MediaStream（来自 video.captureStream）
  let _audioTimer = null;           // 音频轨定时器句柄
  let _audioSegSeq = 0;             // 音频段序号
  let _audioMimeType = null;        // 选定的 MIME 类型

  /** 选一次 MIME 类型（懒初始化） */
  function _getAudioMime() {
    if (_audioMimeType) return _audioMimeType;
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg'];
    _audioMimeType = candidates.find(m => MediaRecorder.isTypeSupported(m)) || 'audio/webm';
    return _audioMimeType;
  }

  /** 初始化音频流（懒加载，只取音轨） */
  function _ensureAudioStream(video) {
    if (_audioStream) return _audioStream;
    try {
      if (!video.captureStream) {
        console.log('[AI Video] 🔇 浏览器不支持 captureStream，跳过音频轨');
        return null;
      }
      const stream = video.captureStream();
      const audioTracks = stream.getAudioTracks();
      if (!audioTracks.length) {
        console.log('[AI Video] 🔇 视频无音轨');
        return null;
      }
      _audioStream = new MediaStream(audioTracks);
      console.log('[AI Video] 🎙️ 音频流就绪，轨道:', audioTracks[0].label);
      return _audioStream;
    } catch (e) {
      console.warn('[AI Video] 音频流初始化失败:', e.message);
      return null;
    }
  }

  /**
   * 启动独立音频轨循环录制。
   * 每完成一段立即发送 audio_segment 消息，同时自动开始下一段。
   */
  function startAudioTrack(video) {
    if (_audioTimer) return;   // 已在运行

    const stream = _ensureAudioStream(video);
    if (!stream) return;       // 无音轨，静默退出

    const mimeType = _getAudioMime();
    console.log(`[AI Video] 🎙️ 音频轨启动，每段 ${AUDIO_SEGMENT_MS / 1000}s，格式: ${mimeType}`);

    function _recordOneSegment() {
      if (!isAnalyzing) return;  // 已停止分析，终止循环

      const segStartTime = video.currentTime;
      const segStartWall = Date.now();
      const chunks = [];

      let recorder;
      try {
        recorder = new MediaRecorder(stream, { mimeType });
      } catch (e) {
        console.warn('[AI Video] MediaRecorder 创建失败:', e.message);
        return;
      }

      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
      };

      recorder.onstop = () => {
        const segEndTime = video.currentTime;
        // 录完后立即开始下一段，不等待发送
        if (isAnalyzing) _audioTimer = setTimeout(_recordOneSegment, 0);

        if (!chunks.length) return;
        const blob = new Blob(chunks, { type: mimeType });
        const reader = new FileReader();
        reader.onloadend = () => {
          const b64 = reader.result.split(',')[1];
          if (!b64) return;
          _audioSegSeq++;
          const segData = {
            seq: _audioSegSeq,
            audio_b64: b64,
            start_sec: segStartTime,
            end_sec: segEndTime,
            video_url: window.location.href,
            title: document.title,
            duration: video.duration || 0,
          };
          console.log(`[AI Video] 🎙️ 音频段 #${_audioSegSeq} [${segStartTime.toFixed(1)}s~${segEndTime.toFixed(1)}s] `
            + `(${(b64.length * 0.75 / 1024).toFixed(0)} KB)`);
          safeSendMessage({ type: 'audio_segment', data: segData }, null);
        };
        reader.readAsDataURL(blob);
      };

      recorder.onerror = (e) => {
        console.warn('[AI Video] 录音错误:', e.error?.message);
        if (isAnalyzing) _audioTimer = setTimeout(_recordOneSegment, 1000);
      };

      recorder.start();
      // AUDIO_SEGMENT_MS 后停止，触发 onstop 并自动开始下一段
      _audioTimer = setTimeout(() => {
        if (recorder.state === 'recording') recorder.stop();
      }, AUDIO_SEGMENT_MS);
    }

    _recordOneSegment();
  }

  /** 停止音频轨 */
  function stopAudioTrack() {
    if (_audioTimer) {
      clearTimeout(_audioTimer);
      _audioTimer = null;
    }
    if (_audioStream) {
      _audioStream.getTracks().forEach(t => t.stop());
      _audioStream = null;
    }
    _audioMimeType = null;
    console.log('[AI Video] 🔇 音频轨已停止');
  }

  // =====================================================================
  // 纯视觉帧捕获（不再附带音频）
  // =====================================================================
  function captureAndSendFrame(video) {
    if (isSendingFrame) {
      console.log('[AI Video] ⏭️ 上一帧处理中，跳过本次截帧');
      return;
    }

    if (!nativeConnected) {
      if (lastFrameData && reconnectRetryCount < 3) {
        reconnectRetryCount++;
        console.log(`[AI Video] 🔄 断线重发帧 #${lastFrameData.seq} (第${reconnectRetryCount}次)`);
        safeSendMessage({ type: 'connect_native' }, (resp) => {
          if (resp?.connected) {
            nativeConnected = true;
            isSendingFrame = true;
            safeSendMessage({ type: 'analyze_frame', data: lastFrameData }, (response) => {
              if (!response) { isSendingFrame = false; }
            });
            reconnectRetryCount = 0;
          } else {
            isSendingFrame = false;
          }
        });
      } else {
        console.log('[AI Video] ⚠️ Native 未连接，跳过截帧');
      }
      return;
    }

    try {
      const currentTime = video.currentTime;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');

      const maxW = 640, maxH = 480;
      let w = video.videoWidth || 640;
      let h = video.videoHeight || 360;
      if (w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
      if (h > maxH) { w = Math.round(w * maxH / h); h = maxH; }

      canvas.width = w; canvas.height = h;
      ctx.drawImage(video, 0, 0, w, h);
      const base64 = canvas.toDataURL('image/jpeg', FRAME_QUALITY).split(',')[1];

      isSendingFrame = true;
      frameSeq++;

      // 安全超时：最多等 25 秒，防止后端推理慢时锁永久卡住
      const _lockSeq = frameSeq;
      setTimeout(() => {
        if (isSendingFrame) {
          console.warn(`[AI Video] ⚠️ 帧 #${_lockSeq} 超时解锁（25s 未收到响应）`);
          isSendingFrame = false;
        }
      }, 25000);

      const frameData = {
        frames: [{ base64, timestamp: currentTime, width: w, height: h }],
        video_url: window.location.href,
        title: document.title,
        frame_seq: frameSeq,
        seq: frameSeq,
        duration: video.duration || 0,
      };
      lastFrameData = frameData;
      reconnectRetryCount = 0;

      const progressPct = Math.min(frameSeq * 5, 95);
      showProgress(progressPct, `帧 #${frameSeq} 发送中...`);
      updateStatus(`📸 帧 #${frameSeq}`, true);
      console.log(`[AI Video] 📸 发送帧 #${frameSeq} (t=${currentTime.toFixed(1)}s)`);

      safeSendMessage({
        type: 'analyze_frame',
        data: frameData,
      }, (response) => {
        if (!response) { isSendingFrame = false; return; }
      });

    } catch (e) {
      console.error('[AI Video] ❌ 帧截取失败:', e.message || e);
      isSendingFrame = false;
    }
  }

  // ========== 结果展示 ==========

  /** 将原始帧分析结果追加到折叠区 */
  function appendRawFrames(results) {
    if (!results || results.length === 0) return;
    const section = document.getElementById('raw-frames-section');
    const list = document.getElementById('raw-frames-list');
    const countEl = document.getElementById('raw-frames-count');
    if (!section || !list) return;

    // 首次有数据时显示折叠区
    section.style.display = 'block';

    for (const r of results) {
      const timeLabel = r.time_str || formatTime(r.timestamp || r.time_sec || 0);
      const desc = r.description || r.content || '(无描述)';
      const div = document.createElement('div');
      div.className = 'raw-frame-item';
      div.innerHTML = `<span class="raw-frame-ts">⏱ ${escapeHtml(timeLabel)}</span>${escapeHtml(desc)}`;
      list.appendChild(div);
    }

    // 更新帧计数
    const current = parseInt(countEl.textContent || '0');
    countEl.textContent = current + results.length;
  }

  /** 展示增量总结卡片（替换上一张，保留历史）*/
  function appendIncrementalSummary(msg) {
    const placeholder = document.getElementById('summary-placeholder');
    const summaryContent = document.getElementById('summary-content');
    if (!summaryContent) return;

    // 隐藏占位符
    if (placeholder) placeholder.style.display = 'none';
    summaryContent.style.display = 'flex';

    const idx = msg.summary_index || '?';
    const timeRange = msg.time_range ? `${msg.time_range.start} → ${msg.time_range.end}` : '';
    const totalFrames = msg.total_frames || '?';
    const elapsed = msg.elapsed_sec ? `${msg.elapsed_sec}s` : '';

    // 创建新总结卡片（覆盖旧卡片：清空后插入最新版本）
    // 策略：保留最近 1 张卡片（最新总结），减少视觉噪声
    summaryContent.innerHTML = '';

    const card = document.createElement('div');
    card.className = 'summary-card';
    card.innerHTML = `
      <div class="summary-header">
        <span class="summary-badge">📝 第 ${escapeHtml(String(idx))} 次总结</span>
        <span class="summary-meta">${timeRange ? escapeHtml(timeRange) + ' · ' : ''}${totalFrames} 帧${elapsed ? ' · ' + elapsed : ''}</span>
      </div>
      <div class="summary-body">${formatMarkdown(msg.summary || '')}</div>
      ${msg.save_path ? `<div class="summary-save-hint">💾 已保存至: ${escapeHtml(msg.save_path)}</div>` : ''}
    `;
    summaryContent.appendChild(card);
    summaryContent.scrollTop = 0;

    // 隐藏进度条（总结完成）
    hideProgress();
  }

  function appendError(errorMsg) {
    // 错误优先插入 summary-content 区域，其次 analysis-content（兼容）
    const target = document.getElementById('summary-content') || document.getElementById('analysis-content');
    if (!target) return;

    // 显示 summary-content 容器
    const placeholder = document.getElementById('summary-placeholder');
    if (placeholder) placeholder.style.display = 'none';
    if (target.id === 'summary-content') target.style.display = 'flex';

    const div = document.createElement('div');
    div.className = 'error-card';
    div.innerHTML = `⚠️ ${String(errorMsg)}`;
    target.appendChild(div);
    target.scrollTop = target.scrollHeight;
  }

  // ========== UI 工具函数 ==========
  function updateStatus(text, isActive) {
    const el = document.getElementById('ai-status');
    if (el) {
      el.textContent = text;
      el.style.color = isActive ? '#ffd54f' : '#white';
    }
  }

  function showProgress(percent, detail) {
    const section = document.getElementById('progress-section');
    const bar = document.getElementById('progress-bar');
    const pctEl = document.getElementById('progress-pct');
    const stageEl = document.getElementById('progress-stage');
    if (section) section.style.display = 'block';
    if (bar) bar.style.width = percent + '%';
    if (pctEl) pctEl.textContent = percent + '%';
    if (stageEl) stageEl.textContent = detail || '';
  }

  function hideProgress() {
    const section = document.getElementById('progress-section');
    if (section) section.style.display = 'none';
  }

  function clearContent() {
    // 重置新面板结构
    const summaryContent = document.getElementById('summary-content');
    if (summaryContent) { summaryContent.innerHTML = ''; summaryContent.style.display = 'none'; }
    const placeholder = document.getElementById('summary-placeholder');
    if (placeholder) placeholder.style.display = 'block';
    const rawList = document.getElementById('raw-frames-list');
    if (rawList) rawList.innerHTML = '';
    const rawSection = document.getElementById('raw-frames-section');
    if (rawSection) rawSection.style.display = 'none';
    const countEl = document.getElementById('raw-frames-count');
    if (countEl) countEl.textContent = '0';
    // 兼容旧结构
    const content = document.getElementById('analysis-content');
    if (content) content.innerHTML = '';
  }

  // ========== 保存笔记 ==========
  function saveToObsidian() {
    const noteInput = document.getElementById('note-input');
    const note = noteInput ? noteInput.value.trim() : '';

    const resultDivs = document.querySelectorAll('#analysis-content > div');
    const analysisResults = [];
    resultDivs.forEach(div => analysisResults.push({ content: div.innerText.trim() }));

    // save_notes 是异步操作：background 只返回 { sent: true/false }
    // 真正结果通过 notes_saved 消息异步推送，由 onMessage 的 notes_saved 分支处理
    safeSendMessage({
      type: 'save_notes',
      data: { note, url: window.location.href, title: document.title, timestamp: new Date().toISOString(), analysis_results: analysisResults },
    }, (response) => {
      if (!response || !response.sent) {
        alert('❌ 保存请求发送失败，WebSocket 连接可能已断开');
        return;
      }
      // 请求已发出，等待 notes_saved 推送
      if (noteInput) noteInput.value = '';
    });
  }

  // ========== 工具函数 ==========
  function formatTime(seconds) {
    if (!seconds || isNaN(seconds)) return '00:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h.toString().padStart(2,'0')}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
    return `${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatMarkdown(md) {
    let html = md
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
      .replace(/---/gm, '<hr>')
      .replace(/\n\n/g, '</p><p>')
      .replace(/\n/g, '<br>');
    return `<p>${html}</p>`;
  }

  function getVideoInfo() {
    const video = findVideoElement();
    return {
      url: window.location.href,
      title: document.title,
      hasVideo: !!video,
      currentTime: video ? video.currentTime : 0,
      duration: video ? video.duration : 0,
    };
  }

  // ========== 启动 ==========
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    setTimeout(init, 1500);
  }

})();
