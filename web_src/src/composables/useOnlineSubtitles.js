import { ref, computed } from 'vue'
import { searchOnlineSubtitles, fetchOnlineSubtitle } from '../api.js'

/** 在线字幕搜索：详情页与播放器字幕台共用，避免两处各写一遍候选状态机。 */
export function useOnlineSubtitles(movieIdRef, onPicked) {
  const searching = ref(false)
  const picking = ref('')
  const candidates = ref([])
  const sources = ref([])
  const searched = ref(false)
  const error = ref('')
  const bestId = ref('')
  const elapsed = ref(0)
  const cached = ref(false)
  const hint = ref('')
  const pickedId = ref('')

  const best = computed(() => candidates.value.find(c => c.id === bestId.value) || null)
  const allSources = computed(() => sources.value || [])
  const hasResult = computed(() => searched.value || candidates.value.length > 0)

  async function search() {
    if (searching.value || !movieIdRef.value) return
    searching.value = true
    error.value = ''
    // 故意不清空上一轮结果：搜索期间保留列表，成功后一次性替换，
    // 否则面板会先白一下再冒出内容，就是用户看到的「闪烁」。
    try {
      const r = await searchOnlineSubtitles(movieIdRef.value)
      candidates.value = r.candidates || []
      sources.value = r.sources || []
      bestId.value = r.best_id || ''
      elapsed.value = r.elapsed_ms || 0
      cached.value = !!r.cached
      hint.value = r.hint || ''
      searched.value = true
      pickedId.value = ''
    } catch (e) {
      // 出错也保留旧候选，用户至少还能看到上次搜到的内容
      error.value = e.message
    } finally {
      searching.value = false
    }
  }

  async function pick(c) {
    if (picking.value || !c) return null
    picking.value = c.id
    try {
      const r = await fetchOnlineSubtitle(movieIdRef.value, c.id)
      pickedId.value = c.id
      if (onPicked) await onPicked(r)
      // 不再清空候选列表：采纳后列表突然消失同样是闪烁，改为给该行打标记
      return r
    } catch (e) {
      error.value = e.message
      return null
    } finally {
      picking.value = ''
    }
  }

  /** 全部源查完后对比出来的最优候选，一键采纳 */
  function pickBest() {
    return best.value ? pick(best.value) : null
  }

  function reset() {
    candidates.value = []
    sources.value = []
    searched.value = false
    error.value = ''
    bestId.value = ''
    elapsed.value = 0
    cached.value = false
    hint.value = ''
    pickedId.value = ''
  }

  return {
    searching, picking, candidates, sources, searched, error,
    bestId, best, allSources, elapsed, cached, hint, pickedId, hasResult,
    search, pick, pickBest, reset,
  }
}
