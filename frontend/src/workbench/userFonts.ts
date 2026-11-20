// 用户自备字体 —— 字体文件由用户自己下载，导入后只存在这台机器的浏览器里。
//
// 为什么需要它（2026-11-20）：用户反馈「里面有些字体太细了，看不太清楚」。
// 应用自带的是 'Space Grotesk' + 'Noto Sans SC' 这一套无衬线，在高密度面板上
// 偏细；而「换一个更合适的字体」不是我们能替他决定的 —— 中文用户往往已经有
// 想用的字体文件在自己电脑上（中易/思源/苹方…），字号、字重、屏显效果都更合手。
//
// 三条路径，都不需要联网：
//   ① 导入本地字体文件（ttf / otf / woff2）→ FontFace 注册 → 可选为「界面字体」
//      或「阅读体」；
//   ② 不导入文件也能先把字重调粗一档（--style-weight-body 400 → 500）——
//      「太细」最直接的解法；
//   ③ 两者都用：自己的字体 + 粗体字重。
//
// 存哪：字体字节进 IndexedDB（一份中文字体几百 KB ~ 几 MB，localStorage 放不下）；
// 选中的偏好进 localStorage（key: docmind.userFont）。两页共用同一份 ——
// 换字体这件事不应该在两个入口里各做一次。
//
// 应用方式：只改 **:root 上的内联自定义属性**（内联样式优先级最高，压得住
// 各个令牌文件里的字体声明），不碰任何组件样式：
//   --mi-font-ui      界面字体（body 与面板都读它，见 memory-inspector-tokens.css）
//   --font-read       阅读体（墨屿两套的衬线栈，chat-ink.css / ask-ink.css）
//   --style-weight-body  正文字重（400 常规 / 500 加粗）

const DB_NAME = 'docmind-user-fonts'
const DB_VERSION = 1
const STORE = 'fonts'
const PREFS_KEY = 'docmind.userFont'
export const FONTS_CHANGE_EVENT = 'docmind:user-fonts'

/** 界面字体的兜底栈 —— 与 memory-inspector-tokens.css 的 --mi-font-ui 同款。 */
const FALLBACK_UI = "'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif"
/** 阅读体的兜底栈 —— 与「墨屿」两套的衬线栈同款。 */
const FALLBACK_READ = "'Songti SC', 'Source Han Serif SC', 'Noto Serif CJK SC', 'Noto Serif SC', 'STSong', 'SimSun', '宋体', serif"

export interface UserFontMeta {
  id: string
  /** 文件名（去掉扩展名），给用户看。 */
  name: string
  /** 注册进 document.fonts 的 family 名。 */
  family: string
  addedAt: number
  size: number
}

export interface FontPrefs {
  /** 界面字体：'' = 用内置的。 */
  ui: string
  /** 阅读体：'' = 用皮肤自带的那套（墨屿衬线 / 其余无衬线）。 */
  read: string
  /** 正文字重：'normal' | 'bold'。 */
  weight: 'normal' | 'bold'
}

interface StoredFont extends UserFontMeta {
  data: ArrayBuffer
}

const DEFAULT_PREFS: FontPrefs = { ui: '', read: '', weight: 'normal' }

function openDb(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    if (typeof indexedDB === 'undefined') {
      resolve(null)
      return
    }
    let request: IDBOpenDBRequest
    try {
      request = indexedDB.open(DB_NAME, DB_VERSION)
    } catch {
      resolve(null)
      return
    }
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'id' })
    }
    request.onsuccess = () => resolve(request.result)
    // 隐私模式 / 策略禁用时 open 会直接失败：存不下就当没这功能，不能白屏。
    request.onerror = () => resolve(null)
    request.onblocked = () => resolve(null)
  })
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve) => {
    tx.oncomplete = () => resolve()
    tx.onerror = () => resolve()
    tx.onabort = () => resolve()
  })
}

async function readAll(): Promise<StoredFont[]> {
  const db = await openDb()
  if (!db) return []
  try {
    return await new Promise<StoredFont[]>((resolve) => {
      const request = db.transaction(STORE, 'readonly').objectStore(STORE).getAll()
      request.onsuccess = () => resolve((request.result || []) as StoredFont[])
      request.onerror = () => resolve([])
    })
  } finally {
    db.close()
  }
}

async function put(record: StoredFont): Promise<void> {
  const db = await openDb()
  if (!db) return
  try {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(record)
    await txDone(tx)
  } finally {
    db.close()
  }
}

async function remove(id: string): Promise<void> {
  const db = await openDb()
  if (!db) return
  try {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).delete(id)
    await txDone(tx)
  } finally {
    db.close()
  }
}

/** 文件名字 → 可用的 family 名（只留字母数字与连字符，避免 CSS 引号问题）。 */
function safeFamily(name: string): string {
  const slug = name.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  return 'docmind-user-' + (slug || 'font')
}

/** 同一份文件重复导入时不产生第二份。 */
function fileKey(file: File): string {
  return file.name + '|' + file.size + '|' + file.lastModified
}

function alreadyRegistered(family: string): boolean {
  return Array.from(document.fonts || []).some((face) => face.family === family)
}

/** 注册一份字体到 document.fonts。已经注册过就跳过。 */
async function registerFace(family: string, data: ArrayBuffer): Promise<boolean> {
  if (alreadyRegistered(family)) return true
  try {
    const face = new FontFace(family, data)
    await face.load()
    document.fonts.add(face)
    return true
  } catch {
    // 文件损坏 / 浏览器不支持这种格式：不当成致命错误，列表里仍然可见但没生效。
    return false
  }
}

