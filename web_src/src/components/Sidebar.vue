<script setup>
import { ref, onMounted } from 'vue'
import { state, NAV_GROUPS, NAV_ICONS } from '../state.js'
import { maintenanceSummary } from '../api.js'

/* 维护中心待办角标 */
const badge = ref(0)
async function loadBadge() {
  try {
    const s = await maintenanceSummary()
    badge.value = (s.noscrape || 0) + (s.missing_cover || 0) + (s.unrecognized || 0) + (s.missing_files || 0)
  } catch { badge.value = 0 }
}
onMounted(loadBadge)

function go(id) {
  state.view = id
  state.mobileNavOpen = false
  if (id === 'maintenance') loadBadge()
}
</script>

<template>
  <aside class="sidebar" :class="{ collapsed: state.sidebarCollapsed, 'mobile-open': state.mobileNavOpen }">
    <div class="sb-body">
      <div v-for="g in NAV_GROUPS" :key="g.title" class="nav-group">
        <div class="nav-group-title">{{ $t(g.title) }}</div>
        <div
          v-for="it in g.items"
          :key="it.id"
          class="nav-item"
          :class="{ on: state.view === it.id }"
          @click="go(it.id)"
          :data-tip="state.sidebarCollapsed ? $t(it.label) : ''"
        >
          <svg class="ico" viewBox="0 0 24 24" fill="currentColor">
            <path :d="NAV_ICONS[it.id]" />
          </svg>
          <span class="lbl">{{ $t(it.label) }}</span>
          <span v-if="it.id === 'maintenance' && badge > 0" class="nav-badge tabular">{{ badge }}</span>
        </div>
      </div>
    </div>

    <div class="sb-foot">
      <button class="nav-item" @click="state.sidebarCollapsed = !state.sidebarCollapsed">
        <svg class="ico chev" :class="{ flip: state.sidebarCollapsed }" viewBox="0 0 24 24" fill="currentColor">
          <path d="M14.71 6.71a.996.996 0 0 0-1.41 0L8.71 11.3a.996.996 0 0 0 0 1.41l4.59 4.59a.996.996 0 1 0 1.41-1.41L10.83 12l3.88-3.88a.996.996 0 0 0 0-1.41z" />
        </svg>
        <span>{{ state.sidebarCollapsed ? '展开侧栏' : '收起侧栏' }}</span>
      </button>
    </div>
  </aside>

  <div v-if="state.mobileNavOpen" class="nav-mask" @click="state.mobileNavOpen = false"></div>
</template>

<style scoped>
.sb-foot .chev { transition: transform var(--t-fast); }
.sb-foot .chev.flip { transform: rotate(180deg); }

.nav-mask {
  position: fixed; inset: 0; z-index: 29;
  background: var(--c-overlay);
}
@media (min-width: 901px) { .nav-mask { display: none; } }

.nav-item { position: relative; }
.nav-badge {
  margin-left: auto; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 999px;
  background: var(--c-danger, #e5484d); color: #fff; font-size: 11px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}
.sidebar.collapsed .nav-item .lbl,
.sidebar.collapsed .nav-item .nav-badge { display: none; }
.sidebar.collapsed .nav-item { justify-content: center; }
.sidebar.collapsed .nav-item[data-tip]:hover::after {
  content: attr(data-tip);
  position: absolute; left: calc(100% + 8px); top: 50%; transform: translateY(-50%);
  background: var(--c-surface-3, #2a2f3a); color: var(--c-text); font-size: var(--fs-sm);
  white-space: nowrap; padding: 4px 8px; border-radius: var(--r-sm); z-index: 40;
  box-shadow: var(--sh-2, 0 4px 14px rgba(0,0,0,.3)); pointer-events: none;
}
</style>
