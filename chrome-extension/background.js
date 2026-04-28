/**
 * Background Service Worker — v5.0 (WebSocket 模式)
 *
 * 架构改变：
 *   之前: Chrome Native Messaging（每次连接重启 Python 进程，协议脆弱）
 *   现在: WebSocket ws://127.0.0.1:19527（常驻服务，稳定可靠）
 *
 * 使用方式：
 *   1. 终端启动: cd local-service && python3 websocket_server.py
 *   2. popup 点击"连接服务" → 建立 WebSocket 连接
 *   3. 视频页面点击"👁️ AI 分析" → 开始实时截帧分析
 */

const WS_URL = 'ws://127.0.0.1:19527';
const WS_RECONNECT_DELAY = 2000;   // 断线重连间隔 ms
const WS_PING_INTERVAL   = 15000;  // 心跳间隔 ms

let ws = null;
let isConnected = false;
let connectionTime = 0;
let nativeStatusCache = null;
let reconnectTimer = null;
let pingTimer = null;
let autoReconnect = false;   // 用户主动连接后才自动重连

// ========== WebSocket 连接管理 ==========

function connectWS() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    console.log('[BG] WS already connected/connecting');
    return;
  }

  console.log(`[BG] 连接 WebSocket: ${WS_URL}`);
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    console.error('[BG] WebSocket 创建失败:', e.message);
    isConnected = false;
    return;
  }

  ws.onopen = () => {
    console.log('[BG] ✅ WebSocket 已连接');
    isConnected = true;
    connectionTime = Date.now();
    autoReconnect = true;
    clearTimeout(reconnectTimer);
    // 启动心跳
    pingTimer = setInterval(() => {
      if (isConnected && ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, WS_PING_INTERVAL);
    // 请求最新状态
    sendToWS({ type: 'get_status' });
  };

  ws.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      console.log(`[BG] <- WS: type=${message.type}`, JSON.stringify(message).substring(0, 200));
      handleMessageFromServer(message);
    } catch (e) {
      console.error('[BG] WS 消息解析失败:', e.message);
    }
  };

  ws.onclose = (event) => {
    const uptime = ((Date.now() - connectionTime) / 1000).toFixed(1);
    console.warn(`[BG] WebSocket 断开 code=${event.code} uptime=${uptime}s`);
    isConnected = false;
    ws = null;
    nativeStatusCache = null;
    clearInterval(pingTimer);

    forwardToPopup({ type: 'native_disconnected', reason: `WebSocket closed (${event.code})`, uptime: parseFloat(uptime) });
    notifyAllTabs({ type: 'native_disconnected', reason: 'WebSocket closed' });

    // 自动重连（用户主动连接后才触发）
    if (autoReconnect) {
      console.log(`[BG] ${WS_RECONNECT_DELAY/1000}s 后自动重连...`);
      reconnectTimer = setTimeout(connectWS, WS_RECONNECT_DELAY);
    }
  };

  ws.onerror = (event) => {
    console.error('[BG] WebSocket 错误');
    // onclose 会紧随其后处理断线逻辑
  };
}

function disconnectWS() {
  console.log('[BG] 主动断开 WebSocket');
  autoReconnect = false;
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  if (ws) {
    try { ws.close(1000, 'user disconnect'); } catch (e) {}
    ws = null;
  }
  isConnected = false;
  nativeStatusCache = null;
}

function sendToWS(message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[BG] WS 未连接，消息丢弃:', message.type);
    return false;
  }
  try {
    ws.send(JSON.stringify(message));
    return true;
  } catch (e) {
    console.error('[BG] WS 发送失败:', e.message);
    return false;
  }
}

// ========== 来自 WebSocket 服务端的消息分发 ==========

function handleMessageFromServer(message) {
  switch (message.type) {

    case 'pong':
      break;

    case 'status':
      nativeStatusCache = { ...message };
      forwardToPopup({ ...message, connected: isConnected });
      break;

    // ---- VL 帧分析 ----
    case 'frame_received':
      forwardToActiveTab({ type: 'frame_received', frame_count: message.frame_count });
      break;

    case 'frame_analysis_result':
      forwardToActiveTab({ type: 'frame_analysis_result', results: message.results });
      break;

    case 'frame_analysis_error':
      forwardToActiveTab({ type: 'frame_analysis_error', error: message.error });
      break;

    // ---- 模型加载状态 ----
    case 'model_loading':
      console.log(`[BG] ${message.model?.toUpperCase()} 模型开始加载...`);
      if (nativeStatusCache && message.model === 'vl') nativeStatusCache.vl_loaded = false;
      forwardToPopup(message);
      forwardToActiveTab(message);  // content.js 也需要知道（preload_vl_done 依赖它）
      break;

    case 'model_loaded':
      console.log(`[BG] ✅ ${message.model?.toUpperCase()} 模型加载完成 (${message.elapsed_sec}s)`);
      if (nativeStatusCache && message.model === 'vl') nativeStatusCache.vl_loaded = true;
      forwardToPopup(message);
      break;

    case 'preload_vl_done':
      console.log(`[BG] VL 预加载完成: ready=${message.vl_ready}`);
      forwardToActiveTab({
        type: 'preload_vl_done',
        vl_ready: message.vl_ready,
        elapsed_sec: message.elapsed_sec,
        error: message.error,
      });
      if (nativeStatusCache && message.vl_ready) nativeStatusCache.vl_loaded = true;
      forwardToPopup({ type: message.vl_ready ? 'model_loaded' : 'model_loading', model: 'vl', elapsed_sec: message.elapsed_sec });
      break;

    case 'preload_vl_queued':
      // 预加载已入队，等待 preload_vl_done
      break;

    // ---- 增量总结 ----
    case 'incremental_summary':
      console.log(`[BG] 📝 增量总结 #${message.summary_index} (${message.source}/${message.video_key})`);
      forwardToActiveTab({
        type: 'incremental_summary',
        summary: message.summary,
        summary_index: message.summary_index,
        total_frames: message.total_frames,
        elapsed_sec: message.elapsed_sec,
        time_range: message.time_range,
        save_path: message.save_path,
        source: message.source,
        video_key: message.video_key,
      });
      break;

    case 'summary_progress':
      forwardToActiveTab({
        type: 'summary_progress',
        status: message.status,
        frame_count: message.frame_count,
        total_frames: message.total_frames,
        summary_index: message.summary_index,
      });
      break;

    case 'summary_error':
      console.warn('[BG] 增量总结失败:', message.error);
      forwardToActiveTab({ type: 'summary_error', error: message.error });
      break;

    // ---- 笔记保存 ----
    case 'notes_saved':
      forwardToActiveTab({
        type: 'notes_saved',
        success: message.data?.success,
        filepath: message.data?.filepath,
        error: message.data?.error,
      });
      break;

    case 'shutdown_ack':
      disconnectWS();
      forwardToPopup({ type: 'native_stopped' });
      break;

    case 'error':
      console.error('[BG] Server error:', message.error);
      notifyAllTabs({ type: 'analysis_error', error: message.error });
      forwardToPopup(message);
      break;

    default:
      console.log('[BG] Unhandled message type:', message.type);
  }
}

