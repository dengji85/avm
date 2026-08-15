<script setup>
import { reactive, computed } from 'vue'
import { state, FACET_KINDS, FLAGS } from '../state.js'

/* 分面配置：FACET_KINDS = [key, field, multi, label] */

// 需要折叠的维度：值可能较多，默认只显示前 INITIAL 个 + 「显示更多」，并支持组内搜索
const COLLAPSIBLE = new Set(['genres', 'tags', 'actresses', 'studios', 'series'])
const INITIAL = 12          // 折叠态下默认展示的热门数量
const SEARCH_THRESHOLD = 20 // 值超过该数量才显示组内搜索框

// 渲染顺序：最常用/高频检索维度置顶（类型、系列、女优），其余依次排列，标记最后。
// 评分、标记为固定分组（不在 FACET_KINDS 中），手动插入。
const byKey = Object.fromEntries(FACET_KINDS.map((g) => [g[0], g]))
const RATING = ['ratings', 'rating', false, 'filter.ratingGE']
const FLAGG = ['flags', 'flags', false, 'filter.flags']
const order = ['genres', 'series', 'actresses', 'tags', 'studios', 'ratings', 'years', 'flags', 'prefixes']
const groupMap = { ratings: RATING, flags: FLAGG, ...byKey }
const groups = order.map((k) => groupMap[k]).filter(Boolean)

// 每个可折叠分面的折叠态 / 搜索词
const ui = reactive({})
for (const k of groups.map((g) => g[0])) {
  if (COLLAPSIBLE.has(k)) ui[k] = { collapsed: true, q: '' }
}

const fullList = (key) => state.facets?.[key] || []

function visibleList(key) {
  let list = fullList(key)
  if (COLLAPSIBLE.has(key)) {
    const q = (ui[key].q || '').trim().toLowerCase()
    if (q) list = list.filter((f) => f.name.toLowerCase().includes(q))
    else if (ui[key].collapsed) list = list.slice(0, INITIAL)
  }
  return list
}

// 已选值优先：始终显示在顶部（不受折叠/搜索影响）
function selectedFirst(key, field, list) {
  const cur = state[field]
  const sel = Array.isArray(cur) ? cur : (cur ? [cur] : [])
  if (!sel.length) return list
  const set = new Set(sel)
  const picked = list.filter((f) => set.has(f.name))
  const rest = list.filter((f) => !set.has(f.name))
  return [...picked, ...rest]
}

function isOn(field, val) {
  const cur = state[field]
  if (Array.isArray(cur)) return cur.includes(val)
  return cur === val
}

function emitReload() {
  window.dispatchEvent(new CustomEvent('avm-reload-view'))
}

function toggle(field, val, multi) {
  if (multi) {
    const cur = state[field]
    const i = cur.indexOf(val)
    if (i >= 0) cur.splice(i, 1)
    else cur.push(val)
  } else {
    state[field] = isOn(field, val) ? '' : val
  }
  state.page = 1
  emitReload()
}

function toggleCollapse(key) {
  ui[key].collapsed = !ui[key].collapsed
  if (!ui[key].collapsed) ui[key].q = '' // 展开时清空搜索，展示全部热门
}

const minRating = computed({
  get: () => state.minRating || 0,
  set: (v) => { state.minRating = Number(v); state.page = 1; emitReload() },
})

function toggleFlag(f) {
  const i = state.flags.indexOf(f)
  if (i >= 0) state.flags.splice(i, 1)
  else state.flags.push(f)
  state.page = 1
  emitReload()
}

defineExpose({ focus: () => {} })
</script>

