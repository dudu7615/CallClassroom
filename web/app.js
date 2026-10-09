/**
 * 操作者页面：WebSocket 收发 PCM，AudioWorklet 采集，AudioBufferSource 排程播放。
 *
 * 上行（喊话）：麦克风 → pcm-capture worklet → 16kHz Int16 帧 → WebSocket
 * 下行（收音）：WebSocket → Int16 帧 → 排程到 AudioContext 播放
 *
 * 半双工在两端都做：服务端在有人喊话时停发教室声音，这里在按住时再本地静音一次，
 * 因为此刻可能还有已在队列里的帧没播完。
 */

// 采样率由服务端决定（它要迁就教室声卡的实际能力），连上后在 state 消息里下发。
// 这里的默认值只在收到 state 之前用得到。
const DEFAULT_RATE = 16000;
const MAX_LEAD = 0.4;             // 播放排程最多领先实时多少秒
const RECONNECT_MAX = 8000;

const $ = (id) => document.getElementById(id);
const el = {
  dotLink: $('dot-link'), txtLink: $('txt-link'),
  selIn: $('sel-in'), selOut: $('sel-out'),
  engineNote: $('engine-note'), engineError: $('engine-error'),
  monitor: $('monitor'), meterFill: $('meter-fill'),
  ptt: $('ptt'), pttLabel: $('ptt-label'), pttHint: $('ptt-hint'),
  start: $('start'), hint: $('hint'), log: $('log'),
};

const state = {
  ws: null,
  reconnectDelay: 500,
  rate: DEFAULT_RATE,
  ctx: null,
  outGain: null,
  analyser: null,
  captureNode: null,
  micStream: null,
  micReady: false,
  starting: false,
  holding: false,
  monitoring: true,
  playHead: 0,
  sources: new Set(),
  meterTimer: null,
};

// ------------------------------------------------------------------ 日志

function log(...args) {
  const line = args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
  console.log(line);
  if (!el.log) return;
  el.log.hidden = false;
  el.log.textContent += `${new Date().toLocaleTimeString()}  ${line}\n`;
  el.log.scrollTop = el.log.scrollHeight;
}

// ------------------------------------------------------------------ WebSocket

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

function connect() {
  const ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = () => {
    state.reconnectDelay = 500;
    el.dotLink.classList.add('on');
    el.txtLink.textContent = '已连接';
    log('WebSocket 已连接');
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      handleControl(JSON.parse(event.data));
    } else {
      enqueuePlayback(event.data);
    }
  };

  ws.onclose = () => {
    el.dotLink.classList.remove('on');
    el.txtLink.textContent = '已断开';
    log(`连接断开，${state.reconnectDelay}ms 后重连`);
    setTimeout(connect, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, RECONNECT_MAX);
  };

  ws.onerror = () => log('WebSocket 出错');
}

function send(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
  }
}

function handleControl(msg) {
  switch (msg.type) {
    case 'state':
      applyDeviceState(msg);
      log(`服务端就绪 ${msg.rate}Hz，采样块 ${msg.blocksize}`);
      break;
    case 'peer':
      el.dotLink.classList.toggle('live', msg.talking);
      el.txtLink.textContent = msg.talking ? '他人正在喊话' : '已连接';
      break;
    case 'pong':
      break;
    default:
      log('未知控制消息', msg);
  }
}

// ------------------------------------------------------------------ 教室设备

/**
 * 用服务端的状态刷新设备区。
 *
 * `selected_*` 是配置里选定的，`device_*` 是当前真正打开的。两者不一致说明
 * 选的设备没找到、服务端退回了系统默认——得让操作者看见，否则会以为选错了。
 */
