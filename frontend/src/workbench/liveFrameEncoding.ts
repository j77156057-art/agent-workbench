import { focusCropRect } from './liveVisionFocus'
import type { FocusRegion } from './liveVisionFocus'
import { changedVisualRegion, shouldAnalyzeVisualFrame } from './liveVisionSampling'
import type { VisualSample } from './liveVisionSampling'

export interface FrameEncodingOptions {
  maxEdge: number
  quality: number
  focus: FocusRegion | null
  previous: VisualSample | null
  lastSentAt: number
  capturedAt: number
  refreshMs: number
  force: boolean
  context?: {
    before?: { image: Blob; capturedAt: number }
    key?: { image: Blob; capturedAt: number }
    detail?: Blob | null
    reason: string
  }
}

export interface EncodedLiveFrame {
  image: Blob | null
  focusImage: Blob | null
  sample: VisualSample
  capturedAt: number
}

type FrameCanvas = HTMLCanvasElement | OffscreenCanvas
type FrameContext = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D

/** Shared by the worker and the browser fallback: sample before JPEG encoding. */
export async function encodeLiveFrame(
  source: CanvasImageSource,
  sourceWidth: number,
  sourceHeight: number,
  options: FrameEncodingOptions,
  canvases: { frame: FrameCanvas; focus: FrameCanvas; sample: FrameCanvas },
): Promise<EncodedLiveFrame> {
  const scale = Math.min(1, options.maxEdge / sourceWidth, options.maxEdge / sourceHeight)
  const width = Math.max(1, Math.round(sourceWidth * scale))
  const height = Math.max(1, Math.round(sourceHeight * scale))
  const resize = (canvas: FrameCanvas, w: number, h: number): FrameContext => {
    if (canvas.width !== w) canvas.width = w
    if (canvas.height !== h) canvas.height = h
    const context = canvas.getContext('2d') as FrameContext | null
    if (!context) throw new Error('无法创建视频采集画布')
    return context
  }
  const rect = options.focus ? focusCropRect(options.focus, width, height)
    : { x: 0, y: 0, width, height }
  const sampleContext = resize(canvases.sample, 64, 36)
  const originalRect = options.focus ? focusCropRect(options.focus, sourceWidth, sourceHeight)
    : { x: 0, y: 0, width: sourceWidth, height: sourceHeight }
  // Static probes only touch the tiny sample canvas; no full-size resize/JPEG.
  sampleContext.drawImage(source, originalRect.x, originalRect.y, originalRect.width, originalRect.height, 0, 0, 64, 36)
  const rgba = sampleContext.getImageData(0, 0, 64, 36).data
  const pixels = new Uint8Array(64 * 36 * 3)
  for (let i = 0; i < 64 * 36; i++) pixels.set(rgba.subarray(i * 4, i * 4 + 3), i * 3)
  const sample = { width: rect.width, height: rect.height, pixels }
  if (!options.force && !shouldAnalyzeVisualFrame(options.previous, sample,
    options.lastSentAt, options.capturedAt, options.refreshMs)) {
    return { image: null, focusImage: null, sample, capturedAt: options.capturedAt }
  }
  const encode = (canvas: FrameCanvas): Promise<Blob> => {
    if ('convertToBlob' in canvas) return canvas.convertToBlob({ type: 'image/jpeg', quality: options.quality })
    return new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error('共享画面截图失败')), 'image/jpeg', options.quality))
  }
  let focusBlob: Promise<Blob> | null = null
  resize(canvases.frame, width, height).drawImage(source, 0, 0, width, height)
  const detailRegion = options.focus || (options.context ? null : changedVisualRegion(options.previous, sample))
  if (detailRegion) {
    const detailRect = focusCropRect(detailRegion, sourceWidth, sourceHeight)
    const detailScale = Math.min(1, 1024 / detailRect.width, 1024 / detailRect.height)
    const detailWidth = Math.max(1, Math.round(detailRect.width * detailScale))
    const detailHeight = Math.max(1, Math.round(detailRect.height * detailScale))
    resize(canvases.focus, detailWidth, detailHeight).drawImage(source,
      detailRect.x, detailRect.y, detailRect.width, detailRect.height, 0, 0, detailWidth, detailHeight)
    focusBlob = encode(canvases.focus)
  }
  const [image, focusImage] = await Promise.all([encode(canvases.frame), focusBlob])
  if (options.context) {
    const opened: ImageBitmap[] = []
    try {
      const history: { bitmap: ImageBitmap; label: string }[] = []
      const labelTime = (at: number) => new Date(at).toISOString().slice(11, 23) + ' UTC'
      for (const [label, frame] of [['变化前', options.context.before], ['变化关键帧', options.context.key]] as const) {
        if (!frame) continue
        const bitmap = await createImageBitmap(frame.image)
        opened.push(bitmap); history.push({ bitmap, label: `${label} ${labelTime(frame.capturedAt)}` })
      }
      if (options.context.detail) {
        const bitmap = await createImageBitmap(image)
        opened.push(bitmap); history.push({ bitmap, label: `当前整屏 ${labelTime(options.capturedAt)}` })
      }
      const main = await createImageBitmap(options.context.detail || image)
      opened.push(main)
      const outputWidth = Math.min(1280, Math.max(960, main.width))
      const headerHeight = 34
      const historyHeight = history.length ? 210 : 0
      const mainHeight = Math.min(900, Math.round(main.height * outputWidth / main.width))
      const context = resize(canvases.sample, outputWidth, historyHeight + headerHeight * 2 + mainHeight)
      context.fillStyle = '#18202b'; context.fillRect(0, 0, canvases.sample.width, canvases.sample.height)
      const title = (text: string, x: number, y: number, maxWidth: number) => {
        context.font = '16px sans-serif'; context.fillStyle = '#ffffff'; context.fillText(text, x + 8, y + 23, maxWidth - 16)
      }
      title(`采集原因：${options.context.reason}；上方是历史/整屏参考，下方是当前${options.context.detail ? '高清局部' : '画面'}`, 0, 0, outputWidth)
      const tileWidth = outputWidth / Math.max(1, history.length)
      history.forEach(({ bitmap, label }, index) => {
        const fit = Math.min(tileWidth / bitmap.width, (historyHeight - headerHeight) / bitmap.height)
        const w = bitmap.width * fit, h = bitmap.height * fit
        title(label, index * tileWidth, headerHeight, tileWidth)
        context.drawImage(bitmap, index * tileWidth + (tileWidth - w) / 2, headerHeight * 2, w, h)
      })
      const y = historyHeight + headerHeight
      title(`当前${options.context.detail ? '高清局部' : '画面'} ${labelTime(options.capturedAt)}`, 0, y, outputWidth)
      const fit = Math.min(outputWidth / main.width, mainHeight / main.height)
      context.drawImage(main, (outputWidth - main.width * fit) / 2, y + headerHeight, main.width * fit, main.height * fit)
      return { image: await encode(canvases.sample), focusImage: null, sample, capturedAt: options.capturedAt }
    } finally { for (const bitmap of opened) bitmap.close() }
  }
  return { image, focusImage, sample, capturedAt: options.capturedAt }
}
