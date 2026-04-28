/**
 * Popup v4.1 — 纯 VL 实时帧分析
 *
 * 架构说明（重要）：
 *   WebSocket 工作方式：
 *   - 需要先在终端执行: cd local-service && python3 websocket_server.py
 *   - 然后点击 popup 中的"连接服务"建立 WebSocket 连接
 *   - WebSocket 常驻服务，断线后自动重连
 *
 * 状态模型：
 *   disconnected  → 尚未连接（初始/断开后）
 *   connecting    → connectNative 进行中
 *   connected     → WebSocket 已建立连接
 *     vl_pending  → VL 模型尚未加载
 *     vl_loading  → VL 模型加载中
 *     vl_ready    → VL 模型已就绪
 */
document.addEventListener('DOMContentLoaded', () => {
  // ========== DOM 元素 ==========
  const $ = (id) => document.getElementById(id);

  const serviceBadge   = $('service-badge');
  const localInfo      = $('local-info');
  const hintOffline    = $('hint-offline');
  const infoPid        = $('info-pid');
  const infoPython     = $('info-python');
  const infoUptime     = $('info-uptime');
  const infoMsgs       = $('info-msgs');
  const infoVlModel    = $('vl-model-name');
  const modelStatus    = $('model-status');
  const vlStatus       = $('vl-status');
  const btnConnect     = $('btn-connect');
  const btnDisconnect  = $('btn-disconnect');
  const btnAnalyze     = $('btn-analyze');
  const btnRefresh     = $('btn-refresh');
  const progressSection = $('progress-section');
  const progressBar    = $('progress-bar');
  const progressStage  = $('progress-stage');
  const progressPct    = $('progress-pct');
  const btnOpenLog     = $('btn-open-log');

  // ========== 状态 ==========
  let connected  = false;   // 是否已建立 Native Messaging 连接
  let connecting = false;   // 是否正在连接中

  // ========== 初始化 ==========
  init();

  function init() {
    btnRefresh.addEventListener('click', handleRefresh);
    btnConnect.addEventListener('click', handleConnect);
    btnDisconnect.addEventListener('click', handleDisconnect);
    btnAnalyze.addEventListener('click', handleAnalyze);
    btnOpenLog.addEventListener('click', handleOpenLogFile);

    // 订阅来自 background 的消息
    chrome.runtime.onMessage.addListener(onBackgroundMessage);

    // 初始化时查询当前连接状态（可能之前就已连接）
    initStatus();
  }

  // ================================================================
  //  初始化：查询已有连接状态
  // ================================================================

  async function initStatus() {
    try {
      const resp = await sendMessage({ type: 'get_status' });
      if (resp && resp.connected) {
        // 已有连接（比如 popup 关了又开，background 仍保持连接）
        connected = true;
        showConnected(resp);
        updateLocalInfo(resp);
        updateVLModelUI(resp.vl_loaded, resp.vl_model, resp.vl_loading);
      } else {
        // 尚未连接：显示"就绪，等待连接"状态
        showDisconnected();
      }
    } catch (e) {
      showDisconnected();
    }
  }

  // ================================================================
  //  UI 状态渲染函数
  // ================================================================

  /** 未连接状态 */
  function showDisconnected() {
    connected = false;
    serviceBadge.textContent = '未连接';
    serviceBadge.className = 'status-badge offline';
    localInfo.style.display = 'none';
    modelStatus.classList.remove('visible');
    hintOffline.style.display = 'block';
    btnConnect.textContent = '🔗 连接服务';
    btnConnect.className = 'btn btn-connect';
    btnConnect.disabled = false;   // 允许直接点击连接，无需先"检测到"
    btnDisconnect.disabled = true;
    btnAnalyze.disabled = true;
  }

  /** 连接成功状态 */
  function showConnected(info) {
    connected = true;
    serviceBadge.textContent = '已连接';
    serviceBadge.className = 'status-badge online';
    hintOffline.style.display = 'none';
    localInfo.style.display = 'block';
    btnConnect.textContent = '✅ 已连接';
    btnConnect.className = 'btn btn-connect connected';
    btnConnect.disabled = true;
    btnDisconnect.disabled = false;
    btnAnalyze.disabled = false;
    if (info) updateLocalInfo(info);
  }

  /** 连接断开状态（服务曾在线但现在断了）*/
  function showNativeDisconnected(reason) {
    connected = false;
    serviceBadge.textContent = '连接断开';
    serviceBadge.className = 'status-badge offline';
    modelStatus.classList.remove('visible');
    localInfo.style.display = 'block';  // 保留最后一次进程信息以便排查
    hintOffline.style.display = 'none';
    btnConnect.textContent = '🔗 重新连接';
    btnConnect.className = 'btn btn-connect';
    btnConnect.disabled = false;
    btnDisconnect.disabled = true;
    btnAnalyze.disabled = true;
  }

  function updateLocalInfo(info) {
    if (!info) return;
    if (info.pid)                infoPid.textContent     = info.pid;
    if (info.python)             infoPython.textContent  = shortenPath(info.python, 32);
    if (info.uptime_sec != null) infoUptime.textContent  = formatUptime(info.uptime_sec);
    if (info.messages_processed != null)
                                 infoMsgs.textContent    = info.messages_processed;
    if (info.vl_model)           infoVlModel.textContent = info.vl_model;
  }

  /**
   * 更新 VL 模型状态区块
   * @param {boolean} loaded  - true=已就绪, false=未加载/加载中
   * @param {string} modelName
   * @param {boolean} isLoading - 区分"待加载"和"加载中"
   */
  function updateVLModelUI(loaded, modelName, isLoading = false) {
    modelStatus.classList.add('visible');
    if (loaded) {
      vlStatus.textContent = '✅ 已就绪';
      vlStatus.className   = 'model-item-val ready';
      // 加载完成后隐藏 spinner
      const spinner = modelStatus.querySelector('.spinner');
      if (spinner) spinner.style.display = 'none';
    } else if (isLoading) {
      vlStatus.textContent = '⏳ 加载中...';
      vlStatus.className   = 'model-item-val loading';
      const spinner = modelStatus.querySelector('.spinner');
      if (spinner) spinner.style.display = '';
    } else {
      vlStatus.textContent = '⏳ 待加载';
      vlStatus.className   = 'model-item-val pending';
      const spinner = modelStatus.querySelector('.spinner');
      if (spinner) spinner.style.display = 'none';
    }
    if (modelName) infoVlModel.textContent = modelName;
  }

  // ================================================================
  //  连接 / 断开
  // ================================================================

  async function handleConnect() {
    if (connected || connecting) return;
    connecting = true;

    btnConnect.disabled  = true;
    btnConnect.textContent = '⏳ 连接中...';
    serviceBadge.textContent = '连接中';
    serviceBadge.className   = 'status-badge connecting';

    try {
      const resp = await sendMessage({ type: 'connect_native' });
      if (resp && resp.connected) {
        // 连接成功，请求最新状态
        showConnected(null);
        // 稍等 300ms，让 background 处理完 ping/pong 后再拉状态
        await sleep(300);
        const status = await sendMessage({ type: 'get_status' });
        if (status) {
          updateLocalInfo(status);
          updateVLModelUI(status.vl_loaded, status.vl_model, status.vl_loading);
        }
      } else {
        showConnectError();
      }
    } catch (e) {
      showConnectError();
    }
    connecting = false;
  }

  async function handleDisconnect() {
    if (!connected) return;
    try { await sendMessage({ type: 'disconnect_native' }); } catch (e) {}
    showDisconnected();
  }

  function showConnectError() {
    connecting = false;
    serviceBadge.textContent = '连接失败';
    serviceBadge.className   = 'status-badge offline';
    btnConnect.textContent   = '🔗 重试连接';
    btnConnect.disabled      = false;
    btnConnect.className     = 'btn btn-connect';
    hintOffline.style.display = 'block';
    hintOffline.innerHTML = '连接失败，请确认已在终端运行：<br><code>python3 local-service/websocket_server.py</code>';
  }

  // ================================================================
  //  刷新按钮 — 重新同步连接状态
  // ================================================================

  async function handleRefresh() {
    if (btnRefresh.classList.contains('spinning')) return;
    btnRefresh.classList.add('spinning');
    btnRefresh.disabled = true;

    try {
      if (connected) {
        // 已连接：直接拉最新状态
        const status = await sendMessage({ type: 'get_status' });
        if (status && status.connected) {
          updateLocalInfo(status);
          updateVLModelUI(status.vl_loaded, status.vl_model, status.vl_loading);
        } else {
          // 连接已经断了
          showNativeDisconnected('refresh detected');
        }
      } else {
        // 未连接：探测是否有正在运行的服务（尝试连接）
        await sendMessage({ type: 'probe_native' });
        await sleep(700);
        const status = await sendMessage({ type: 'get_status' });
        if (status && status.connected) {
          connected = true;
          showConnected(status);
          updateVLModelUI(status.vl_loaded, status.vl_model, status.vl_loading);
        } else {
          // 确实没有服务在运行
          showDisconnected();
        }
      }
    } catch (e) {}

    setTimeout(() => {
      btnRefresh.classList.remove('spinning');
      btnRefresh.disabled = false;
    }, 300);
  }

  // 定期刷新运行时长/消息数/VL 模型状态（仅在已连接时）
  setInterval(async () => {
    if (!connected) return;
    try {
      const status = await sendMessage({ type: 'get_status' });
      if (status && status.connected) {
        if (status.uptime_sec != null) infoUptime.textContent = formatUptime(status.uptime_sec);
        if (status.messages_processed != null) infoMsgs.textContent = status.messages_processed;
        // 同步 VL 模型状态（预热模式下模型会在后台加载，需要实时更新）
        updateVLModelUI(status.vl_loaded, status.vl_model, status.vl_loading);
      } else if (status && !status.connected) {
        showNativeDisconnected('poll detected');
      }
    } catch (e) {}
  }, 3000);

  // ================================================================
  //  一键分析
  // ================================================================

  function handleAnalyze() {
    if (!connected) return;

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.sendMessage(tabs[0].id, { type: 'toggle_analysis', mode: 'visual' }, (response) => {
          if (chrome.runtime.lastError || !response) {
            alert('请在包含视频的页面使用（B站、YouTube 等）');
          } else {
            window.close();
          }
        });
      }
    });
  }

  function handleOpenLogFile() {
    alert(
      '日志位置:\n~/aiS/video-analysis-system/logs/\n\n' +
      '最新日志: native_host_latest.log\n\n' +
      '终端查看:\ncd ~/aiS/video-analysis-system/logs && tail -f native_host_latest.log'
    );
  }

  // ================================================================
  //  Background 消息监听（异步推送）
  // ================================================================

  function onBackgroundMessage(msg) {
    switch (msg.type) {

      case 'status':
        // background 主动推送的状态更新
        if (msg.pid) {
          updateLocalInfo(msg);
          if (connected) updateVLModelUI(msg.vl_loaded, msg.vl_model, false);
        }
        break;

      case 'native_disconnected':
        showNativeDisconnected(msg.reason);
        break;

      case 'native_stopped':
        showDisconnected();
        break;

      case 'model_loading':
        if (msg.model === 'vl' && connected) {
          updateVLModelUI(false, null, true /* isLoading */);
        }
        break;

      case 'model_loaded':
        if (msg.model === 'vl' && connected) {
          updateVLModelUI(true, null, false);
        }
        break;

      case 'frame_received':
        if (connected) showProgress(
          Math.min((parseInt(progressBar.style.width) || 0) + 10, 90),
          `帧 #${msg.frame_count || '?'} → VL 推理中...`
        );
        break;

      case 'frame_analysis_result':
        showProgress(100, '✅ 分析完成!');
        hideProgressDelayed();
        break;

      case 'frame_analysis_error':
        hideProgress();
        break;

      case 'analysis_error':
      case 'error':
        hideProgress();
        break;
    }
    return true;
  }

  // ================================================================
  //  进度条
  // ================================================================

  function showProgress(pct, stageText) {
    progressSection.classList.add('visible');
    progressBar.style.width = Math.min(pct, 100) + '%';
    progressStage.textContent = stageText || '';
    progressPct.textContent   = Math.min(pct, 100) + '%';
  }

  function hideProgress() {
    progressSection.classList.remove('visible');
    progressBar.style.width = '0%';
  }

  function hideProgressDelayed() { setTimeout(hideProgress, 2000); }

  // ================================================================
  //  工具函数
  // ================================================================

  function sendMessage(msg) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) { resolve(null); }
        else { resolve(resp); }
      });
    });
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  function shortenPath(s, max = 35) {
    if (!s) return '-';
    s = String(s);
    return s.length <= max ? s : '…' + s.slice(-(max - 1));
  }

  function formatUptime(sec) {
    if (sec == null || sec === 0) return '-';
    sec = Math.round(sec);
    if (sec < 60)   return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  }
});

