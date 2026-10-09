/**
 * 采集侧 AudioWorklet：把麦克风的 Float32 重采样成 16kHz 单声道 Int16 PCM，
 * 每凑够一帧（默认 1600 样本 = 100ms）就 postMessage 给主线程。
 *
 * 「按住说话」的门控在这里做：没按下时整帧都不产生，主线程不用做二次过滤，
 * 也不会把静音数据推到 WebSocket 上。
 */

const TARGET_RATE = 16000;
const FRAME_SIZE = 1600;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || TARGET_RATE;
    this.frameSize = opts.frameSize || FRAME_SIZE;

    // sampleRate 是 AudioWorkletGlobalScope 提供的全局变量。
    // 浏览器未采纳我们请求的采样率时，ratio != 1，走线性插值重采样。
    this.ratio = sampleRate / this.targetRate;

    this.input = [];      // 待重采样的输入样本
    this.phase = 0;       // 重采样相位，跨 process() 调用保持连续
    this.output = [];     // 重采样后的输出样本
    this.muted = true;    // 默认不发：按下按钮前不该有音频流出去

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && data.type === 'gate') this.muted = !data.on;
    };
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i += 1) this.input.push(channel[i]);

    // 线性插值重采样；ratio === 1 时退化成直接搬运。
    while (this.phase + 1 < this.input.length) {
      const i0 = Math.floor(this.phase);
      const frac = this.phase - i0;
      this.output.push(
        this.input[i0] * (1 - frac) + this.input[i0 + 1] * frac,
      );
      this.phase += this.ratio;
    }

    const consumed = Math.floor(this.phase);
    if (consumed > 0) {
      this.input.splice(0, consumed);
      this.phase -= consumed;
    }

    while (this.output.length >= this.frameSize) {
      const frame = this.output.splice(0, this.frameSize);
      if (!this.muted) {
        const pcm = new Int16Array(frame.length);
        for (let i = 0; i < frame.length; i += 1) {
          const v = Math.max(-1, Math.min(1, frame[i]));
          pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
        }
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
    }

    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
