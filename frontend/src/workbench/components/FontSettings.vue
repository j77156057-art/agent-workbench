<script setup lang="ts">
// 设置 · 外观 · 字体：导入用户自己下载的字体文件，或把正文字重调粗一档。
//
// 范围：只做「换字体 / 调字重」这一件事，不做字号缩放 —— 字号是排版密度的问题，
// 改字体会连带把行高与断行都动一遍，反而更难收拾。
// 数据流全在 workbench/userFonts.ts：字节进 IndexedDB，偏好进 localStorage，
// 应用方式是写 :root 上的内联自定义属性（--mi-font-ui / --font-read /
// --style-weight-body），不碰任何组件样式。

import { computed, onMounted, ref } from 'vue'
import {
  applyFontPrefs,
  deleteUserFont,
  getFontPrefs,
  importFontFile,
  listUserFonts,
  onFontsChange,
  setFontPrefs,
  type FontPrefs,
  type UserFontMeta,
} from '../userFonts'

const fonts = ref<UserFontMeta[]>([])
const prefs = ref<FontPrefs>(getFontPrefs())
const busy = ref(false)
const message = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const DEFAULT_OPTION = '__default__'

const options = computed(() => [
  { value: DEFAULT_OPTION, label: '默认（应用自带）' },
  ...fonts.value.map((item) => ({ value: item.family, label: item.name })),
])

const currentUi = computed(() => (prefs.value.ui ? prefs.value.ui : DEFAULT_OPTION))
const currentRead = computed(() => (prefs.value.read ? prefs.value.read : DEFAULT_OPTION))

function refresh() {
  prefs.value = getFontPrefs()
}

function choose(kind: 'ui' | 'read', family: string) {
  const value = family === DEFAULT_OPTION ? '' : family
  prefs.value = setFontPrefs({ [kind]: value } as Partial<FontPrefs>)
}

function setWeight(weight: 'normal' | 'bold') {
  prefs.value = setFontPrefs({ weight })
}

