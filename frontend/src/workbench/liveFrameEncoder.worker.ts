import { encodeLiveFrame } from './liveFrameEncoding'
import type { FrameEncodingOptions } from './liveFrameEncoding'

const scope = self as unknown as {
  onmessage: ((event: MessageEvent<{ id: number; bitmap: ImageBitmap; options: FrameEncodingOptions }>) => void) | null
  postMessage(message: unknown): void
}
let canvases: { frame: OffscreenCanvas; focus: OffscreenCanvas; sample: OffscreenCanvas } | null = null
scope.onmessage = async ({ data }) => {
  try {
    canvases ??= { frame: new OffscreenCanvas(1, 1), focus: new OffscreenCanvas(1, 1), sample: new OffscreenCanvas(64, 36) }
    const result = await encodeLiveFrame(data.bitmap, data.bitmap.width, data.bitmap.height, data.options, canvases)
    scope.postMessage({ id: data.id, result })
  } catch (cause) {
    scope.postMessage({ id: data.id, error: cause instanceof Error ? cause.message : '视频后台编码失败' })
  } finally {
    data.bitmap.close()
  }
}
