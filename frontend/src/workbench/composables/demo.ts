// 离线演示模式：页面打开时探测同源后端（/api/health，纯存活探针、不依赖 Ollama）。
// 示例数据仅在显式 ?demo=1 时启用，断连不能冒充项目数据。
import { ref } from 'vue'
// P4 收口：开发台内所有 /api/* 请求统一带上当前项目头（withProject）。健康探测本身与项目
// 无关（后端 /api/health 不读项目上下文），但为一致性也走同一注入；未选项目时不带头（生命线）。
import type { AssetItem, FlowDef, BugItem, ChangesetItem } from '../api'
import { withProject } from '../api'

export const demoMode = ref(false)
export const demoProbed = ref(false)
let probing: Promise<boolean> | null = null

export function probeBackend(timeoutMs = 4000): Promise<boolean> {
  if (probing) return probing
  probing = (async () => {
    if (new URLSearchParams(location.search).get('demo') === '1') {
      demoMode.value = true
      demoProbed.value = true
      return false
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), timeoutMs)
    try {
      const res = await fetch('/api/health', withProject({ signal: controller.signal }))
      return res.ok
    } catch {
      return false
    } finally {
      window.clearTimeout(timer)
      demoProbed.value = true
    }
  })()
  return probing
}

// ---------------------------------------------------------------- 演示数据
// 字段与真实端点对齐（/api/budget、/api/sessions、/api/trace、/api/skills、/api/hooks）。

export const demoBudget = {
  status: {
    global_limit: 5,
    global_spent: 0,
    day: new Date().toISOString().slice(0, 10),
    day_spent: 0,
    per_minute_calls_limit: 30,
    per_minute_cost_limit: 1,
    minute_calls: 0,
    minute_cost: 0,
    sessions: {} as Record<string, { spent: number; limit: number }>,
    pricing_file: '（演示模式）',
  },
  check: {
    ok: true, reason: '', global_limit: 5, global_spent: 0,
    session_limit: 0, session_spent: 0, day_spent: 0,
    minute_calls: 0, minute_cost: 0,
    per_minute_calls_limit: 30, per_minute_cost_limit: 1,
  },
}

export const demoSessions = [
  { session_id: 'web-default', turns: 12, has_summary: true, updated_at: new Date(Date.now() - 1000 * 60 * 8).toISOString() },
  { session_id: '玩家数值排查', turns: 5, has_summary: false, updated_at: new Date(Date.now() - 1000 * 60 * 47).toISOString() },
  { session_id: '新手引导对话', turns: 3, has_summary: false, updated_at: new Date(Date.now() - 1000 * 60 * 60 * 3).toISOString() },
]

export const demoTraceSummary = {
  turns: 42, prompt_tokens: 38650, completion_tokens: 9420, total_tokens: 48070,
  avg_elapsed_ms: 6300, aborted: 0, errors: 1,
  by_provider: { ollama: { turns: 42, tokens: 48070 } },
}

