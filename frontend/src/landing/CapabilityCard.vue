<script setup lang="ts">
// 能力卡片：8 个分区能力共用一个组件，靠 v-for + 数据驱动渲染。
// rc = 分区强调色（theme.ts 同源），icon = 内联 SVG path（v-html 注入 <svg>）。
defineProps<{
  rc: string
  icon: string
  title: string
  desc: string
  index: number
}>()
</script>

<template>
  <article class="cap reveal" :class="'d' + (index % 4)" :style="{ '--rc': rc }">
    <div class="ico">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" v-html="icon"></svg>
    </div>
    <h3>{{ title }}</h3>
    <p>{{ desc }}</p>
  </article>
</template>
