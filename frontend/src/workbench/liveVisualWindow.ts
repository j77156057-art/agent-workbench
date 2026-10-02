import type { EncodedLiveFrame } from './liveFrameEncoding'
import type { VisualSample } from './liveVisionSampling'

export interface VisualFrame extends EncodedLiveFrame { image: Blob }
export interface VisualSelection { current: VisualFrame; before: VisualFrame | null; key: VisualFrame | null; reason: string }

/** Local history is bounded independently of model/network capacity. */
export function createLiveVisualWindow() {
  let frames: VisualFrame[] = []
  let sent: VisualFrame | null = null
  let key: VisualFrame | null = null
  let keyScore = 0
  let activityUntil = 0
  let reason = '定期观察'
  let lastUploadAt = 0
  let uploads: { at: number; bytes: number }[] = []
  const maxBytes = 6_000_000
  const size = (frame: VisualFrame) => frame.image.size + (frame.focusImage?.size || 0)
  const score = (previous: VisualSample | null, current: VisualSample) => {
    if (!previous || previous.pixels.length !== current.pixels.length) return 1
    let changed = 0
    for (let i = 0; i < current.pixels.length; i += 3) {
      const delta = (Math.abs(previous.pixels[i] - current.pixels[i])
        + Math.abs(previous.pixels[i + 1] - current.pixels[i + 1])
        + Math.abs(previous.pixels[i + 2] - current.pixels[i + 2])) / 3
      if (delta >= 18) changed++
    }
    return changed / Math.max(1, current.pixels.length / 3)
  }
  const prune = (now: number) => {
    frames = frames.filter(frame => now - frame.capturedAt <= 2500)
    if (key && now - key.capturedAt > 2500) { key = null; keyScore = 0 }
    // Baseline and peak are references to retained frames, never extra copies.
    const retained = () => [...new Set([...frames, ...(sent ? [sent] : []), ...(key ? [key] : [])])]
    while (frames.length > 8 || retained().reduce((sum, frame) => sum + size(frame), 0) > maxBytes) {
      if (frames.length > 1) frames.shift()
      else if (key) { key = null; keyScore = 0 }
      else if (sent) sent = null
      else { frames = []; break }
    }
    uploads = uploads.filter(item => now - item.at < 60_000)
  }
  return {
    reset(clearBudget = false) {
      frames = []; sent = null; key = null; keyScore = 0; activityUntil = 0; lastUploadAt = 0
      if (clearBudget) uploads = []
    },
    activate(now: number, why: string) { activityUntil = now + 3000; reason = why },
    latest(): VisualFrame | null { return frames[frames.length - 1] || sent },
    push(frame: VisualFrame) {
      const previous = frames[frames.length - 1] || sent
      const change = score(previous?.sample || null, frame.sample)
      if (previous && change >= .012) { activityUntil = frame.capturedAt + 3000; reason = '画面发生变化' }
      // Keep the first peak on equal scores: restoration must not erase a flash.
      const fromBaseline = score(sent?.sample || null, frame.sample)
      if (sent && fromBaseline >= .012 && fromBaseline > keyScore) { key = frame; keyScore = fromBaseline }
      frames.push(frame)
      prune(frame.capturedAt)
    },
    interval(now: number, networkInterval: number, native: boolean) {
      return Math.max(networkInterval, native && now < activityUntil ? 300 : native ? 800 : 1400)
    },
    captureInterval(now: number, networkInterval: number, imageLimit: number) {
      prune(now)
      return networkInterval >= 1500 || uploads.length >= imageLimit ? 500 : 125
    },
    due(now: number, networkInterval: number, native: boolean, imageLimit: number) {
      prune(now)
      return (!lastUploadAt || now - lastUploadAt >= this.interval(now, networkInterval, native))
        && uploads.length < imageLimit && uploads.reduce((sum, item) => sum + item.bytes, 0) < 8_000_000
    },
    select(now: number): VisualSelection | null {
      prune(now)
      const current = frames[frames.length - 1]
      if (!current) return null
      return { current, before: sent && (key || score(sent.sample, current.sample) >= .012) && sent.capturedAt !== current.capturedAt
        && current.capturedAt - sent.capturedAt <= 5000 ? sent : null,
        key: key && key.capturedAt < current.capturedAt ? key : null, reason }
    },
    canUpload(now: number, bytes: number, imageLimit: number) {
      prune(now)
      return uploads.length < imageLimit && uploads.reduce((sum, item) => sum + item.bytes, 0) + bytes <= 8_000_000
    },
    uploaded(frame: VisualFrame, now: number, bytes: number) {
      lastUploadAt = now; uploads.push({ at: now, bytes }); sent = frame
      frames = frames.filter(item => item.capturedAt > frame.capturedAt)
      key = null; keyScore = 0
    },
    usage(now: number) { prune(now); return uploads.length },
    retainedBytes(now: number) {
      prune(now)
      return [...new Set([...frames, ...(sent ? [sent] : []), ...(key ? [key] : [])])]
        .reduce((sum, frame) => sum + size(frame), 0)
    },
  }
}