export const demoTraceItems = [
  {
    turn_id: 'demo-001', ts: new Date(Date.now() - 1000 * 60 * 6).toISOString(),
    session_id: 'web-default', provider: 'ollama', model: 'qwen2.5:7b', route: 'local',
    question_chars: 18, messages_count: 6, prompt_tokens: 1284, completion_tokens: 386,
    total_tokens: 1670, cost_cny: 0, llm_calls: 2, llm_ms: 5200, elapsed_ms: 6100,
    outcome: 'completed', aborted: false, error: '',
    steps: [
      { i: 0, action: 'search_code', latency_ms: 420, obs_chars: 312, ok: true },
      { i: 1, action: 'read_file', latency_ms: 90, obs_chars: 1024, ok: true },
    ],
  },
  {
    turn_id: 'demo-002', ts: new Date(Date.now() - 1000 * 60 * 14).toISOString(),
    session_id: '玩家数值排查', provider: 'ollama', model: 'qwen3:14b', route: 'local',
    question_chars: 23, messages_count: 4, prompt_tokens: 980, completion_tokens: 245,
    total_tokens: 1225, cost_cny: 0, llm_calls: 1, llm_ms: 3900, elapsed_ms: 4300,
    outcome: 'completed', aborted: false, error: '',
    steps: [
      { i: 0, action: 'grep', latency_ms: 120, obs_chars: 488, ok: true },
    ],
  },
  {
    turn_id: 'demo-003', ts: new Date(Date.now() - 1000 * 60 * 26).toISOString(),
    session_id: '新手引导对话', provider: 'ollama', model: 'qwen2.5:7b', route: 'local',
    question_chars: 12, messages_count: 2, prompt_tokens: 410, completion_tokens: 0,
    total_tokens: 410, cost_cny: 0, llm_calls: 1, llm_ms: 0, elapsed_ms: 800,
    outcome: 'aborted', aborted: true, error: '用户手动停止',
    steps: [],
  },
  {
    turn_id: 'demo-004', ts: new Date(Date.now() - 1000 * 60 * 3).toISOString(),
    session_id: '敌人受击修复', provider: 'ollama', model: 'qwen3:14b', route: 'local',
    question_chars: 31, messages_count: 12, prompt_tokens: 6842, completion_tokens: 1530,
    total_tokens: 8372, cost_cny: 0, llm_calls: 7, llm_ms: 21400, elapsed_ms: 24800,
    outcome: 'completed', aborted: false, error: '',
    steps: [
      { i: 0, action: 'search_code', latency_ms: 380, arg_chars: 18, obs_chars: 940, ok: true },
      { i: 1, action: 'read_file', latency_ms: 70, arg_chars: 24, obs_chars: 2140, ok: true },
      { i: 2, action: 'dev_region_edit', latency_ms: 310, arg_chars: 420, obs_chars: 180, ok: true },
      { i: 3, action: 'dev_region_verify', latency_ms: 860, arg_chars: 12, obs_chars: 522, ok: false },
      { i: 4, action: 'read_file', latency_ms: 65, arg_chars: 22, obs_chars: 1680, ok: true },
      { i: 5, action: 'dev_region_edit', latency_ms: 280, arg_chars: 365, obs_chars: 196, ok: true },
      { i: 6, action: 'game_playtest', latency_ms: 5200, arg_chars: 40, obs_chars: 760, ok: true },
    ],
  },
  {
    turn_id: 'demo-005', ts: new Date(Date.now() - 1000 * 60 * 1).toISOString(),
    session_id: 'web-default', provider: 'deepseek', model: 'deepseek-chat', route: 'cloud',
    question_chars: 26, messages_count: 8, prompt_tokens: 3210, completion_tokens: 920,
    total_tokens: 4130, cost_cny: 0.0082, llm_calls: 3, llm_ms: 9600, elapsed_ms: 15200,
    outcome: 'completed', aborted: false, error: '',
    steps: [
      { i: 0, action: 'web_research', latency_ms: 4200, arg_chars: 20, obs_chars: 3800, ok: true },
      { i: 1, action: 'web_fetch', latency_ms: 1600, arg_chars: 64, obs_chars: 5200, ok: true },
    ],
  },
]

export const demoSkills = {
  skills_dir: '（演示模式）.docmind/skills', exists: true, count: 2, errors: [] as string[],
  items: [
    { name: '引擎项目初始化', description: '检测本机 Godot/Unity/Unreal 安装并生成启动配置', when_to_use: '第一次接入某个游戏引擎时', path: 'engine-project-setup/SKILL.md' },
    { name: '冻结发布', description: '按标准清单完成测试、构建、打包与记录', when_to_use: '需要发布桌面新版本时', path: 'docmind-frozen-release/SKILL.md' },
  ],
}

export const demoHooks = {
  hooks_dir: '（演示模式）.docmind/hooks', exists: false,
  counts: { pre_tool: 0, post_tool: 0, pre_turn: 0, post_turn: 0 },
  sources: {} as Record<string, unknown>, errors: [] as string[],
}

