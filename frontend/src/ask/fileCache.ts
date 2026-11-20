// 源码弹窗的短时内容缓存：同一个文件被答案里的多条引用连续打开时，不再整份重读。
// 必须放在独立模块 —— `<script setup>` 的顶层代码会被编译进 setup()，
// 组件卸载（关弹窗）就跟着失效，起不到跨次打开复用的作用。
// 命中前由调用方用 /api/fs/exists 的 mtime 校验，文件被改过就重读；只留最近 6 个。
export interface CachedFile {
  mtime: number
  content: string
}

const CACHE_MAX = 6
const cache = new Map<string, CachedFile>()

export function getCachedFile(path: string): CachedFile | null {
  return cache.get(path) || null
}

export function rememberFile(path: string, mtime: number, content: string): void {
  cache.delete(path)
  cache.set(path, { mtime, content })
  while (cache.size > CACHE_MAX) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
  }
}