// ========== 消息转发工具函数 ==========

function forwardToActiveTab(message) {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, message).catch(() => {});
  });
}

function notifyAllTabs(message) {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) chrome.tabs.sendMessage(tab.id, message).catch(() => {});
  });
}

function forwardToPopup(message) {
  chrome.runtime.sendMessage(message).catch(() => {});
}

// ========== 来自 Content Script / Popup 的消息监听 ==========

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const msgType = request.type;
  const source = sender.tab ? `tab:${sender.tab.id}` : 'popup';
  console.log(`[BG] onMessage: type="${msgType}" from ${source}`);

  switch (msgType) {

    // ---- 连接管理 ----
    case 'connect_native': {
      if (!isConnected) {
        connectWS();
        // 等待连接建立（最多 2s）
        let waited = 0;
        const check = setInterval(() => {
          waited += 100;
          if (isConnected || waited >= 2000) {
            clearInterval(check);
            const uptime = isConnected ? ((Date.now() - connectionTime) / 1000) : 0;
            sendResponse({ connected: isConnected, uptime, timestamp: Date.now() });
          }
        }, 100);
        return true;  // 异步 sendResponse
      }
      const uptime = (Date.now() - connectionTime) / 1000;
      sendResponse({ connected: true, uptime, timestamp: Date.now() });
      return false;
    }

    case 'disconnect_native': {
      disconnectWS();
      sendResponse({ disconnected: true });
      return false;
    }

    case 'get_connection_status': {
      const uptime = isConnected ? ((Date.now() - connectionTime) / 1000) : 0;
      sendResponse({ connected: isConnected, uptime });
      return false;
    }

    // ---- 状态查询 ----
    case 'get_status': {
      if (isConnected) sendToWS({ type: 'get_status' });
      const s = (isConnected && nativeStatusCache) ? nativeStatusCache : {};
      sendResponse({
        pid: s.pid,
        python: s.python,
        hostname: s.hostname,
        vl_loaded: !!s.vl_loaded,
        vl_loading: !!s.vl_loading,
        vl_model: s.vl_model,
        device: s.device,
        version: s.version,
        messages_processed: s.messages_processed || 0,
        uptime_sec: s.uptime_sec,
        connected: isConnected,
        uptime: isConnected ? ((Date.now() - connectionTime) / 1000) : 0,
        ws_url: WS_URL,
      });
      return false;
    }

    // ---- 探测（尝试连接）----
    case 'probe_native': {
      if (isConnected) {
        sendToWS({ type: 'get_status' });
        sendResponse({ probed: true, was_connected: true });
      } else {
        connectWS();
        setTimeout(() => {
          if (isConnected) sendToWS({ type: 'get_status' });
        }, 300);
        sendResponse({ probed: true, was_connected: false });
      }
      return false;
    }

    // ---- VL 帧分析（来自 content script）----
    case 'analyze_frame': {
      const sent = sendToWS({ type: 'analyze_frame', data: request.data });
      sendResponse({ sent });
      return false;
    }

    // ---- VL 模型预加载（来自 content script）----
    case 'preload_vl': {
      const sent = sendToWS({ type: 'preload_vl' });
      sendResponse({ sent });
      return false;
    }

    // ---- 保存笔记 ----
    case 'save_notes': {
      const sent = sendToWS({ type: 'save_notes', data: request.data });
      sendResponse({ sent });
      return false;
    }

    case 'stop_analysis': {
      sendResponse({ success: true });
      return false;
    }

    default:
      console.warn(`[BG] Unknown message type: ${msgType}`);
      sendResponse({ error: `Unknown: ${msgType}` });
      return false;
  }
});

// ========== 扩展安装/启动 ==========

chrome.runtime.onInstalled.addListener(() => {
  console.log('[BG] AI Video Analyzer v5.0 installed (WebSocket Mode)');
});

// ========== Service Worker Keep-Alive ==========
// WebSocket 模式下，keepalive 同时维持 SW 和 WS 心跳

chrome.alarms?.create('keepalive', { delayInMinutes: 0.2, periodInMinutes: 0.33 });
chrome.alarms?.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepalive') {
    console.log(`[BG] 💓 keepalive tick (ws=${isConnected ? 'connected' : 'disconnected'})`);
    // WebSocket 心跳由 pingTimer 负责，这里只维持 SW 存活
  }
});