// ---------------------------------------------------------------- 阶段 1：语义定位 / 分区卡片演示数据
export const demoLocateFiles = [
  {
    path: 'scripts/player/player_stats.gd', name: 'player_stats.gd', score: 12.5,
    reasons: ['业务标签命中', '语义向量检索'], line: 14, symbol: 'take_damage',
    region: 'values', region_name: '数值区',
    tags: ['玩家属性', '生命与战斗数值'], summary: '管理玩家生命值、攻击力与受伤结算',
  },
  {
    path: 'scripts/combat/damage_calc.gd', name: 'damage_calc.gd', score: 10.2,
    reasons: ['语义向量检索'], line: 31, symbol: 'calc_final_damage',
    region: 'values', region_name: '数值区',
    tags: ['伤害计算', '战斗系统'], summary: '攻防换算、暴击与减伤公式',
  },
  {
    path: 'scripts/player/player_controller.gd', name: 'player_controller.gd', score: 6.4,
    reasons: ['文件名命中'], line: 8, symbol: '',
    region: 'behaviors', region_name: '角色行为区',
    tags: ['玩家角色', '输入控制'], summary: '玩家移动、跳跃与输入响应',
  },
]

export const demoLocateRegions = [
  { key: 'values', name: '数值区', dir: 'values', desc: '玩家与敌人的数值配置' },
]

export const demoTagMap = {
  'scripts/player/player_stats.gd': {
    mtime: 0, size: 0, tags: ['玩家属性', '生命与战斗数值'], summary: '管理玩家生命值',
    symbols: ['take_damage', 'heal'], origin: 'llm',
  },
  'scripts/player/player_controller.gd': {
    mtime: 0, size: 0, tags: ['玩家角色', '输入控制'], summary: '玩家移动与输入',
    symbols: ['_physics_process'], origin: 'rules',
  },
  'scripts/inventory_system.gd': {
    mtime: 0, size: 0, tags: ['背包道具'], summary: '道具拾取与背包管理',
    symbols: ['add_item'], origin: 'rules',
  },
  'scripts/combat/damage_calc.gd': {
    mtime: 0, size: 0, tags: ['伤害计算', '战斗系统'], summary: '伤害公式',
    symbols: ['calc_final_damage'], origin: 'llm',
  },
  'scripts/enemy/enemy_ai.gd': {
    mtime: 0, size: 0, tags: ['敌人AI'], summary: '巡逻与追击状态机',
    symbols: ['patrol', 'chase_target'], origin: 'rules',
  },
} as Record<string, {
  mtime: number; size: number; tags: string[]; summary: string;
  symbols: string[]; origin: string;
}>