async function onPick(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files && input.files[0]
  if (!file) return
  busy.value = true
  message.value = ''
  try {
    const meta = await importFontFile(file)
    message.value = '已导入「' + meta.name + '」，选它即可生效。'
    fonts.value = await listUserFonts()
  } catch (error) {
    message.value = '导入失败：' + (error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
    // 清掉 value，同一个文件再选一次也能触发 change。
    input.value = ''
  }
}

async function removeFont(id: string) {
  await deleteUserFont(id)
  fonts.value = await listUserFonts()
  refresh()
}

onMounted(async () => {
  fonts.value = await listUserFonts()
  refresh()
  onFontsChange(refresh)
})
</script>

<template>
  <div class="fs">
    <div class="fs-head">
      <div>
        <h4 class="fs-title">字体</h4>
        <p class="fs-hint">
          有些字太细、看不清？导入你自己下载的字体文件，或把正文字重调粗一档。
          字体只存在这台机器的浏览器里 —— 不联网、不外传、不进应用包。
        </p>
      </div>
    </div>

    <div class="fs-actions">
      <button type="button" class="fs-btn" :disabled="busy" @click="fileInput?.click()">
        {{ busy ? '导入中…' : '导入字体文件' }}
      </button>
      <input
        ref="fileInput"
        type="file"
        accept=".ttf,.otf,.woff,.woff2"
        class="fs-file"
        @change="onPick"
      />
      <span class="fs-note">支持 ttf / otf / woff / woff2。中文字体文件通常几 MB，属正常。</span>
    </div>

    <p v-if="message" class="fs-message">{{ message }}</p>

    <ul v-if="fonts.length" class="fs-list">
      <li v-for="item in fonts" :key="item.id" class="fs-item">
        <span class="fs-item-name">{{ item.name }}</span>
        <span class="fs-item-size">{{ (item.size / 1024 / 1024).toFixed(1) }} MB</span>
        <button type="button" class="fs-remove" @click="removeFont(item.id)">删除</button>
      </li>
    </ul>
    <p v-else class="fs-empty">还没有导入字体。先用「默认」也完全没问题 —— 只是字重可以调粗一档。</p>

    <label class="fs-row">
      <span class="fs-row-label">界面字体</span>
      <select class="fs-select" :value="currentUi" @change="choose('ui', ($event.target as HTMLSelectElement).value)">
        <option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option>
      </select>
    </label>

    <label class="fs-row">
      <span class="fs-row-label">阅读体</span>
      <select class="fs-select" :value="currentRead" @change="choose('read', ($event.target as HTMLSelectElement).value)">
        <option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option>
      </select>
    </label>

    <div class="fs-row">
      <span class="fs-row-label">正文字重</span>
      <div class="fs-weights">
        <button
          type="button"
          class="fs-weight"
          :class="{ on: prefs.weight === 'normal' }"
          :aria-pressed="prefs.weight === 'normal'"
          @click="setWeight('normal')"
        >常规</button>
        <button
          type="button"
          class="fs-weight"
          :class="{ on: prefs.weight === 'bold' }"
          :aria-pressed="prefs.weight === 'bold'"
          @click="setWeight('bold')"
        >加粗</button>
      </div>
    </div>

    <div class="fs-preview">
      <p class="fs-preview-text">
        预览：这是一段示例文字，用来确认当前字体的粗细与可读性。0123456789 · Workbench
      </p>
    </div>
  </div>
</template>

<style scoped>
.fs { display: flex; flex-direction: column; gap: 12px; }
.fs-head { display: flex; justify-content: space-between; gap: 12px; }
.fs-title { margin: 0 0 4px; font-size: var(--fs-lead); font-weight: 600; color: var(--text); }
.fs-hint { margin: 0; max-width: 62ch; font-size: var(--fs-sm); line-height: 1.65; color: var(--text-muted); }
.fs-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.fs-btn {
  padding: 6px 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  color: var(--text);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.fs-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-text); }
.fs-btn:disabled { opacity: .6; cursor: not-allowed; }
.fs-file { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.fs-note { font-size: var(--fs-cap); color: var(--text-faint); }
.fs-message { margin: 0; font-size: var(--fs-sm); color: var(--accent-text); }
.fs-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.fs-item {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px;
  border-left: 2px solid var(--border);
  background: var(--bg);
}
.fs-item-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); color: var(--text); }
.fs-item-size { flex: none; font-size: var(--fs-cap); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.fs-remove {
  flex: none; padding: 2px 8px; border: 0; border-radius: var(--r-xs);
  background: transparent; color: var(--text-muted); font-size: var(--fs-cap); cursor: pointer;
}
.fs-remove:hover { color: var(--danger); background: var(--bg-hover); }
.fs-empty { margin: 0; font-size: var(--fs-sm); color: var(--text-faint); }
.fs-row { display: flex; align-items: center; gap: 12px; }
.fs-row-label { flex: none; width: 72px; font-size: var(--fs-sm); color: var(--text-muted); }
.fs-select {
  flex: 1; min-width: 0; padding: 6px 8px;
  border: 1px solid var(--border-strong); border-radius: var(--r-sm);
  background: var(--bg-raised); color: var(--text); font-size: var(--fs-sm);
}
.fs-weights { display: inline-flex; gap: 4px; }
.fs-weight {
  padding: 4px 12px;
  border: 1px solid var(--border-strong); border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted); font-size: var(--fs-sm); cursor: pointer;
}
.fs-weight.on { border-color: var(--accent); color: var(--accent-text); background: var(--accent-soft); }
.fs-preview { padding: 10px 12px; border-left: 2px solid var(--border); background: var(--bg); }
.fs-preview-text { margin: 0; font-size: var(--fs-body); line-height: 1.7; color: var(--text); }
</style>
