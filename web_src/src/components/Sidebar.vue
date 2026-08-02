<script setup>
import { ref, computed } from 'vue'
import { state, NAV_GROUPS, NAV_ICONS, FLAGS, FACET_KINDS, resetFilters, hasActiveFilter } from '../state.js'

const emit = defineEmits(['change'])

/* 分面展开状态 */
const open = ref({ actresses: true, genres: true, studios: false, series: false, prefixes: false, years: false })
const search = ref({})

function go(id) {
  state.view = id
  state.mobileNavOpen = false
}

function isMulti(key) { return key === 'actress' || key === 'genre' }

function facetOn(key, name) {
  if (isMulti(key)) return state[key].includes(name)
  return String(state[key] ?? '') === String(name)
}

function toggleFacet(key, name) {
  if (isMulti(key)) {
    const i = state[key].indexOf(name)
    if (i >= 0) state[key].splice(i, 1)
    else state[key].push(name)
  } else {
    state[key] = facetOn(key, name) ? (key === 'year' ? null : '') : name
  }
  state.page = 1
  if (state.view !== 'gallery') state.view = 'gallery'
  emit('change')
}

function toggleFlagF(f) {
  const i = state.flags.indexOf(f)
  if (i >= 0) state.flags.splice(i, 1)
  else state.flags.push(f)
  state.page = 1
  if (state.view !== 'gallery') state.view = 'gallery'
  emit('change')
}

/** 分面列表：搜索过滤 + 折叠时限量 */
function facetItems(srcKey, key) {
  const all = state.facets[srcKey] || []
  const kw = (search.value[srcKey] || '').trim().toLowerCase()
  const label = (it) => String(it.name ?? it.year ?? '')
  let list = kw ? all.filter((it) => label(it).toLowerCase().includes(kw)) : all
  return list.slice(0, open.value[srcKey] ? 200 : 0)
}
function facetTotal(srcKey) { return (state.facets[srcKey] || []).length }

function clearAll() {
  resetFilters()
  emit('change')
}
const active = computed(hasActiveFilter)
</script>

<template>
  <aside class="sidebar" :class="{ collapsed: state.sidebarCollapsed, 'mobile-open': state.mobileNavOpen }">
    <div class="sb-body">
      <!-- 主导航 -->
      <div v-for="g in NAV_GROUPS" :key="g.title" class="nav-group">
        <div class="nav-group-title">{{ g.title }}</div>
        <div
          v-for="it in g.items"
          :key="it.id"
          class="nav-item"
          :class="{ on: state.view === it.id }"
          @click="go(it.id)"
        >
          <svg class="ico" viewBox="0 0 24 24" fill="currentColor">
            <path :d="NAV_ICONS[it.id]" />
          </svg>
          <span class="lbl">{{ it.label }}</span>
        </div>
      </div>

      <template v-if="!state.sidebarCollapsed && state.view === 'gallery'">
        <!-- 快捷标记 -->
        <div class="nav-group">
          <div class="nav-group-title">
            快捷筛选
            <button v-if="active" class="clear-mini" @click="clearAll">清空</button>
          </div>
          <div class="chip-list flags">
            <button
              v-for="[f, label] in FLAGS"
              :key="f"
              class="chip"
              :class="{ on: state.flags.includes(f) }"
              @click="toggleFlagF(f)"
            >{{ label }}</button>
          </div>
        </div>

        <!-- 分面 -->
        <div v-for="[srcKey, key, , label] in FACET_KINDS" :key="srcKey" class="nav-group facet-block">
          <div class="nav-group-title fh" @click="open[srcKey] = !open[srcKey]">
            <span class="caret" :class="{ down: open[srcKey] }">▸</span>
            {{ label }}
            <span class="ft">{{ facetTotal(srcKey) }}</span>
          </div>
          <template v-if="open[srcKey]">
            <input
              v-if="facetTotal(srcKey) > 12"
              class="facet-search"
              v-model="search[srcKey]"
              :placeholder="`筛选${label}…`"
            />
            <div class="facet-scroll">
              <div
                v-for="it in facetItems(srcKey, key)"
                :key="it.name ?? it.year"
                class="facet-item"
                :class="{ on: facetOn(key, it.name ?? it.year) }"
                @click="toggleFacet(key, it.name ?? it.year)"
              >
                <span class="fname">{{ it.name ?? it.year }}</span>
                <span class="fn">{{ it.count }}</span>
              </div>
              <p v-if="!facetItems(srcKey, key).length" class="fnone">无匹配</p>
            </div>
          </template>
        </div>
      </template>
    </div>

    <div class="sb-foot">
      <button class="nav-item" @click="state.sidebarCollapsed = !state.sidebarCollapsed">
        <svg class="ico chev" :class="{ flip: state.sidebarCollapsed }" viewBox="0 0 24 24" fill="currentColor">
          <path d="M14.71 6.71a.996.996 0 0 0-1.41 0L8.71 11.3a.996.996 0 0 0 0 1.41l4.59 4.59a.996.996 0 1 0 1.41-1.41L10.83 12l3.88-3.88a.996.996 0 0 0 0-1.41z" />
        </svg>
        <span>{{ state.sidebarCollapsed ? '展开侧栏' : '收起侧栏' }}</span>
      </button>
      <p class="hint" v-if="!state.sidebarCollapsed">Ctrl + K 搜索 · 数字键 1-8 快速导航</p>
    </div>
  </aside>

  <div v-if="state.mobileNavOpen" class="nav-mask" @click="state.mobileNavOpen = false"></div>
</template>

<style scoped>
.nav-group-title {
  display: flex; align-items: center; gap: var(--sp-2);
}
.nav-group-title.fh { cursor: pointer; user-select: none; }
.nav-group-title.fh:hover { color: var(--c-text-2); }
.caret { transition: transform var(--t-fast); display: inline-block; font-size: 9px; }
.caret.down { transform: rotate(90deg); }
.ft { margin-left: auto; font-weight: 400; letter-spacing: 0; opacity: .7; }

.clear-mini {
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--c-primary-h);
  text-transform: none;
  letter-spacing: 0;
  padding: 2px 6px;
  border-radius: var(--r-xs);
}
.clear-mini:hover { text-decoration: underline; background: var(--c-primary-soft); }

.chip-list.flags { padding: 0 var(--sp-2); gap: var(--sp-1); }
.chip-list.flags .chip { height: 24px; padding: 0 var(--sp-2); font-size: var(--fs-xs); }
.chip-list.flags .chip.on { background: var(--c-primary); color: #fff; border-color: var(--c-primary); }

.facet-search {
  height: 28px;
  margin: 0 var(--sp-2) var(--sp-1);
  width: calc(100% - var(--sp-4));
  font-size: var(--fs-sm);
  background: var(--c-surface-2);
  border-color: var(--c-line-strong);
}
.facet-scroll { max-height: 232px; overflow-y: auto; padding-right: 2px; }
.fnone { padding: var(--sp-2) var(--sp-3); font-size: var(--fs-sm); color: var(--c-text-3); }

.sb-foot .chev { transition: transform var(--t-fast); }
.sb-foot .chev.flip { transform: rotate(180deg); }

.nav-mask {
  position: fixed; inset: 0; z-index: 29;
  background: var(--c-overlay);
}
@media (min-width: 901px) { .nav-mask { display: none; } }
</style>