// ---------------------------------------------------------------- 阶段 2：素材中心演示数据
/** 生成内联 SVG 缩略图（离线演示无网络，占位图也要能直接显示）。 */
function svgThumb(inner: string, bg = '#eef2f8'): string {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="336" height="220" viewBox="0 0 336 220">` +
    `<rect width="336" height="220" fill="${bg}"/>${inner}</svg>`
  return 'data:image/svg+xml,' + encodeURIComponent(svg)
}

const thumbCube = svgThumb(
  '<path d="M168 52 L238 88 L238 158 L168 194 L98 158 L98 88 Z M168 52 L168 122 M98 88 L168 122 L238 88 M168 122 L168 194" '
  + 'fill="none" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round"/>')
const thumbTree = svgThumb(
  '<path d="M168 40 L214 118 H122 Z M168 84 L226 168 H110 Z" fill="color-mix(in srgb, var(--accent) 18%, transparent)" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round"/>'
  + '<rect x="160" y="166" width="16" height="26" fill="#8a6d4b"/>')
const thumbWood = svgThumb(
  '<g stroke="#b98a57" stroke-width="3">' + [58, 88, 118, 148, 178].map((y) =>
    `<path d="M40 ${y} Q120 ${y - 12} 200 ${y + 4} T296 ${y - 2}" fill="none"/>`).join('') + '</g>', '#f3ead9')
const thumbGrid = svgThumb(
  '<g stroke="color-mix(in srgb, var(--accent) 55%, transparent)" stroke-width="2">' +
  [108, 168, 228].map((x) => `<line x1="${x}" y1="40" x2="${x}" y2="180"/>`).join('') +
  [70, 110, 150].map((y) => `<line x1="78" y1="${y}" x2="258" y2="${y}"/>`).join('') + '</g>')
const thumbHdri = svgThumb(
  '<circle cx="168" cy="110" r="62" fill="none" stroke="var(--accent)" stroke-width="3"/>'
  + '<circle cx="168" cy="110" r="20" fill="color-mix(in srgb, var(--accent) 25%, transparent)"/>'
  + '<g stroke="var(--accent)" stroke-width="2"><line x1="168" y1="30" x2="168" y2="48"/><line x1="168" y1="172" x2="168" y2="190"/>'
  + '<line x1="88" y1="110" x2="106" y2="110"/><line x1="230" y1="110" x2="248" y2="110"/></g>')
const thumbSunset = svgThumb(
  '<circle cx="168" cy="128" r="40" fill="rgba(214,137,52,.55)" stroke="#c97a2b" stroke-width="3"/>'
  + '<line x1="40" y1="150" x2="296" y2="150" stroke="#8a6d4b" stroke-width="3"/>',
  '#f6e7d3')
const thumbUi = svgThumb(
  '<rect x="78" y="78" width="180" height="64" rx="8" fill="#fff" stroke="var(--accent)" stroke-width="3"/>'
  + '<rect x="96" y="98" width="80" height="10" rx="5" fill="color-mix(in srgb, var(--accent) 45%, transparent)"/>'
  + '<rect x="96" y="118" width="50" height="10" rx="5" fill="color-mix(in srgb, var(--accent) 25%, transparent)"/>'
  + '<rect x="196" y="106" width="46" height="22" rx="6" fill="var(--accent)"/>')
const thumbAudio = svgThumb(
  '<g stroke="var(--accent)" stroke-width="4" stroke-linecap="round">' +
  [86, 110, 134, 158, 182, 206, 230].map((x, i) =>
    `<line x1="${x}" y1="${110 - 28 - (i % 3) * 12}" x2="${x}" y2="${110 + 28 + ((i + 1) % 3) * 12}"/>`).join('') + '</g>')

export const demoAssetResults: AssetItem[] = [
  { id: 'vintage_armchair', source: 'polyhaven', kind: 'model', name: '复古扶手椅',
    author: 'Kirill Sannikov', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbCube, tags: ['家具', '室内'], summary: '带布艺坐垫的木质扶手椅，室内场景道具' },
  { id: 'lowpoly_oak', source: 'polyhaven', kind: 'model', name: '低多边形橡树',
    author: 'Dairon Sanchez', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbTree, tags: ['自然', '植物'], summary: '风格化低多边形树木，可直接放进关卡' },
  { id: 'wood_planks_01', source: 'polyhaven', kind: 'texture', name: '旧木板纹理',
    author: 'Poly Haven', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbWood, tags: ['木材', 'PBR'], summary: '无缝拼接旧木板 PBR 贴图（颜色/法线/粗糙度）' },
  { id: 'prototype_grid', source: 'polyhaven', kind: 'texture', name: '原型网格贴图',
    author: 'Poly Haven', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbGrid, tags: ['原型', '网格'], summary: '灰盒阶段用的网格贴图，便于判断比例' },
  { id: 'studio_softbox', source: 'polyhaven', kind: 'hdri', name: '摄影棚柔光',
    author: 'Poly Haven', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbHdri, tags: ['室内', '布光'], summary: '中性柔光摄影棚环境，展示模型默认灯光' },
  { id: 'sunset_field', source: 'polyhaven', kind: 'hdri', name: '黄昏旷野',
    author: 'Poly Haven', license: 'CC0', page_url: 'https://polyhaven.com/',
    thumb_url: thumbSunset, tags: ['户外', '黄昏'], summary: '暖色调黄昏户外环境光' },
]

export const demoKenneyPacks = [
  { slug: 'prototype-kit', name: '原型套件', kinds: ['model'], summary: '灰盒原型用几何体模块，搭关卡最快的一套',
    source: 'kenney', license: 'CC0', page_url: 'https://kenney.nl/assets/prototype-kit', thumb_url: thumbCube },
  { slug: 'nature-kit', name: '自然套件', kinds: ['model'], summary: '树木、岩石、灌木等自然道具',
    source: 'kenney', license: 'CC0', page_url: 'https://kenney.nl/assets/nature-kit', thumb_url: thumbTree },
  { slug: 'prototype-textures', name: '原型纹理集', kinds: ['texture'], summary: '网格/棋盘格原型贴图，快速拼出灰盒关卡',
    source: 'kenney', license: 'CC0', page_url: 'https://kenney.nl/assets/prototype-textures', thumb_url: thumbGrid },
  { slug: 'ui-pack', name: 'UI 套件', kinds: ['2d'], summary: '按钮、面板、图标等通用界面素材',
    source: 'kenney', license: 'CC0', page_url: 'https://kenney.nl/assets/ui-pack', thumb_url: thumbUi },
]

export const demoPackFiles = [
  { path: 'Models/primitive_cube.glb', show_path: 'Models/primitive_cube.glb', ext: '.glb', kind: 'model', size: 1820, dep: false },
  { path: 'Models/primitive_cylinder.glb', show_path: 'Models/primitive_cylinder.glb', ext: '.glb', kind: 'model', size: 2140, dep: false },
  { path: 'Models/primitive_plane.glb', show_path: 'Models/primitive_plane.glb', ext: '.glb', kind: 'model', size: 980, dep: false },
  { path: 'Models/ramp_01.glb', show_path: 'Models/ramp_01.glb', ext: '.glb', kind: 'model', size: 1560, dep: false },
  { path: 'Textures/grid_01.png', show_path: 'Textures/grid_01.png', ext: '.png', kind: 'image', size: 3400, dep: false },
  { path: 'License.txt', show_path: 'License.txt', ext: '.txt', kind: 'other', size: 1200, dep: false },
]

/** AI 生成面板演示：生图结果占位 */
export const demoGenImages = [thumbSunset, thumbCube, thumbTree]

/** AI 生成面板演示：4×4 跳跃循环精灵图（小球按帧跳起，可真实循环播放） */
function demoSpriteSheetDataUri(): string {
  const C = 4, FW = 64, S = C * FW
  const cells: string[] = []
  for (let k = 0; k < C * C; k++) {
    const ox = (k % C) * FW
    const oy = Math.floor(k / C) * FW
    const t = k / (C * C - 1)
    const bx = FW / 2
    const by = FW - 14 - Math.abs(Math.sin(t * Math.PI)) * 32
    cells.push(
      `<g transform="translate(${ox},${oy})">`,
      `<rect width="${FW}" height="${FW}" fill="#dfe7f2"/>`,
      `<ellipse cx="${bx}" cy="${FW - 10}" rx="16" ry="3.5" fill="#9fb0c6"/>`,
      `<circle cx="${bx}" cy="${by.toFixed(1)}" r="11" fill="var(--accent)"/>`,
      `<circle cx="${(bx - 3).toFixed(1)}" cy="${(by - 3).toFixed(1)}" r="3" fill="#fff"/>`,
      `<text x="4" y="12" font-size="9" fill="#6b7280">${String(k).padStart(2, '0')}</text>`,
      '</g>',
    )
  }
  const lines: string[] = []
  for (let i = 1; i < C; i++) {
    lines.push(`<line x1="${i * FW}" y1="0" x2="${i * FW}" y2="${S}" stroke="#c2cdda" stroke-width="1"/>`)
    lines.push(`<line x1="0" y1="${i * FW}" x2="${S}" y2="${i * FW}" stroke="#c2cdda" stroke-width="1"/>`)
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${S}" height="${S}" viewBox="0 0 ${S} ${S}">`
    + cells.join('') + lines.join('') + '</svg>'
  return 'data:image/svg+xml,' + encodeURIComponent(svg)
}
export const demoGenSheet = {
  src: demoSpriteSheetDataUri(),
  cols: 4, rows: 4, fw: 64, fh: 64, fps: 12, frames: 16,
}