function applyDeviceState(msg) {
  el.selIn.value = msg.selected_in ?? '';
  el.selOut.value = msg.selected_out ?? '';

  // 服务端换了采样率（迁就声卡），AudioContext 建好就改不了采样率，只能重建
  if (msg.rate && msg.rate !== state.rate) {
    const previous = state.rate;
    state.rate = msg.rate;
    if (state.ctx) {
      log(`采样率 ${previous} → ${msg.rate}Hz，重建音频通路`);
      teardownAudio();
      startAudio();
    }
  }

  if (msg.error) {
    el.engineError.hidden = false;
    el.engineError.textContent = `服务端音频引擎未启动：${msg.error}`;
  } else {
    el.engineError.hidden = true;
  }

  if (!msg.engine_available) {
    el.engineNote.textContent = msg.error ? '声卡不可用' : '未占用（有操作者连上时自动打开）';
    return;
  }

  const opened = `${msg.device_in || '默认'} / ${msg.device_out || '默认'}`;
  const fellBack =
    (msg.selected_in && msg.selected_in !== msg.device_in) ||
    (msg.selected_out && msg.selected_out !== msg.device_out);
  const khz = msg.rate ? ` @ ${msg.rate / 1000}kHz` : '';
  el.engineNote.textContent =
    `正在使用：${opened}${khz}${fellBack ? '（所选设备未找到，已回退默认）' : ''}`;
}

/** 拉一次设备列表填充下拉框。列表本身不随连接状态变化，拉一次就够。 */
async function loadDevices() {
  try {
    const response = await fetch('api/devices');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    fillSelect(el.selIn, data.devices, data.selected.in_device, true);
    fillSelect(el.selOut, data.devices, data.selected.out_device, false);
    el.selIn.disabled = false;
    el.selOut.disabled = false;
  } catch (error) {
    log(`读取设备列表失败：${error.message}`);
    el.engineNote.textContent = '读取设备列表失败';
  }
}

function fillSelect(select, devices, selected, wantInput) {
  const usable = devices.filter((d) => (wantInput ? d.inputs : d.outputs) > 0);
  select.replaceChildren(new Option('系统默认', ''));
  for (const device of usable) {
    select.append(new Option(device.name, device.name));
  }
  select.value = selected ?? '';
  if (selected && select.value !== selected) {
    // 配置里存的设备现在不在列表里：照样显示出来，别让操作者以为从没选过
    select.append(new Option(`${selected}（未找到）`, selected));
    select.value = selected;
  }
}

async function selectDevice() {
  el.selIn.disabled = true;
  el.selOut.disabled = true;
  try {
    const response = await fetch('api/devices/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        in_device: el.selIn.value || null,
        out_device: el.selOut.value || null,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyDeviceState(data.status);
    log(`教室设备已切换：${el.selIn.value || '系统默认'} / ${el.selOut.value || '系统默认'}`);
  } catch (error) {
    log(`切换设备失败：${error.message}`);
    el.engineError.hidden = false;
    el.engineError.textContent = `切换设备失败：${error.message}`;
  } finally {
    el.selIn.disabled = false;
    el.selOut.disabled = false;
  }
}

el.selIn.addEventListener('change', selectDevice);
el.selOut.addEventListener('change', selectDevice);

// ------------------------------------------------------------------ 播放（收音）

function enqueuePlayback(arrayBuffer) {
  const ctx = state.ctx;
  if (!ctx || !arrayBuffer.byteLength) return;

  const pcm = new Int16Array(arrayBuffer);
  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i += 1) floats[i] = pcm[i] / 0x8000;

  const buffer = ctx.createBuffer(1, floats.length, state.rate);
  buffer.copyToChannel(floats, 0);

  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(state.outGain);

  const now = ctx.currentTime;
  // 落后了就追到当前时刻；领先太多说明积压，直接丢弃重排，避免延迟越滚越大
  if (state.playHead < now + 0.02) state.playHead = now + 0.02;
  if (state.playHead > now + MAX_LEAD) state.playHead = now + 0.02;

  source.start(state.playHead);
  state.playHead += buffer.duration;

  state.sources.add(source);
  source.onended = () => state.sources.delete(source);
}

function stopPlayback() {
  for (const source of state.sources) {
    try { source.stop(); } catch { /* 已经自然结束 */ }
  }
  state.sources.clear();
  state.playHead = 0;
}

// ------------------------------------------------------------------ 电平表

function startMeter() {
  if (state.meterTimer) return;
  const data = new Uint8Array(state.analyser.fftSize);
  const tick = () => {
    state.meterTimer = requestAnimationFrame(tick);
    state.analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i += 1) {
      peak = Math.max(peak, Math.abs(data[i] - 128) / 128);
    }
    el.meterFill.style.width = `${Math.min(100, peak * 140).toFixed(1)}%`;
  };
  tick();
}

// ------------------------------------------------------------------ 音频启动

