// 皮肤（视觉风格）切换 —— 纯展示层，存 localStorage，不落后端。
//
// 为什么单独一个模块：工作台与问答页是两个独立入口（workbench.html / index.html），
// 但**皮肤必须是同一个**——用户在设置里选了「石墨夜航」，切到问答页也得是同一套，
// 否则两页风格分裂。单一 localStorage key + 启动时应用到 <html data-skin>，
// 天然跨入口一致。
//
// 关键实现细节：
//  ① 皮肤写在 <html> 的 data-skin 上，skins.css 用 html[data-skin='x'] 覆写 :root 令牌，
//     所以「切皮肤」= 改一个 dataset 值，浏览器立即重算全部 var()，无需刷新。
//  ② 必须在应用挂载**之前**应用（main.ts 里 createApp 之前），否则首帧会闪一下默认皮。
//     这里导出 applyStoredSkin() 供两个 main.ts 同步调用。
//  ③ 同一标签页内两个文档（工作台弹 pywebview 窗口、问答页）之间不触发 storage 事件，
//     所以额外派发 SKIN_CHANGE_EVENT，监听方直接改自己的 dataset。
//  ④ localStorage 在隐私模式 / 策略禁用时直接抛，读写一律吞异常回退默认，
//     绝不能让设置页或主界面因为存不下而白屏。

// 2026-10-07 收敛：8 套 × 5 种造型的组合矩阵（40+ 种，谁也调不过来）砍到
// 两套精调皮肤 × 1 种造型（brutal）。被砍皮肤的 localStorage 残值由
// isSkinKey 校验拦下，自动回默认 paper，不需要迁移。
export type SkinKey = 'paper' | 'graphite'

export interface SkinDef {
  key: SkinKey
  name: string
  desc: string
  /** 卡片色板预览用的四个取样色：纸底 / 描边 / 强调 / 强调块上的字 */
  swatch: [string, string, string, string]
}

const KEY = 'docmind.skin'
export const SKIN_CHANGE_EVENT = 'docmind:skin'

export const SKINS: SkinDef[] = [
  { key: 'paper', name: '素纸朱红', desc: '暖骨白纸 + 朱红，零圆角 + 硬阴影。印刷感最强', swatch: ['#ece7db', '#14110f', '#ff674d', '#000000'] },
  { key: 'graphite', name: '石墨夜航', desc: '深炭底 + 电光青，硬边 + 近黑阴影。夜间低亮环境', swatch: ['#16181d', '#e8e6e0', '#4dd0e1', '#000000'] },
]

export const DEFAULT_SKIN: SkinKey = 'paper'

const VALID = new Set<string>(SKINS.map((s) => s.key))

export function isSkinKey(v: unknown): v is SkinKey {
  return typeof v === 'string' && VALID.has(v)
}

/** 读当前皮肤；读不到/被禁用/值非法一律回默认（不抛） */
export function loadSkin(): SkinKey {
  try {
    const raw = window.localStorage.getItem(KEY)
    return isSkinKey(raw) ? raw : DEFAULT_SKIN
  } catch {
    return DEFAULT_SKIN
  }
}

export function saveSkin(key: SkinKey): void {
  try {
    window.localStorage.setItem(KEY, key)
  } catch {
    /* 存不下就只在本次会话生效，不打断切换 */
  }
}

/** 把皮肤写到 <html data-skin>；非法 key 一律回落默认，不留"半失效"状态 */
export function applySkin(key: SkinKey): void {
  const k = isSkinKey(key) ? key : DEFAULT_SKIN
  document.documentElement.setAttribute('data-skin', k)
  // 同步 color-scheme：深色皮肤让原生滚动条/表单控件也跟着走暗色
  document.documentElement.style.colorScheme = k === 'graphite' ? 'dark' : 'light'
}

/**
 * 启动时调用：读存储 → 写 <html>。必须在 createApp().mount() 之前，
 * 否则首帧会用默认皮渲染再切，用户看到一次闪白。
 */
export function applyStoredSkin(): SkinKey {
  const k = loadSkin()
  applySkin(k)
  return k
}

/**
 * 切换皮肤：写存储 + 应用 + 广播。
 * 返回实际生效的 key（非法值会被夹到默认）。
 */
export function setSkin(key: SkinKey): SkinKey {
  const k = isSkinKey(key) ? key : DEFAULT_SKIN
  saveSkin(k)
  applySkin(k)
  window.dispatchEvent(new CustomEvent(SKIN_CHANGE_EVENT, { detail: k }))
  return k
}

/** 订阅皮肤变化，返回取消订阅函数（Vue 的 onUnmounted 里调用） */
export function onSkinChange(fn: (key: SkinKey) => void): () => void {
  const handler = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (isSkinKey(d)) fn(d)
  }
  window.addEventListener(SKIN_CHANGE_EVENT, handler)
  // 另一个文档（如问答页）改了皮肤，storage 事件在本标签页也会触发
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY && isSkinKey(e.newValue)) {
      applySkin(e.newValue as SkinKey)
      fn(e.newValue as SkinKey)
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(SKIN_CHANGE_EVENT, handler)
    window.removeEventListener('storage', onStorage)
  }
}
