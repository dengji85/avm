<script setup>
import { state, NAV_TABS, NAV_MORE, NAV_ICONS } from '../state.js'

function go(id) {
  state.view = id
  state.mobileNavOpen = false
}
</script>

<template>
  <transition name="mn">
    <div v-if="state.mobileNavOpen" class="mn-mask" @click.self="state.mobileNavOpen = false">
      <aside class="mn-panel">
        <div class="mn-head">
          <b>{{ $t('nav.menu') }}</b>
          <button class="btn ghost icon" @click="state.mobileNavOpen = false">✕</button>
        </div>

        <!-- 一级核心 -->
        <div class="mn-section">
          <div class="mn-group-title">{{ $t('navGroup.core') }}</div>
          <button
            v-for="t in NAV_TABS"
            :key="t.id"
            class="mn-item"
            :class="{ on: state.view === t.id }"
            @click="go(t.id)"
          >
            <span v-if="NAV_ICONS[t.icon]" class="mn-ico">
              <svg viewBox="0 0 24 24" fill="currentColor"><path :d="NAV_ICONS[t.icon]" /></svg>
            </span>
            <span class="mn-label">{{ $t(t.label) }}</span>
            <span v-if="state.view === t.id" class="mn-check">✓</span>
          </button>
        </div>

        <!-- 更多 分组 -->
        <div v-for="g in NAV_MORE" :key="g.key" class="mn-section">
          <div class="mn-group-title">{{ $t(g.title) }}</div>
          <button
            v-for="it in g.items"
            :key="it.id"
            class="mn-item"
            :class="{ on: state.view === it.id }"
            @click="go(it.id)"
          >
            <span v-if="NAV_ICONS[it.icon]" class="mn-ico">
              <svg viewBox="0 0 24 24" fill="currentColor"><path :d="NAV_ICONS[it.icon]" /></svg>
            </span>
            <span class="mn-label">{{ $t(it.label) }}</span>
            <span v-if="state.view === it.id" class="mn-check">✓</span>
          </button>
        </div>
      </aside>
    </div>
  </transition>
</template>

<style scoped>
.mn-mask {
  position: fixed; inset: 0; z-index: 90;
  background: rgba(0,0,0,.5);
}
.mn-panel {
  width: min(300px, 86vw); height: 100%;
  background: var(--c-surface);
  border-right: 1px solid var(--c-line-strong);
  box-shadow: var(--sh-3);
  display: flex; flex-direction: column;
  overflow-y: auto;
  padding-bottom: 40px;
}
.mn-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 16px; border-bottom: 1px solid var(--c-line);
  position: sticky; top: 0; background: var(--c-surface); z-index: 1;
}
.mn-head b { font-size: var(--fs-lg); }
.mn-section { padding: 10px 10px 4px; }
.mn-group-title {
  font-size: 11px; font-weight: 700; color: var(--c-text-3);
  padding: 6px 8px 4px; text-transform: uppercase; letter-spacing: .5px;
}
.mn-item {
  width: 100%; display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border: 0; background: none; border-radius: var(--r-sm);
  color: var(--c-text-2); font-size: var(--fs-md); cursor: pointer; text-align: left;
}
.mn-item:hover { background: var(--c-surface-2); }
.mn-item.on { background: color-mix(in srgb, var(--c-accent, #4f8cff) 14%, transparent); color: var(--c-accent, #4f8cff); }
.mn-ico { width: 20px; height: 20px; flex: none; opacity: .85; }
.mn-ico svg { width: 20px; height: 20px; display: block; }
.mn-label { flex: 1; }
.mn-check { color: var(--c-accent, #4f8cff); font-weight: 700; }

.mn-enter-active, .mn-leave-active { transition: transform .18s ease, opacity .18s; }
.mn-enter-from, .mn-leave-to { transform: translateX(-100%); opacity: 0; }
</style>