function requireSecureContext() {
  if (window.isSecureContext) return true;
  el.hint.textContent =
    '当前是非安全源（http:// 加局域网 IP），浏览器会禁用麦克风和 AudioWorklet。'
    + '请改用 https 访问，或在 Chrome 的 chrome://flags 里把本站加入 '
    + 'unsafely-treat-insecure-origin-as-secure 白名单。';
  log('非安全上下文：麦克风与 AudioWorklet 不可用');
  return false;
}

/** 拆掉音频通路。换采样率时必须先拆，因为 AudioContext 的采样率建好就不能改。 */
function teardownAudio() {
  setHolding(false);
  if (state.captureNode) {
    state.captureNode.disconnect();
    state.captureNode = null;
  }
  if (state.micStream) {
    for (const track of state.micStream.getTracks()) track.stop();
    state.micStream = null;
  }
  stopPlayback();
  if (state.ctx) {
    state.ctx.close();
    state.ctx = null;
  }
  state.outGain = null;
  state.analyser = null;
  state.micReady = false;
  el.ptt.disabled = true;
}

async function startAudio() {
  if (!requireSecureContext() || state.starting) return;
  state.starting = true;
  el.start.disabled = true;

  try {
    state.ctx = new AudioContext({ sampleRate: state.rate, latencyHint: 'interactive' });
    await state.ctx.resume();
    await state.ctx.audioWorklet.addModule('pcm-worklet.js');

    state.outGain = state.ctx.createGain();
    state.outGain.gain.value = state.monitoring ? 1 : 0;
    state.analyser = state.ctx.createAnalyser();
    state.analyser.fftSize = 512;
    state.outGain.connect(state.analyser);
    state.analyser.connect(state.ctx.destination);
    startMeter();

    if (state.ctx.sampleRate !== state.rate) {
      log(`浏览器未采纳 ${state.rate}Hz，实际 ${state.ctx.sampleRate}Hz，已在 worklet 内重采样`);
    }

    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const source = state.ctx.createMediaStreamSource(state.micStream);
    state.captureNode = new AudioWorkletNode(state.ctx, 'pcm-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      processorOptions: {
        targetRate: state.rate,
        frameSize: Math.max(1, Math.round(state.rate / 10)), // 100ms 一帧
      },
    });
    state.captureNode.port.onmessage = (event) => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(event.data);
    };
    source.connect(state.captureNode);
    state.captureNode.port.postMessage({ type: 'gate', on: false });

    state.micReady = true;
    el.ptt.disabled = false;
    el.hint.textContent = `按住下方按钮说话，松手即停（${state.rate / 1000}kHz）。`;
    log(`麦克风已就绪，采样率 ${state.rate}Hz`);
  } catch (error) {
    log(`启动音频失败：${error.name} ${error.message}`);
    el.hint.textContent = `启动失败：${error.message}`;
    el.start.disabled = false;
  } finally {
    state.starting = false;
  }
}

// ------------------------------------------------------------------ 按住说话

function setHolding(on) {
  if (state.holding === on) return;
  if (on && !state.micReady) return;
  state.holding = on;

  el.ptt.classList.toggle('holding', on);
  el.pttLabel.textContent = on ? '松手结束' : '按住说话';
  el.pttHint.textContent = on ? '正在向教室喊话' : '松手即停';

  if (state.captureNode) {
    state.captureNode.port.postMessage({ type: 'gate', on });
  }
  send({ type: 'ptt', on });

  if (on) {
    // 本地也立刻静音：服务端停发之前排进队列的教室声音还得掐掉
    stopPlayback();
    if (state.outGain) state.outGain.gain.value = 0;
  } else if (state.outGain) {
    state.outGain.gain.value = state.monitoring ? 1 : 0;
  }
}

el.ptt.addEventListener('pointerdown', (event) => {
  event.preventDefault();
  el.ptt.setPointerCapture(event.pointerId);
  setHolding(true);
});
for (const type of ['pointerup', 'pointercancel']) {
  el.ptt.addEventListener(type, () => setHolding(false));
}
el.ptt.addEventListener('contextmenu', (event) => event.preventDefault());

el.monitor.addEventListener('change', () => {
  state.monitoring = el.monitor.checked;
  if (state.outGain && !state.holding) {
    state.outGain.gain.value = state.monitoring ? 1 : 0;
  }
});

el.start.addEventListener('click', startAudio);

connect();
loadDevices();
