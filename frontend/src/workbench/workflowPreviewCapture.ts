/** Capture the current same-origin viewport, including DOM and canvas content. */
export async function capturePreviewFrame(frame: HTMLIFrameElement): Promise<Blob> {
  let doc: Document | undefined
  try { doc = frame.contentWindow?.document } catch {
    throw new Error('跨域预览暂不支持自动截图，请使用文字反馈或提供截图')
  }
  if (!doc?.body || doc.readyState !== 'complete') throw new Error('预览尚未加载完成，请稍后重试')
  const width = frame.clientWidth, height = frame.clientHeight
  if (!width || !height || width * height > 16_000_000) throw new Error('预览尺寸无效，请缩小画面后重试')
  // Never silently label a partial DOM rendering as a full screenshot.
  const visible = (element: Element) => {
    const rect = element.getBoundingClientRect()
    const style = doc!.defaultView!.getComputedStyle(element)
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0
      && rect.right > 0 && rect.bottom > 0 && rect.left < width && rect.top < height
  }
  for (const element of doc.querySelectorAll('iframe, video, object, embed')) {
    if (visible(element)) throw new Error('画面含嵌套页面或视频，暂需专用截图适配器')
  }
  for (const canvas of doc.querySelectorAll('canvas')) {
    if (visible(canvas)) canvas.toDataURL('image/png') // A tainted canvas must fail explicitly.
  }
  for (const image of doc.querySelectorAll('img')) {
    if (visible(image) && (!image.complete || image.naturalWidth === 0)) throw new Error('预览图片尚未加载成功，请稍后重试')
    if (visible(image) && image.currentSrc) {
      const url = new URL(image.currentSrc, doc.baseURI)
      if (url.protocol.startsWith('http') && url.origin !== new URL(doc.baseURI).origin) {
        throw new Error('画面含跨域图片，暂需专用截图适配器')
      }
    }
  }
  const { default: html2canvas } = await import('html2canvas')
  const view = doc.defaultView!
  const output = await html2canvas(doc.documentElement, {
    width, height, windowWidth: width, windowHeight: height,
    x: view.scrollX, y: view.scrollY, scrollX: view.scrollX, scrollY: view.scrollY,
    scale: 1, logging: false, imageTimeout: 5000, allowTaint: false, useCORS: false,
  })
  return new Promise((resolve, reject) => output.toBlob(blob => {
    if (!blob || blob.size > 8 * 1024 * 1024) reject(new Error('截图失败或过大，请缩小画面后重试'))
    else resolve(blob)
  }, 'image/png'))
}

/** Reload the displayed frame and wait for its new document; never capture the old one. */
export function refreshPreviewFrame(frame: HTMLIFrameElement): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); frame.removeEventListener('load', loaded); frame.removeEventListener('error', failed) }
    const loaded = () => { cleanup(); resolve() }
    const failed = () => { cleanup(); reject(new Error('预览重新加载失败，请手动刷新后重试')) }
    const timer = setTimeout(() => { cleanup(); reject(new Error('预览加载超时，请手动刷新后重试')) }, 12000)
    frame.addEventListener('load', loaded, { once: true })
    frame.addEventListener('error', failed, { once: true })
    // 同值赋值是刻意的：重设 src 才会触发一次新的 load，拿不到新文档就不截图。
    // eslint-disable-next-line no-self-assign
    frame.src = frame.src
  })
}
