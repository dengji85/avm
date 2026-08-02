import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import { listMovies } from '../api.js'
import { toast, debounce } from '../utils.js'

/**
 * 影片列表数据源：负责根据 state 中的筛选条件拉取数据。
 * 所有视图共用同一套查询语义。
 */
export function useLibrary() {
  const items = ref([])
  const total = ref(0)
  const loading = ref(false)
  const error = ref('')
  let reqSeq = 0

  const params = computed(() => ({
    q: state.q || undefined,
    actress: state.actress.length ? state.actress.join(',') : undefined,
    genre: state.genre.length ? state.genre.join(',') : undefined,
    studio: state.studio || undefined,
    series: state.series || undefined,
    prefix: state.prefix || undefined,
    year: state.year || undefined,
    flags: state.flags.length ? state.flags.join(',') : undefined,
    sort: state.sort,
    page: state.page,
    page_size: state.page_size,
    op: state.multiOp,
  }))

  const pageCount = computed(() =>
    Math.max(1, Math.ceil(total.value / (state.page_size || 60))),
  )

  async function load() {
    const seq = ++reqSeq
    loading.value = true
    error.value = ''
    try {
      const r = await listMovies(params.value)
      if (seq !== reqSeq) return          // 丢弃过期响应
      items.value = r.items || []
      total.value = Number(r.total) || 0
      // 页码越界回退
      if (state.page > pageCount.value) {
        state.page = pageCount.value
      }
    } catch (e) {
      if (seq !== reqSeq) return
      error.value = e.message || '加载失败'
      items.value = []
      total.value = 0
      toast(error.value, 'err')
    } finally {
      if (seq === reqSeq) loading.value = false
    }
  }

  const reload = debounce(load, 220)

  /* 条件变化自动重载；改筛选条件时重置页码 */
  watch(
    () => [state.q, state.actress.slice(), state.genre.slice(), state.studio,
           state.series, state.prefix, state.year, state.flags.slice(), state.multiOp],
    () => { state.page = 1; reload() },
    { deep: true },
  )
  watch(() => [state.sort, state.page, state.page_size], reload)

  function onExternalReload() { load() }
  onMounted(() => window.addEventListener('avm-reload-view', onExternalReload))
  onBeforeUnmount(() => window.removeEventListener('avm-reload-view', onExternalReload))

  /** 局部更新一条记录（避免整页重拉） */
  function patchItem(id, patch) {
    const it = items.value.find((x) => x.id === id)
    if (it) Object.assign(it, patch)
  }
  function removeItem(id) {
    const i = items.value.findIndex((x) => x.id === id)
    if (i >= 0) { items.value.splice(i, 1); total.value = Math.max(0, total.value - 1) }
  }

  return { items, total, loading, error, pageCount, load, reload, patchItem, removeItem }
}

/** 选择模式辅助 */
export function useSelection() {
  const selected = computed(() => Array.from(state.selected))
  const count = computed(() => state.selected.size)

  function toggle(id) {
    if (state.selected.has(id)) state.selected.delete(id)
    else state.selected.add(id)
    // 触发响应式
    state.selected = new Set(state.selected)
  }
  function isOn(id) { return state.selected.has(id) }
  function clear() { state.selected = new Set() }
  function selectAll(ids) { state.selected = new Set(ids) }
  function exitMode() { state.selMode = false; clear() }

  return { selected, count, toggle, isOn, clear, selectAll, exitMode }
}
