<script setup>
import { ref, computed } from 'vue'
import { state } from '../state.js'
import { batchMovies, startScrape, addToCollection, listCollections } from '../api.js'
import { toast, confirmDialog } from '../utils.js'
import { t } from '../i18n/index.js'

const emit = defineEmits(['done'])
const busy = ref(false)
const showTag = ref(false)
const showColl = ref(false)
const tagText = ref('')
const colls = ref([])
const collId = ref('')

const ids = computed(() => Array.from(state.selected))
const n = computed(() => ids.value.length)

function clear() {
  state.selected = new Set()
  state.selMode = false
}

async function apply(patch, label) {
  if (!n.value || busy.value) return
  busy.value = true
  try {
    await batchMovies({ ids: ids.value, ...patch })
    toast(t('bulk.applied', { label: t(label), n: n.value }), 'ok')
    emit('done')
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

async function rate(v) { await apply({ rating: v }, 'bulk.rateStars') }

async function scrapeSelected() {
  if (!n.value) return
  busy.value = true
  try {
    await startScrape({ ids: ids.value })
    state.taskPanelOpen = true
    toast(t('common.scrapeQueued'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

async function openTag() {
  showTag.value = true
  showColl.value = false
  tagText.value = ''
}

async function submitTags() {
  const tags = tagText.value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
  if (!tags.length) { toast(t('bulk.tagEmpty'), 'err'); return }
  await apply({ tags }, 'bulk.setTags')
  showTag.value = false
}

async function openColl() {
  showColl.value = true
  showTag.value = false
  try {
    colls.value = await listCollections() || []
    if (colls.value.length) collId.value = colls.value[0].id
  } catch (e) { toast(e.message, 'err') }
}

async function submitColl() {
  if (!collId.value) { toast(t('bulk.collEmpty'), 'err'); return }
  busy.value = true
  try {
    for (const id of ids.value) await addToCollection(collId.value, id)
    toast(t('bulk.joined', { n: n.value }), 'ok')
    showColl.value = false
    emit('done')
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}
</script>

<template>
  <div v-if="n" class="bulk-bar">
    <span class="n">{{ n }}</span>
    <span class="muted">{{ $t('bulk.selected') }}</span>
    <div class="divider vert"></div>

    <button class="btn tiny ghost" :disabled="busy" @click="apply({ favorite: 1 }, 'bulk.addFav')" :data-tip="$t('bulk.addFav')">♥</button>
    <button class="btn tiny ghost" :disabled="busy" @click="apply({ watched: 1 }, 'bulk.markWatched')" :data-tip="$t('bulk.markWatched')">●</button>
    <button class="btn tiny ghost" :disabled="busy" @click="apply({ watchlist: 1 }, 'bulk.addWatch')" :data-tip="$t('bulk.addWatch')">⌚</button>

    <div class="divider vert"></div>
    <div class="rate-group">
      <button v-for="v in 5" :key="v" class="rb" :disabled="busy" @click="rate(v)" :data-tip="$t('bulk.star', { v })">★</button>
    </div>

    <div class="divider vert"></div>
    <button class="btn tiny ghost" :disabled="busy" @click="openTag">{{ $t('bulk.tag') }}</button>
    <button class="btn tiny ghost" :disabled="busy" @click="openColl">{{ $t('bulk.addToColl') }}</button>
    <button class="btn tiny ghost" :disabled="busy" @click="scrapeSelected">{{ $t('bulk.scrape') }}</button>

    <div class="divider vert"></div>
    <button class="btn tiny ghost" @click="clear">{{ $t('common.cancel') }}</button>

    <!-- 标签输入 -->
    <div v-if="showTag" class="bb-pop">
      <input v-model="tagText" :placeholder="$t('bulk.tagPlaceholder')" @keydown.enter="submitTags" autofocus />
      <button class="btn tiny primary" @click="submitTags">应用</button>
      <button class="btn tiny ghost" @click="showTag = false">✕</button>
    </div>

    <!-- 片单选择 -->
    <div v-if="showColl" class="bb-pop">
      <select v-model="collId">
        <option v-for="c in colls" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>
      <button class="btn tiny primary" :disabled="busy" @click="submitColl">加入</button>
      <button class="btn tiny ghost" @click="showColl = false">✕</button>
    </div>
  </div>
</template>

<style scoped>
.rate-group { display: flex; gap: 1px; }
.rb {
  width: 20px; height: 24px;
  color: var(--c-text-3); font-size: 13px;
  border-radius: var(--r-xs);
  transition: color var(--t-fast), transform var(--t-fast);
}
.rb:hover { color: var(--c-gold); transform: scale(1.16); }
/* 悬停时点亮左侧全部星 */
.rate-group:hover .rb { color: var(--c-gold); }
.rate-group .rb:hover ~ .rb { color: var(--c-text-3); }

.bb-pop {
  position: absolute;
  bottom: calc(100% + 8px); left: 50%;
  transform: translateX(-50%);
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2);
  border-radius: var(--r-md);
  background: var(--c-surface-3);
  border: 1px solid var(--c-line-strong);
  box-shadow: var(--sh-3);
  animation: rise-in var(--t-slow);
}
.bb-pop input, .bb-pop select { width: 220px; height: 28px; }
</style>
