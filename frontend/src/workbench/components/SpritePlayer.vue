<template>
  <div class="sp-wrap">
    <canvas ref="canvas" class="sp-canvas" :width="frameW" :height="frameH" />
    <p v-if="error" class="sp-err">动画帧加载失败</p>
  </div>
</template>

<script setup lang="ts">
/** SpriteSheet 逐帧播放器：按 fps 循环裁剪图集网格绘制到 canvas。 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  src: string
  cols: number
  rows: number
  frameW: number
  frameH: number
  fps?: number
  /** 实际有效帧数；图集末尾可能有未填充的空格，不能按 cols*rows 播放 */
  frames?: number
}>(), { fps: 12, frames: 0 })

function totalFrames(): number {
  return props.frames > 0 ? props.frames : props.cols * props.rows
}

const canvas = ref<HTMLCanvasElement | null>(null)
const error = ref(false)
let img: HTMLImageElement | null = null
let raf = 0
let start = 0

function frameIndex(t: number): number {
  return Math.floor((t - start) / 1000 * props.fps) % totalFrames()
}

function draw(now: number) {
  const cv = canvas.value
  if (cv && img && img.complete && img.naturalWidth > 0) {
    const ctx = cv.getContext('2d')
    if (ctx) {
      const k = frameIndex(now)
      const col = k % props.cols
      const row = Math.floor(k / props.cols)
      ctx.clearRect(0, 0, props.frameW, props.frameH)
      ctx.imageSmoothingEnabled = false
      ctx.drawImage(img, col * props.frameW, row * props.frameH,
        props.frameW, props.frameH, 0, 0, props.frameW, props.frameH)
    }
  }
  raf = requestAnimationFrame(draw)
}

function load() {
  cancelAnimationFrame(raf)
  error.value = false
  img = new Image()
  img.onload = () => { start = performance.now(); raf = requestAnimationFrame(draw) }
  img.onerror = () => { error.value = true }
  img.src = props.src
}

onMounted(load)
watch(() => props.src, load)
onBeforeUnmount(() => cancelAnimationFrame(raf))
</script>

<style scoped>
.sp-wrap { display: inline-flex; flex-direction: column; align-items: center; gap: 6px; padding: 5px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); }
.sp-canvas {
  max-width: 100%;
  height: auto;
  image-rendering: pixelated;
  border: 1px solid var(--border-strong);
  background: #eef1f5;
  border-radius: 4px;
  box-shadow: 0 2px 8px rgba(35,52,84,.08);
}
.sp-err { font-size: var(--fs-cap); color: var(--danger); margin: 0; padding: 4px 7px; border-radius: 5px; background: color-mix(in srgb, var(--danger) 8%, transparent); }
</style>