export const demoAssetLibrary = [
  { path: 'assets/models/vintage_armchair.glb', name: 'vintage_armchair.glb', kind: 'model', size: 248320,
    mtime: Date.now() / 1000 - 3600, source: 'polyhaven', author: 'Kirill Sannikov',
    license: 'CC0', imported_at: '', duplicate: false, thumb: thumbCube },
  { path: 'assets/models/lowpoly_oak.glb', name: 'lowpoly_oak.glb', kind: 'model', size: 184200,
    mtime: Date.now() / 1000 - 7200, source: 'polyhaven', author: 'Dairon Sanchez',
    license: 'CC0', imported_at: '', duplicate: false, thumb: thumbTree },
  { path: 'assets/textures/wood_planks_01.jpg', name: 'wood_planks_01.jpg', kind: 'texture', size: 924532,
    mtime: Date.now() / 1000 - 86400, source: 'polyhaven', author: 'Poly Haven',
    license: 'CC0', imported_at: '', duplicate: false, thumb: thumbWood },
  { path: 'assets/generated/hero_bg.png', name: 'hero_bg.png', kind: 'image', size: 512004,
    mtime: Date.now() / 1000 - 172800, source: 'comfyui', author: '',
    license: '', imported_at: '', duplicate: false, thumb: thumbSunset },
  { path: 'assets/interface-sounds/Audio/click_01.wav', name: 'click_01.wav', kind: 'audio', size: 18420,
    mtime: Date.now() / 1000 - 259200, source: 'kenney', author: 'Kenney',
    license: 'CC0', imported_at: '', duplicate: false, thumb: thumbAudio },
  { path: 'assets/generated/animations/slime_jump/slime_jump_sheet.png',
    name: 'slime_jump.anim.json', kind: 'animation', size: 245760,
    mtime: Date.now() / 1000 - 5400, source: 'comfyui-h3', author: '',
    license: '', imported_at: '', duplicate: false, thumb: demoGenSheet.src,
    fps: 12, frame_count: 16, cols: 4, rows: 4, frame_width: 64, frame_height: 64,
    frames_dir: 'assets/generated/animations/slime_jump/frames',
    first_frame: 'assets/generated/animations/slime_jump/frames/frame_0001.png',
    prompt: 'slime monster jumping in place, seamless loop',
    manifest: 'assets/generated/animations/slime_jump/slime_jump.anim.json' },
]

