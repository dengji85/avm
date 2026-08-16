<script setup>
import { ref, computed, onMounted, nextTick, watch, onBeforeUnmount } from 'vue'
import { listCollections, addToCollection, createCollection } from '../api.js'
import { toast } from '../utils.js'
import { t } from '../i18n/index.js'
import { state } from '../state.js'

const props = defineProps({
  movieId: { type: Number, required: true },
  // 详情页模式：展开更宽、可多选；卡片模式：紧凑一键
  variant: { type: String, default: 'card' }, // card | detail
})

const open = ref(false)
const collList = ref([])
const picked = ref(new Set())
const newName = ref('')
const busy = ref(false)
const rootEl = ref(null)
const menuPos = ref({ top: 0, left: 0, placement: 'bottom' })

function placeMenu() {
  const btn = rootEl.value?.querySelector('.atc-fab, .btn.icon')
  if (!btn) return
  const r = btn.getBoundingClientRect()
  const mw = 240
  const mh = 320
  let left = r.right - mw
  if (left < 8) left = 8
  let top = r.bottom + 6
  let placement = 'bottom'
  if (top + mh > window.innerHeight - 8) {
    top = r.top - 6 - mh
    placement = 'top'
  }
  if (top < 8) top = 8
  menuPos.value = { top, left, placement }
}

const lastId = computed(() => state.lastCollection || 0)
const lastName = computed(() => {
  const c = collList.value.find((x) => x.id === lastId.value)
  return c ? c.name : ''
})

async function load() {
  try {
    const r = await listCollections()
    collList.value = (r && r.items) || []
  } catch (e) { toast(e.message, 'err') }
}

function toggle() {
  if (open.value) { open.value = false; return }
  open.value = true
  if (!collList.value.length) load()
  // 预选上次片单
  if (state.lastCollection) picked.value = new Set([state.lastCollection])
  nextTick(placeMenu)
}

async function joinOne(id) {
  busy.value = true
  try {
    await addToCollection(id, props.movieId)
    state.lastCollection = id
    const c = collList.value.find((x) => x.id === id)
    toast(t('detail.joinedCollectionName', { name: c ? c.name : id }), 'ok')
    if (props.variant === 'card') open.value = false
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

// 卡片模式：点一次直接加入上次片单（无上次则展开菜单）
function quickAdd() {
  if (lastId.value) { joinOne(lastId.value); return }
  toggle()
}

async function createAndAdd() {
  const name = newName.value.trim()
  if (!name) return
  busy.value = true
  try {
    const c = await createCollection({ name })
    const id = (c && c.id) || (c && c.collection && c.collection.id)
    if (id) {
      await addToCollection(id, props.movieId)
      state.lastCollection = id
      toast(t('detail.joinedCollectionName', { name }), 'ok')
    }
    newName.value = ''
    open.value = false
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

function onPick(id) {
  const s = new Set(picked.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  picked.value = s
}

async function submitPicked() {
  const ids = [...picked.value]
  if (!ids.length) return
  busy.value = true
  try {
    for (const id of ids) await addToCollection(id, props.movieId)
    state.lastCollection = ids[ids.length - 1]
    toast(t('detail.joinedCollectionN', { n: ids.length }), 'ok')
    open.value = false
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

onMounted(() => { if (open.value && !collList.value.length) load() })

// 菜单用 fixed 定位在 body 上，滚动/缩放时位置会错位，统一关闭。
function onReflow() { if (open.value) open.value = false }
watch(open, (v) => {
  if (v) window.addEventListener('scroll', onReflow, true)
  else window.removeEventListener('scroll', onReflow, true)
})
onBeforeUnmount(() => window.removeEventListener('scroll', onReflow, true))
</script>

<template>
  <div class="atc" :class="variant" ref="rootEl">
    <button
      v-if="variant === 'card'"
      class="atc-fab"
      :data-tip="lastId ? $t('detail.addToLast', { name: lastName }) : $t('detail.addToCollection')"
      @click.stop="quickAdd"
    >＋</button>

    <button
      v-else
      class="btn icon"
      :data-tip="$t('detail.addToCollection')"
      @click.stop="toggle"
    >＋</button>

    <Teleport to="body">
      <div
        v-if="open"
        class="atc-menu"
        :style="{ top: menuPos.top + 'px', left: menuPos.left + 'px' }"
        :class="menuPos.placement"
        @click.stop
      >
      <div v-if="lastId" class="atc-last" @click="joinOne(lastId)">
        ↻ {{ $t('detail.addToLast', { name: lastName }) }}
      </div>
      <div class="atc-list">
        <label v-for="c in collList" :key="c.id" class="atc-row" @click.prevent="onPick(c.id)">
          <input type="checkbox" :checked="picked.has(c.id)" />
          <span class="atc-name ellipsis">{{ c.name }}</span>
          <span class="atc-cnt tabular">{{ c.count || 0 }}</span>
        </label>
        <div v-if="!collList.length" class="atc-empty">{{ $t('detail.noCollection') }}</div>
      </div>
      <div class="atc-new">
        <input v-model="newName" :placeholder="$t('collections.namePlaceholder')" @keyup.enter="createAndAdd" />
        <button class="btn tiny primary" :disabled="busy || !newName.trim()" @click="createAndAdd">＋</button>
      </div>
      <div class="atc-foot">
        <button class="btn tiny ghost" @click="open = false">{{ $t('common.cancel') }}</button>
        <button class="btn tiny primary" :disabled="busy || !picked.size" @click="submitPicked">
          {{ $t('detail.addSelected', { n: picked.size }) }}
        </button>
      </div>
      </div>
      </Teleport>
  </div>
</template>

<style scoped>
.atc { position: relative; display: inline-block; }
.atc-fab {
  width: 28px; height: 28px; border-radius: 50%;
  border: 1px solid var(--c-line); background: var(--c-surface);
  color: var(--c-text-2); font-size: 16px; line-height: 1; cursor: pointer;
}
.atc-fab:hover { color: var(--c-accent); border-color: var(--c-accent); }

.atc-menu {
  position: fixed; z-index: 9999;
  width: 240px; max-height: 320px; overflow-y: auto;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); box-shadow: var(--shadow-pop, 0 8px 24px rgba(0,0,0,.35));
  padding: var(--sp-2); display: flex; flex-direction: column; gap: var(--sp-2);
}
.atc-last {
  padding: var(--sp-2); border-radius: var(--r-sm); cursor: pointer;
  background: color-mix(in srgb, var(--c-accent, #4f8cff) 14%, transparent);
  color: var(--c-accent, #4f8cff); font-size: var(--fs-sm);
}
.atc-last:hover { filter: brightness(1.08); }
.atc-list { display: flex; flex-direction: column; max-height: 200px; overflow-y: auto; gap: 2px; }
.atc-row { display: flex; align-items: center; gap: var(--sp-2); padding: 6px 8px; border-radius: var(--r-sm); cursor: pointer; }
.atc-row:hover { background: var(--c-surface-3, var(--c-surface)); }
.atc-name { flex: 1; min-width: 0; }
.atc-cnt { color: var(--c-text-3); font-size: var(--fs-xs); }
.atc-empty { padding: var(--sp-2); color: var(--c-text-3); font-size: var(--fs-sm); }
.atc-new { display: flex; gap: var(--sp-1); }
.atc-new input { flex: 1; min-width: 0; }
.atc-foot { display: flex; justify-content: space-between; gap: var(--sp-2); }
</style>
