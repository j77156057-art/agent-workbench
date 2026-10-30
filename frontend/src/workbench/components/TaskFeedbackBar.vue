<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{ busy?: boolean }>()
const emit = defineEmits<{
  (e: 'submit', stars: number): void
  (e: 'dismiss'): void
}>()

const stars = ref(0)
</script>

<template>
  <div class="fb-bar" role="group" aria-label="本次任务满意度">
    <span class="fb-label">这次任务满意吗？</span>
    <div class="fb-stars" role="radiogroup" aria-label="1 到 5 星，5 星为非常满意">
      <button
        v-for="n in 5"
        :key="n"
        type="button"
        role="radio"
        class="fb-star"
        :class="{ on: n <= stars }"
        :aria-label="`${n} 星`"
        :aria-checked="stars === n"
        :disabled="props.busy"
        @click="stars = n"
      >★</button>
    </div>
    <button
      type="button"
      class="fb-btn"
      :disabled="!stars || props.busy"
      @click="emit('submit', stars)"
    >{{ props.busy ? '提交中…' : '确定' }}</button>
    <button type="button" class="fb-skip" :disabled="props.busy" @click="emit('dismiss')">忽略</button>
  </div>
</template>

<style scoped>
.fb-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-top: 1px dashed var(--border);
  background: var(--bg-raised);
  color: var(--text-muted);
  font-size: var(--fs-cap);
  user-select: none;
}
.fb-label { white-space: nowrap; }
.fb-stars { display: inline-flex; gap: 2px; }
.fb-star {
  border: 0;
  background: transparent;
  cursor: pointer;
  padding: 2px 1px;
  font-size: 16px;
  line-height: 1;
  color: var(--text-faint);
  transition: color .15s ease, transform .15s var(--ease-spring);
}
.fb-star:hover:not(:disabled) { color: var(--amber); transform: translateY(-1px); }
.fb-star.on { color: var(--amber); }
.fb-star:disabled { cursor: default; opacity: .6; }
.fb-star:focus-visible,
.fb-btn:focus-visible,
.fb-skip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 4px;
}
.fb-btn {
  margin-left: auto;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text);
  padding: 3px 10px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.fb-btn:disabled { opacity: .45; cursor: default; }
.fb-skip {
  border: 0;
  background: transparent;
  color: var(--text-faint);
  padding: 3px 4px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.fb-skip:hover:not(:disabled) { color: var(--text-muted); }
.fb-skip:disabled { cursor: default; opacity: .6; }
</style>