export const demoRegionCards = [
  {
    key: 'values', name: '数值区', dir: 'values', desc: '玩家与敌人的数值配置',
    access: '', depends_on: [], exports: ['stats_api.gd'], missing_exports: [],
    verify: '', exists: true, git: true, branch: 'main', dirty: true, files: 12,
    dirty_count: 2, own_repo: true,
    last_commit: {
      hash: 'a1b2c3d', full_hash: 'a1b2c3d4', message: 'feat: 调整玩家初始生命值为 120',
      author: '你', time: Date.now() / 1000 - 3600 * 5, time_raw: '',
    },
  },
  {
    key: 'behaviors', name: '角色行为区', dir: 'behaviors',
    desc: '角色行为逻辑（玩家/敌人 AI）', access: '', depends_on: ['values'],
    exports: ['behavior_api.gd'], missing_exports: [], verify: '', exists: true,
    git: true, branch: 'main', dirty: false, files: 23, dirty_count: 0, own_repo: true,
    last_commit: {
      hash: 'e4f5g6h', full_hash: 'e4f5g6h7', message: 'fix: 敌人追击穿墙的问题',
      author: '你', time: Date.now() / 1000 - 3600 * 30, time_raw: '',
    },
  },
  {
    key: 'ui', name: '界面区', dir: 'ui', desc: 'HUD、菜单与弹窗',
    access: '', depends_on: ['values'], exports: [], missing_exports: [],
    verify: '', exists: false, git: false, branch: '', dirty: false, files: 0,
    dirty_count: 0, own_repo: false, last_commit: null,
  },
]

