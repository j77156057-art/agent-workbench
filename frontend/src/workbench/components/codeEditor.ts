// CodeMirror 6 运行时工厂：所有 CM 依赖集中在本模块，由 CodeView 在首次打开文件时
// 动态 import。工作台概览首屏不加载 650KB 的 vendor-codemirror。
// CodeView 仍保留 EditorView/EditorState 的「类型」引用（类型编译后擦除，无运行时成本）。
import { basicSetup } from 'codemirror'
import { Compartment, EditorState } from '@codemirror/state'
import { EditorView, keymap, type ViewUpdate } from '@codemirror/view'
import { python } from '@codemirror/lang-python'
import { javascript } from '@codemirror/lang-javascript'
import { json } from '@codemirror/lang-json'
import { html } from '@codemirror/lang-html'
import { css } from '@codemirror/lang-css'
import { markdown } from '@codemirror/lang-markdown'
import { syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language'
import { oneDarkHighlightStyle } from '@codemirror/theme-one-dark'
import type { Ref } from 'vue'
import type { EditorTab } from '../composables/workbench-types'

// 编辑器主题：**跟随皮肤**（2026-10-20 改）。
//
// 之前这里是一套写死的浅色主题，注释里写着"故意不跟随皮肤"，理由是
// 「主题固化在 EditorState 里，切皮肤换不掉」—— 这个理由只对**语法高亮表**成立
// （高亮表是 JS 对象，不是 CSS）。但**主体配色**（底色/字色/光标/选区/gutter）
// 走的是 EditorView.theme 注入的普通 CSS 规则，里面写 var(--bg-raised) 之类的
// 变量，浏览器会自动跟着 :root 重算 —— 根本不需要重建 EditorState。
//
// 所以现在拆成两半：
//   ① 主体配色 → 全部读令牌，随皮肤自动跟随；
//   ② 语法高亮表 → 是 JS 对象，浅底用默认高亮、深底用 oneDark 高亮，
//      用一个 Compartment 在切肤时 reconfigure。
// 明暗判定读 skins.css 的 --skin-dark 令牌（新增深色皮肤只改那一处）。
const MONO_FONT = "'Cascadia Code','JetBrains Mono',Consolas,monospace"

/** 当前皮肤是否深色（读 skins.css 的 --skin-dark 令牌）。 */
export function isDarkSkin(): boolean {
  if (typeof document === 'undefined') return false
  const v = getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim()
  return v === '1'
}

/** 主题 + 高亮表放在同一个 Compartment：切肤时一起换。 */
const themeCompartment = new Compartment()

function editorTheme(dark: boolean) {
  return [
    EditorView.theme({
      '&': {
        height: '100%',
        fontSize: '12.5px',
        backgroundColor: 'var(--bg-raised)',
        color: 'var(--text)',
      },
      '.cm-scroller': { fontFamily: MONO_FONT },
      '.cm-content': { caretColor: 'var(--accent)' },
      '&.cm-focused .cm-cursor': { borderLeftColor: 'var(--accent)' },
      '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection':
        { backgroundColor: 'color-mix(in srgb, var(--accent) 16%, transparent)' },
      '.cm-gutters': {
        backgroundColor: 'var(--bg)',
        color: 'var(--text-muted)',
        borderRight: '1px solid var(--border)',
      },
      '.cm-activeLine': { backgroundColor: 'color-mix(in srgb, var(--accent) 5%, transparent)' },
      '.cm-activeLineGutter': {
        backgroundColor: 'color-mix(in srgb, var(--accent) 12%, var(--bg-raised))',
        color: 'var(--accent-text)',
      },
      '.cm-foldPlaceholder': {
        backgroundColor: 'var(--bg-hover)',
        border: '1px solid var(--border)',
        color: 'var(--text-muted)',
      },
    }),
    // basicSetup 自带 defaultHighlightStyle；这里再挂一个同 facet 的高亮表会**覆盖**它，
    // 所以浅底传 defaultHighlightStyle（等价）、深底传 oneDarkHighlightStyle。
    syntaxHighlighting(dark ? oneDarkHighlightStyle : defaultHighlightStyle),
  ]
}

function langExtension(lang: string) {
  switch (lang) {
    case 'python': return python()
    case 'javascript': return javascript()
    case 'jsx': return javascript({ jsx: true })
    case 'typescript': return javascript({ typescript: true })
    case 'tsx': return javascript({ jsx: true, typescript: true })
    case 'json': return json()
    case 'html': return html()
    case 'css': return css()
    case 'markdown': return markdown()
    // gdscript / ini(tscn/tres/cfg) / yaml / toml / csv 等无专用语言包，按纯文本渲染
    default: return []
  }
}

export interface EditorStateHooks {
  saveActive: () => void
  tabs: Ref<EditorTab[]>
  /** 每次事务后把新 EditorState 回写到 CodeView 的 Map（含纯选区变化）。 */
  commitState: (tab: EditorTab, state: EditorState) => void
  publishSelection: (view: EditorView, tab: EditorTab) => void
}

/** CRLF 归一化比较（Windows autocrlf 仓库下 CM 统一存 \n，直接比会误判 dirty）。 */
function isDocDirty(docText: string, saved: string): boolean {
  const norm = (s: string) => s.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  return norm(docText) !== norm(saved)
}

export function buildEditorState(tab: EditorTab, hooks: EditorStateHooks): EditorState {
  // 每个 state 一个独立 Compartment（当前无动态切换只读，仅作唯一占位）
  const readOnlyComp = new Compartment()
  return EditorState.create({
    doc: tab.draftContent ?? tab.savedContent,
    extensions: [
      basicSetup,
      langExtension(tab.lang),
      themeCompartment.of(editorTheme(isDarkSkin())),
      readOnlyComp.of(EditorState.readOnly.of(!tab.writable)),
      keymap.of([{
        key: 'Mod-s',
        preventDefault: true,
        run: () => {
          hooks.saveActive()
          return true
        },
      }]),
      EditorView.updateListener.of((u: ViewUpdate) => {
        // CM6 state 不可变：dispatch 后是全新 state 对象，必须回写 Map，
        // 否则非活动标签/保存时 contentGetter 取到的还是旧引用。
        hooks.commitState(tab, u.state)
        // P2：选区或文档变化时刷新选区快照（文档替换后变光标则隐藏浮条）
        hooks.publishSelection(u.view, tab)
        if (!u.docChanged) return
        const t = hooks.tabs.value.find((x) => x.id === tab.id)
        if (t) {
          t.draftContent = u.state.doc.toString()
          t.dirty = isDocDirty(t.draftContent, t.savedContent)
        }
      }),
    ],
  })
}

export function createEditorView(host: HTMLElement): EditorView {
  return new EditorView({ parent: host, extensions: [themeCompartment.of(editorTheme(isDarkSkin()))] })
}

/**
 * 切肤后把某个 EditorState 的主题换掉（2026-10-20）。
 * 主体配色是 CSS 变量，会自动跟随；这里要换的是**语法高亮表** —— 它是 JS 对象，
 * 必须重新 configure 一次。走 Compartment 即可，不需要重建 EditorState
 * （重建会丢选区与滚动位置）。
 *
 * 注意：工作台每个标签各有一个 EditorState（CodeView 的 states Map），
 * 而 Compartment 的 reconfigure 只作用于**被 dispatch 的那个 state**。
 * 所以切肤时要对所有 state 各做一次，否则切到别的标签会露出旧皮肤的高亮表。
 */
export function rethemeState(st: EditorState): EditorState {
  return st.update({ effects: themeCompartment.reconfigure(editorTheme(isDarkSkin())) }).state
}
