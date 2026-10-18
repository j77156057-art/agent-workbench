// 极简 markdown 渲染（选区 AI 面板与底部 AI 对话台共用）：
// 仅覆盖答案里实际会出现的结构——围栏代码、标题、列表、段落/换行；输入先转义。
// 同时提供答案内文件引用提取（path:line / 第 N 行），用于渲染可点击跳转卡片。

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

export function inlineHtml(s: string): string {
  let t = escapeHtml(s)
  t = t.replace(/(https?:\/\/[^\s<]+)/g, '<a class="md-link" href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
  t = t.replace(/`([^`\n]+?)`/g, (_m, c) => `<code class="md-ic">${c}</code>`)
  t = t.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
  return t
}

export interface WebRef { url: string; title: string; score?: string }

/** 从联网工具输出中提取来源卡片，避免把长 URL 淹没在正文里。 */
export function extractWebRefs(text: string): WebRef[] {
  const lines = (text || '').split(/\r?\n/)
  const out: WebRef[] = []; const seen = new Set<string>(); let title = ''
  for (const line of lines) {
    const bullet = /^\s*[·•-]\s+(.+)$/.exec(line)
    if (bullet && !/^https?:\/\//.test(bullet[1])) { title = bullet[1].trim(); continue }
    const url = /(https?:\/\/[^\s)<>]+)/.exec(line)?.[1]?.replace(/[.,，。]+$/, '')
    if (!url || seen.has(url)) continue
    seen.add(url)
    const score = /可信度参考：([0-9.]+)/.exec(line)?.[1]
    out.push({ url, title: title || url, score })
    title = ''
  }
  return out.slice(0, 8)
}

export function mdToHtml(md: string): string {
  const blocks: string[] = []
  const codeStore: string[] = []
  const rest = md.replace(/```[A-Za-z0-9_+\-.]*\n?([\s\S]*?)```/g, (_m, code: string) => {
    const i = codeStore.length
    codeStore.push(code.replace(/\n$/, ''))
    return `\0CODEBLOCK${i}\0`
  })

  const lines = rest.replace(/\r\n/g, '\n').split('\n')
  let html = ''
  let listOpen = false
  const closeList = () => { if (listOpen) { html += '</ul>'; listOpen = false } }

  for (const rawLine of lines) {
    const line = rawLine
    const cb = /^\s*CODEBLOCK(\d+)\s*$/.exec(line.trim())
    if (cb) {
      closeList()
      html += `<pre class="md-pre"><code>${escapeHtml(codeStore[Number(cb[1])] || '')}</code></pre>`
      continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      closeList()
      html += `<div class="md-h md-h${h[1].length}">${inlineHtml(h[2])}</div>`
      continue
    }
    // 有序列表 1. / 无序列表 - *
    const oli = /^\s*\d+[.、]\s+(.*)$/.exec(line)
    const li = /^\s*[-*]\s+(.*)$/.exec(line)
    if (oli || li) {
      if (!listOpen) { html += '<ul class="md-ul">'; listOpen = true }
      html += `<li>${inlineHtml((oli || li)![1])}</li>`
      continue
    }
    if (/^\s*$/.test(line)) {
      closeList()
      continue
    }
    closeList()
    html += `<p class="md-p">${inlineHtml(line)}</p>`
  }
  closeList()
  html = html.replace(/\s*CODEBLOCK(\d+)\s*/g, (_m, i) =>
    `<pre class="md-pre"><code>${escapeHtml(codeStore[Number(i)] || '')}</code></pre>`)
  blocks.push(html)
  return blocks.join('')
}

// ---------------------------------------------------------------- 文件引用
export interface FileRef {
  path: string
  line: number
}

const CODE_EXT =
  'gd|gdshader|tscn|tres|godot|cfg|toml|py|cs|ts|js|vue|json|java|kt|cpp|h|hpp|c|go|rs|shader|uxml|uss|yaml|yml|md|shader|lua|rb'
const REF_TOKEN = new RegExp(
  `(?:res://)?((?:[A-Za-z0-9_.\\-]+/)*[A-Za-z0-9_.\\-]+\\.(?:${CODE_EXT})(?![A-Za-z0-9_]))`,
  'g',
)

/** 从答案文本中提取文件引用，兼容 `a/b.gd:12`、`a/b.gd 第 12 行`、res:// 前缀，按 path+line 去重。 */
export function extractFileRefs(text: string): FileRef[] {
  const stripped = (text || '').replace(/https?:\/\/\S+/g, ' ')
  const out: FileRef[] = []
  const seen = new Set<string>()
  let m: RegExpExecArray | null
  REF_TOKEN.lastIndex = 0
  while ((m = REF_TOKEN.exec(stripped)) !== null) {
    const path = m[1]
    if (/^(v|pr|issue)\d/i.test(path)) continue
    const after = stripped.slice(m.index + m[0].length, m.index + m[0].length + 24)
    let line = 0
    const colon = /^\s*[:：]\s*(\d{1,5})/.exec(after)
    const cnLine = /^\s*第\s*(\d{1,5})\s*行/.exec(after)
    if (colon) line = Number(colon[1])
    else if (cnLine) line = Number(cnLine[1])
    const key = `${path}:${line}`
    if (!seen.has(key)) {
      seen.add(key)
      out.push({ path, line })
    }
  }
  return out
}