// ---------------------------------------------------------------- 阶段 3b：可执行 demo 流程
// 一条完整的「定位 → 改 → 校验 → 提交 → 导出试玩 → 不满意回滚」流水线。
// 演示态离线可跑：verify 步刻意「首次失败」以演示「红卡 → 单步重跑修复」，
// 其余动作返回可信的中文结果（见 flowRunner.demoResult）。
export const demoFlows: FlowDef[] = [
  {
    id: 'flow-demo-1',
    name: '调平数值 → 校验 → 导出试玩 → 回滚',
    desc: '改玩家生命值 → 分区校验 → 提交变更集 → Web 导出试玩 → 不满意回滚变更集。',
    nodes: [
      { id: 'n1', action: 'dev_list_regions', label: '定位分区', params: {} },
      { id: 'n2', action: 'dev_region_edit', label: '改玩家生命值', params: {
        region: 'values', path: 'balance.json',
        old_text: '"player_hp": 100', new_text: '"player_hp": 120',
      } },
      { id: 'n3', action: 'dev_region_verify', label: '数值区校验', params: { region: 'values' } },
      { id: 'n4', action: 'dev_commit_all', label: '提交变更集', params: { message: '调平玩家初始生命值为 120' } },
      { id: 'n5', action: 'engine_web_export', label: 'Web 导出试玩', params: {} },
      { id: 'n6', action: 'dev_rollback_changeset', label: '不满意就回滚', params: { changeset: '' } },
    ],
    edges: [
      { source: 'n1', target: 'n2' },
      { source: 'n2', target: 'n3' },
      { source: 'n3', target: 'n4' },
      { source: 'n4', target: 'n5' },
      { source: 'n5', target: 'n6' },
    ],
  },
]

/** 演示态：这些节点「首次执行失败」，用于演示「失败停红 → 单步重跑修复 → 续跑」。 */
export const demoFlowFlaky: Record<string, string[]> = {
  'flow-demo-1': ['n3'],
}

// ---------------------------------------------------------------- 阶段 4：Bug 反馈闭环（边玩边改）演示
// 一条「发现问题 → 归档 → 让 AI 修 → 重导出 → 回滚」的离线示例（?demo=1 可看）。
export const demoBugs: BugItem[] = [
  {
    id: 'BUG-20260918-101530-a1b2c3', title: '敌人受击后血量不下降',
    severity: 'error', source_region: 'behaviors', status: 'open',
    error: "Invalid access to property 'hp' on a base object of type: 'Node2D'.",
    traceback: 'scripts/enemy/enemy_ai.gd:42\ntake_damage(damage) -> hp -= damage',
    reproduction: '运行「战斗场景」，让玩家攻击敌人，敌人 HP 条不变化。',
    created_at: new Date(Date.now() - 1000 * 60 * 12).toISOString(),
  },
  {
    id: 'BUG-20260918-100210-d4e5f6', title: '玩家初始生命值调成 120 后 HUD 仍显示 100',
    severity: 'warning', source_region: 'values', status: 'investigating',
    error: 'balance.json 的 player_hp 与 HUD 绑定不一致',
    traceback: '', reproduction: '改 values/balance.json 的 player_hp=120，重导出后 HUD 仍显示 100。',
    created_at: new Date(Date.now() - 1000 * 60 * 55).toISOString(),
  },
  {
    id: 'BUG-20260917-203355-9a8b7c', title: '新手引导第 3 步点不动「继续」',
    severity: 'info', source_region: 'ui', status: 'fixed',
    error: '按钮 hitbox 与 sprite 尺寸不一致', traceback: '', reproduction: '进入新手引导到第 3 步。',
    created_at: new Date(Date.now() - 1000 * 60 * 60 * 20).toISOString(),
    updated_at: new Date(Date.now() - 1000 * 60 * 60 * 19).toISOString(),
  },
]

/** 演示态变更集（供「回滚本次改动」展示）。 */
export const demoChangesets: ChangesetItem[] = [
  { id: 'cs-9f3a1c2b', message: 'fix: 修正敌人受击扣血逻辑', commits: { behaviors: 'a1b2c3d', ui: 'b2c3d4e' } },
  { id: 'cs-1a2b3c4d', message: '调平玩家初始生命值为 120', commits: { values: 'e4f5g6h' } },
]

/** 演示态「让 AI 修」返回的修复结论。 */
export const demoBugFixAnswer =
  '已定位：scripts/enemy/enemy_ai.gd:42 在 take_damage() 里误用 owner.hp（Node2D 无此属性）。\n' +
  '修复：改为读取 $Stats.hp 并回写，受击后同步刷新血条。\n' +
  '改动文件：behaviors/scripts/enemy/enemy_ai.gd（1 处）。\n' +
  '建议：点「重导出并重载」在浏览器里验证；不满意可「回滚本次改动」。'

