/** Small RGB preview used only to decide whether another model call is useful. */
export interface VisualSample {
  width: number
  height: number
  pixels: Uint8Array
}

const SAMPLE_WIDTH = 32
const SAMPLE_HEIGHT = 18
const MAX_STALE_MS = 20_000

/** A localized change is a candidate detail crop, never a semantic diagnosis. */
export function changedVisualRegion(previous: VisualSample | null, current: VisualSample) {
  if (!previous || previous.width !== current.width || previous.height !== current.height
    || previous.pixels.length !== 64 * 36 * 3 || current.pixels.length !== previous.pixels.length) return null
  let x1 = 64, y1 = 36, x2 = -1, y2 = -1, count = 0
  for (let y = 0; y < 36; y++) for (let x = 0; x < 64; x++) {
    const i = (y * 64 + x) * 3
    const delta = (Math.abs(previous.pixels[i] - current.pixels[i])
      + Math.abs(previous.pixels[i + 1] - current.pixels[i + 1])
      + Math.abs(previous.pixels[i + 2] - current.pixels[i + 2])) / 3
    if (delta < 18) continue
    count++; x1 = Math.min(x1, x); x2 = Math.max(x2, x); y1 = Math.min(y1, y); y2 = Math.max(y2, y)
  }
  if (count < 28 || (x2 - x1 + 1) * (y2 - y1 + 1) > 64 * 36 * .5) return null
  x1 = Math.max(0, x1 - 2); y1 = Math.max(0, y1 - 2)
  x2 = Math.min(63, x2 + 2); y2 = Math.min(35, y2 + 2)
  return { x: x1 / 64, y: y1 / 36, width: (x2 - x1 + 1) / 64, height: (y2 - y1 + 1) / 36 }
}

export async function sampleVisualFrame(blob: Blob): Promise<VisualSample | null> {
  if (typeof createImageBitmap !== 'function') return null
  let bitmap: ImageBitmap | null = null
  try {
    bitmap = await createImageBitmap(blob)
    const canvas = document.createElement('canvas')
    canvas.width = SAMPLE_WIDTH
    canvas.height = SAMPLE_HEIGHT
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) return null
    context.drawImage(bitmap, 0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT)
    const rgba = context.getImageData(0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT).data
    const pixels = new Uint8Array(SAMPLE_WIDTH * SAMPLE_HEIGHT * 3)
    for (let i = 0; i < SAMPLE_WIDTH * SAMPLE_HEIGHT; i++) {
      const source = i * 4
      const target = i * 3
      pixels[target] = rgba[source]
      pixels[target + 1] = rgba[source + 1]
      pixels[target + 2] = rgba[source + 2]
    }
    return { width: bitmap.width, height: bitmap.height, pixels }
  } catch {
    // Unsupported decoders should not disable visual analysis.
    return null
  } finally {
    bitmap?.close()
  }
}

/** Compare with the last frame actually sent to the model, so small changes accumulate. */
export function shouldAnalyzeVisualFrame(
  previous: VisualSample | null,
  current: VisualSample | null,
  lastAnalyzedAt: number,
  now: number,
  maxStaleMs = MAX_STALE_MS,
): boolean {
  if (!previous || !current || !lastAnalyzedAt) return true
  if (now - lastAnalyzedAt >= maxStaleMs) return true
  if (previous.width !== current.width || previous.height !== current.height ||
      previous.pixels.length !== current.pixels.length) return true
  let difference = 0
  let changed = 0
  const count = current.pixels.length / 3
  for (let i = 0; i < current.pixels.length; i += 3) {
    const delta = Math.max(
      Math.abs(current.pixels[i] - previous.pixels[i]),
      Math.abs(current.pixels[i + 1] - previous.pixels[i + 1]),
      Math.abs(current.pixels[i + 2] - previous.pixels[i + 2]),
    )
    difference += delta
    if (delta >= 18) changed++
  }
  return difference / count >= 3.5 || changed / count >= 0.012
}