/** 已导入的字体列表（不含字节）。 */
export async function listUserFonts(): Promise<UserFontMeta[]> {
  const all = await readAll()
  return all
    .map(({ id, name, family, addedAt, size }) => ({ id, name, family, addedAt, size }))
    .sort((a, b) => b.addedAt - a.addedAt)
}

/**
 * 导入一个本地字体文件。返回导入后的条目；浏览器不支持该格式时抛错。
 * 只接受 ttf / otf / woff / woff2 —— 这些是浏览器能直接注册的格式。
 */
export async function importFontFile(file: File): Promise<UserFontMeta> {
  const name = (file.name || '').replace(/\.[^.]+$/, '') || '未命名字体'
  const data = await file.arrayBuffer()
  const family = safeFamily(name)
  const ok = await registerFace(family, data)
  if (!ok) throw new Error('这个文件注册失败，可能不是浏览器支持的字体格式（ttf / otf / woff2）。')
  const all = await readAll()
  const same = all.find((item) => item.name === name && item.size === file.size)
  if (same) return same
  const id = 'f' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
  const record: StoredFont = { id, name, family, addedAt: Date.now(), size: file.size, data }
  await put(record)
  emitChange()
  return { id, name, family, addedAt: record.addedAt, size: record.size }
}

/** 删掉一份字体（同时从 document.fonts 里移除）。 */
export async function deleteUserFont(id: string): Promise<void> {
  const all = await readAll()
  const target = all.find((item) => item.id === id)
  await remove(id)
  if (target) {
    // FontFaceSet.delete() 只收 FontFace 对象，不收 family 名字 —— 逐个找出来删。
    for (const face of Array.from(document.fonts || [])) {
      if (face.family === target.family) {
        try {
          document.fonts.delete(face)
        } catch {
          /* 已经不在了就无所谓 */
        }
      }
    }
  }
  const prefs = getFontPrefs()
  if (prefs.ui === target?.family || prefs.read === target?.family) {
    setFontPrefs({ ...prefs, ui: prefs.ui === target?.family ? '' : prefs.ui, read: prefs.read === target?.family ? '' : prefs.read })
  }
  emitChange()
}

/** 读偏好。没存过就是默认（内置字体 + 常规字重）。 */
export function getFontPrefs(): FontPrefs {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY)
    if (!raw) return { ...DEFAULT_PREFS }
    const parsed = JSON.parse(raw) as Partial<FontPrefs>
    return {
      ui: typeof parsed.ui === 'string' ? parsed.ui : '',
      read: typeof parsed.read === 'string' ? parsed.read : '',
      weight: parsed.weight === 'bold' ? 'bold' : 'normal',
    }
  } catch {
    return { ...DEFAULT_PREFS }
  }
}

/**
 * 把偏好落到 :root 的内联自定义属性上。
 * 只改「用户真的选过」的那几项：没选就不写内联值，令牌文件里的原样保留。
 */
export function applyFontPrefs(prefs?: FontPrefs): FontPrefs {
  const next = prefs || getFontPrefs()
  const root = document.documentElement
  const set = (name: string, value: string) => {
    if (value) root.style.setProperty(name, value)
    else root.style.removeProperty(name)
  }
  // 三条字体令牌一起覆盖 —— 它们各自定义在不同的文件里，页面各用一条：
  //   --font-ui        工作台的 body（style.css）与问答页的 body（ask.css）
  //   --mi-font-ui     面板 / 记忆面板那一套（memory-inspector-tokens.css）
  //   --style-font-ui  问答页 html[data-style] body（skins.css 的别名映射）
  // 只改其中一条，另一页或另一类面板就还是旧字体 —— 实测踩过。
  const uiStack = next.ui ? '"' + next.ui + '", ' + FALLBACK_UI : ''
  set('--font-ui', uiStack)
  set('--mi-font-ui', uiStack)
  set('--style-font-ui', uiStack)
  set('--font-read', next.read ? '"' + next.read + '", ' + FALLBACK_READ : '')
  // 字重：问答页的 body 有自己的 font-weight 规则（读 --style-weight-body），
  // 工作台的 body 没有 —— 它从 <html> 继承。所以两条都得写。
  set('--style-weight-body', next.weight === 'bold' ? '500' : '')
  root.style.fontWeight = next.weight === 'bold' ? '500' : ''
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(next))
  } catch {
    /* 存不下就只在本次会话生效 */
  }
  return next
}

/** 存偏好并立刻生效。 */
export function setFontPrefs(prefs: Partial<FontPrefs>): FontPrefs {
  const next = { ...getFontPrefs(), ...prefs }
  applyFontPrefs(next)
  emitChange()
  return next
}

/**
 * 启动时调用：把存下来的字体重新注册（IndexedDB 里存的是字节，页面刷新后
 * document.fonts 是空的），然后把偏好落到 :root。
 * 同步部分（应用偏好）先跑，不等 IndexedDB —— 首帧就已经是对的。
 */
export async function restoreUserFonts(): Promise<UserFontMeta[]> {
  applyFontPrefs()
  const all = await readAll()
  const metas: UserFontMeta[] = []
  for (const item of all) {
    const ok = await registerFace(item.family, item.data)
    if (ok) metas.push({ id: item.id, name: item.name, family: item.family, addedAt: item.addedAt, size: item.size })
  }
  applyFontPrefs()
  return metas
}

function emitChange() {
  window.dispatchEvent(new CustomEvent(FONTS_CHANGE_EVENT))
}

/** 订阅字体偏好 / 列表变化。 */
export function onFontsChange(fn: () => void): () => void {
  const handler = () => fn()
  window.addEventListener(FONTS_CHANGE_EVENT, handler)
  return () => window.removeEventListener(FONTS_CHANGE_EVENT, handler)
}