<template>
  <div class="movie-filter">
    <div class="f-search">
      <input
        v-model="state.q"
        type="search"
        :placeholder="$t('filter.search')"
        @input="state.page = 1"
      />
    </div>

    <!-- 所有分组统一按 order 渲染：顺序由 order 数组决定 -->
    <template v-for="[key, field, multi, label] in groups" :key="key">
      <!-- 评分：滑块 -->
      <div v-if="key === 'ratings'" class="f-group">
        <div class="f-label">{{ $t(label) }}</div>
        <div class="f-rating">
          <input type="range" min="0" max="5" step="1" v-model="minRating" />
          <span class="rv">{{ minRating || $t('filter.all') }}</span>
        </div>
      </div>

      <!-- 标记：固定 FLAGS 常量 -->
      <div v-else-if="key === 'flags'" class="f-group">
        <div class="f-label">{{ $t(label) }}</div>
        <div class="f-chips">
          <button
            v-for="[f, flabel] in FLAGS"
            :key="f"
            class="f-chip"
            :class="{ on: state.flags.includes(f) }"
            @click="toggleFlag(f)"
          >{{ $t(flabel) }}</button>
        </div>
      </div>

      <!-- 可折叠分面（类型/标签/女优/厂商/系列/番号前缀）：折叠 + 组内搜索 -->
      <div v-else-if="COLLAPSIBLE.has(key)" class="f-group collapsible" :class="{ open: !ui[key].collapsed }">
        <div class="f-label head" @click="toggleCollapse(key)">
          <span class="t">{{ $t(label) }}</span>
          <span class="n tabular">{{ fullList(key).length }}</span>
          <span class="caret">{{ ui[key].collapsed ? '▸' : '▾' }}</span>
        </div>

        <input
          v-if="fullList(key).length > SEARCH_THRESHOLD"
          class="f-filter"
          type="search"
          :placeholder="$t('filter.browse') + $t(label) + '…'"
          v-model="ui[key].q"
        />

        <div class="f-chips">
          <button
            v-for="f in selectedFirst(key, field, visibleList(key))"
            :key="f.name"
            class="f-chip"
            :class="{ on: isOn(field, f.name) }"
            @click="toggle(field, f.name, multi)"
          >
            {{ f.name }} <span class="c">{{ f.count }}</span>
          </button>
        </div>

        <div v-if="!ui[key].q && ui[key].collapsed && fullList(key).length > INITIAL" class="f-more">
          <button class="btn tiny ghost" @click="toggleCollapse(key)">
            {{ $t('filter.showAll') }} {{ fullList(key).length }} {{ $t('filter.values') }}{{ $t(label) }}
          </button>
        </div>
      </div>

      <!-- 普通分面（低基数，始终展开）：年份等 -->
      <div v-else class="f-group">
        <div class="f-label">{{ $t(label) }}</div>
        <div class="f-chips">
          <button
            v-for="f in selectedFirst(key, field, fullList(key))"
            :key="f.name"
            class="f-chip"
            :class="{ on: isOn(field, f.name) }"
            @click="toggle(field, f.name, multi)"
          >
            {{ f.name }} <span class="c">{{ f.count }}</span>
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.movie-filter { display: flex; flex-direction: column; gap: 16px; padding: 4px; }
.f-search input {
  width: 100%; box-sizing: border-box;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  color: var(--c-text); border-radius: 8px; padding: 8px 10px; font: inherit;
}
.f-label { font-size: 12px; font-weight: 700; color: var(--c-text-2); margin-bottom: 8px; }
.f-label.head { display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
.f-label.head .t { flex: none; }
.f-label.head .n { opacity: .55; font-size: 11px; font-variant-numeric: tabular-nums; }
.f-label.head .caret { margin-left: auto; opacity: .6; font-size: 10px; }
.f-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.f-chip {
  border: 1px solid var(--c-line); background: var(--c-surface-2);
  color: var(--c-text-2); border-radius: 99px; padding: 4px 10px;
  font: inherit; font-size: 12px; cursor: pointer; transition: all .12s;
}
.f-chip:hover { border-color: var(--c-primary); color: var(--c-text); }
.f-chip.on { background: var(--c-primary); border-color: var(--c-primary); color: #fff; }
.f-chip .c { opacity: .6; font-variant-numeric: tabular-nums; margin-left: 2px; }
.f-rating { display: flex; align-items: center; gap: 10px; }
.f-rating input { flex: 1; }
.rv { font-variant-numeric: tabular-nums; color: var(--c-text-2); min-width: 32px; }
/* 长尾分面：折叠态弱化 */
.f-group.collapsible:not(.open) .f-label.head { color: var(--c-text-3); }
.f-filter {
  width: 100%; box-sizing: border-box; margin-bottom: 8px;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  color: var(--c-text); border-radius: 8px; padding: 6px 9px; font: inherit; font-size: 12px;
}
.f-more { margin-top: 8px; }
/* 展开后的折叠分组：内容较多时内部滚动，避免单个分组撑爆浮层/侧栏 */
.f-group.collapsible.open .f-chips { max-height: 38vh; overflow-y: auto; padding-right: 4px; }
</style>
