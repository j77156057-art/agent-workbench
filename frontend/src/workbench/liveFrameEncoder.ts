import type { EncodedLiveFrame, FrameEncodingOptions } from './liveFrameEncoding'

export interface EncoderWorker {
  onmessage: ((event: MessageEvent<{ id: number; result?: EncodedLiveFrame; error?: string }>) => void) | null
  onerror: (() => void) | null
  onmessageerror: (() => void) | null
  postMessage(value: unknown, transfer: Transferable[]): void
  terminate(): void
}

/** One worker per live session, one outstanding frame, with bounded recovery time. */
export class LiveFrameEncoder {
  private worker: EncoderWorker | null = null
  private disabled = false
  private nextId = 0
  private pending: { id: number; resolve(value: EncodedLiveFrame): void; reject(cause: Error): void;
    timer: ReturnType<typeof setTimeout> } | null = null
  private factory: () => EncoderWorker
  private timeoutMs: number

  constructor(factory: () => EncoderWorker = () => new Worker(
    new URL('./liveFrameEncoder.worker.ts', import.meta.url), { type: 'module' }) as unknown as EncoderWorker,
    timeoutMs = 3000) { this.factory = factory; this.timeoutMs = timeoutMs }

  available(): boolean { return !this.disabled }

  encode(bitmap: ImageBitmap, options: FrameEncodingOptions): Promise<EncodedLiveFrame> {
    if (this.disabled || this.pending) {
      bitmap.close()
      return Promise.reject(new Error('视频后台编码不可用或仍在处理上一帧'))
    }
    return new Promise((resolve, reject) => {
      try {
        const worker = this.worker ??= this.factory()
        worker.onmessage = ({ data }) => {
          const pending = this.pending
          if (!pending || data.id !== pending.id) return
          if (data.error || !data.result) { this.dispose(new Error(data.error || '后台编码返回无效结果')); return }
          clearTimeout(pending.timer)
          this.pending = null
          pending.resolve(data.result)
        }
        worker.onerror = worker.onmessageerror = () => this.dispose(new Error('视频后台编码失败'))
        const id = ++this.nextId
        this.pending = { id, resolve, reject, timer: setTimeout(() => this.dispose(new Error('视频后台编码超时')), this.timeoutMs) }
        worker.postMessage({ id, bitmap, options }, [bitmap])
      } catch (cause) {
        bitmap.close()
        this.dispose(cause instanceof Error ? cause : new Error('视频后台编码启动失败'))
        reject(cause)
      }
    })
  }

  dispose(cause = new Error('视频会话已停止')): void {
    this.disabled = true
    this.worker?.terminate()
    this.worker = null
    if (this.pending) {
      clearTimeout(this.pending.timer)
      this.pending.reject(cause)
      this.pending = null
    }
  }
}
