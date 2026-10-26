import { computed, reactive, ref } from 'vue'
import {
  aiStatus, aiGenerateSynopsis, aiApplyPlot, aiSuggestTags,
  aiTranslate, aiDeleteSubtitle, subtitleFileUrl,
  aiAsk, aiAskHistory, aiSimilar, subtitleTracks, updateMovie,
  aiPrompts, aiSavePrompt, aiResetPrompt, aiTraces,
  aiAsrStatus, movieDialog, startMovieDialog, deleteMovieDialog, exportDialogSrt,
} from '../api.js'
import { t } from '../i18n/index.js'

/**
 * 详情页 AI 工作台的状态与动作。
 *
 * 设计原则：
 * 1) 所有会写库的 AI 动作都要「先预览、再确认」，避免一次点击就把人工整理的内容冲掉；
 * 2) 每个动作各自维护 loading / error，互不阻塞——问答应答时仍能看标签建议；
 * 3) 只依赖 movieId + movie 两个响应式引用，便于详情页与播放器复用。
 */
export function useAiAssistant({ movieId, movie, toast, confirm, onChange }) {
  const ready = ref(false)
  const model = ref('')
  const checking = ref(false)
  const probing = ref(false)
  // 后端是否已经加载了 agent 能力：老进程不会返回这个字段，据此提示「重启」
  const agentOk = ref(true)

  /* ---------------- 剧情 ---------------- */
  const plot = reactive({
    loading: false, error: '', draft: '', old: '', source: '', track: '', chars: 0,
    // 模型对字幕/对白返回空时，后端会降级到元数据，并把原因放在这里
    degradedFrom: '',
  })

  /* ---------------- 标签 ---------------- */
  const tags = reactive({ loading: false, error: '', items: [] })

  /* ---------------- 字幕 ---------------- */
  const subs = reactive({
    loading: false, error: '', target: 'zh-Hans', bilingual: false, tracks: [],
  })

  /* ---------------- 问答 ---------------- */
  const ask = reactive({
    loading: false, error: '', question: '', answer: '', refs: [],
    track: '', history: [], openId: 0,
    // 后端取片段的方式：match（命中句）/ digest（整片抽样，泛问时用）
    mode: '',
  })

  /* ---------------- 相似推荐 ---------------- */
  const similar = reactive({ loading: false, error: '', items: [] })

  const aiSubs = computed(() => (movie.value?.subtitles || []).filter((s) => s.source === 'ai-translate'))
  // 只认「真的能读出内容」的字幕轨：0 字节文件与已丢失文件不算，
  // 否则空字幕会让详情页误判成「有字幕」，把语音对白入口藏起来。
  const hasSubtitles = computed(() => subs.tracks.length > 0)
  const hasDraft = computed(() => !!plot.draft)
  const pickedTags = computed(() => tags.items.filter((x) => x.picked))
  /** 当前简介来源：抓取 / AI 基于字幕 / AI 基于元数据 / 人工 */
  const plotSource = computed(() => {
    const s = movie.value?.plot_source || ''
    if (s === 'ai-subtitle') return 'subtitle'
    if (s === 'ai-dialog') return 'dialog'
    if (s === 'ai-meta') return 'meta'
    if (s === 'manual') return 'manual'
    return movie.value?.plot ? 'scraped' : ''
  })

  async function refreshStatus() {
    checking.value = true
    try {
      const r = await aiStatus()
      ready.value = !!r.enabled
      model.value = r.model || ''
      agentOk.value = !!r.agent
    } catch (e) {
      ready.value = false
    } finally {
      checking.value = false
    }
  }

  /** 探测这部片有哪些字幕轨（决定翻译/问答是否可用） */
  async function loadTracks() {
    if (!movieId.value) return
    probing.value = true
    try {
      const r = await subtitleTracks(movieId.value)
      subs.tracks = r.tracks || []
    } catch (e) {
      subs.tracks = []
    } finally {
      probing.value = false
    }
  }

  /* ---------------- 剧情简介 ---------------- */
  async function generatePlot() {
    if (plot.loading) return
    plot.loading = true
    plot.error = ''
    try {
      const r = await aiGenerateSynopsis(movieId.value, true)
      plot.old = r.old_plot ?? (movie.value?.plot || '')
      plot.draft = r.plot || ''
      plot.source = r.source || 'meta'
      plot.track = r.track || ''
      plot.chars = r.chars || 0
      plot.degradedFrom = r.degraded_from || ''
      loadTrace('plot')
    } catch (e) {
      plot.error = e.message || t('ai.failed')
    } finally {
      plot.loading = false
    }
  }

  /** 采用预览结果：写入 plot 并标记 AI 来源（不重新消耗模型） */
  async function applyPlot() {
    if (!plot.draft) return
    try {
      const r = await aiApplyPlot(movieId.value, plot.draft, plot.source)
      if (movie.value) {
        movie.value.plot = r.plot
        movie.value.plot_source = r.plot_source
        movie.value.ai_plot_at = new Date().toISOString()
      }
      // 采用后回到「当前简介」视图，不再保留对比栏
      plot.old = r.plot
      plot.draft = ''
      toast(t('ai.plotApplied'), 'ok')
    } catch (e) {
      plot.error = e.message || t('ai.failed')
    }
  }

  function discardPlot() {
    plot.draft = ''
    plot.error = ''
    plot.old = ''
    plot.degradedFrom = ''
  }

  /* ---------------- 标签建议 ---------------- */
  async function suggestTags() {
    if (tags.loading) return
    tags.loading = true
    tags.error = ''
    try {
      const r = await aiSuggestTags(movieId.value)
      const current = new Set((movie.value?.tags || []).map((x) => String(x)))
      const seen = new Set()
      tags.items = (r.tags || [])
        .map((x) => String(x).trim())
        .filter((x) => x && !seen.has(x) && seen.add(x))
        .map((name) => ({ name, existing: current.has(name), picked: !current.has(name) }))
      if (!tags.items.length) tags.error = t('ai.tagsEmpty')
      loadTrace('tags')
    } catch (e) {
      tags.error = e.message || t('ai.failed')
    } finally {
      tags.loading = false
    }
  }

  function toggleTag(name) {
    const it = tags.items.find((x) => x.name === name)
    if (it) it.picked = !it.picked
  }

  function pickAllTags(on) {
    tags.items.forEach((x) => { if (!x.existing) x.picked = !!on })
  }

  async function applyTags() {
    const picked = pickedTags.value.map((x) => x.name)
    if (!picked.length) return
    try {
      // 不直接并入正式标签（会污染全库标签清单）：存为该片的「AI 建议」待定区，
      // 详情页里确认「转正」后才真正进标签库
      const r = await updateMovie(movieId.value, { tags_ai: picked })
      if (movie.value && r) movie.value.tags_ai = r.tags_ai || picked
      tags.items = tags.items.map((x) => (picked.includes(x.name) ? { ...x, picked: false } : x))
      toast(t('ai.tagsSaved'), 'ok')
      if (onChange) onChange()
    } catch (e) {
      tags.error = e.message || t('ai.failed')
    }
  }

  /* ---------------- 字幕翻译 ---------------- */
  async function translateSubs() {
    if (subs.loading) return
    subs.loading = true
    subs.error = ''
    try {
      const r = await aiTranslate(movieId.value, subs.target, subs.bilingual)
      toast(t('ai.subsDone', { n: r.cues }), 'ok', 3000)
      loadTrace('subs')
      if (onChange) await onChange()
    } catch (e) {
      subs.error = e.message || t('ai.failed')
    } finally {
      subs.loading = false
    }
  }

  function downloadSub(s) {
    window.open(subtitleFileUrl(movieId.value, s.id), '_blank', 'noopener')
  }

  async function removeAiSub(s) {
    const ok = await confirm(t('ai.subsDeleteConfirm'), '', { danger: true, okText: t('common.delete') })
    if (!ok) return
    try {
      await aiDeleteSubtitle(movieId.value, s.id)
      toast(t('ai.subsDeleted'), 'ok')
      if (onChange) await onChange()
    } catch (e) {
      subs.error = e.message || t('ai.failed')
    }
  }

  /* ---------------- 剧情问答 ---------------- */
  async function loadAskHistory() {
    if (!movieId.value) return
    try {
      const r = await aiAskHistory(movieId.value, 20)
      ask.history = r.items || []
    } catch (e) {
      ask.history = []
    }
  }

  async function sendAsk() {
    const q = ask.question.trim()
    if (!q || ask.loading) return
    ask.loading = true
    ask.error = ''
    ask.openId = 0
    try {
      const r = await aiAsk(movieId.value, q)
      ask.answer = r.answer || ''
      ask.refs = r.refs || []
      ask.track = r.track || ''
      ask.mode = r.mode || ''
      ask.question = ''
      loadTrace('ask')
      await loadAskHistory()
    } catch (e) {
      ask.error = e.message || t('ai.failed')
      ask.answer = ''
      ask.refs = []
    } finally {
      ask.loading = false
    }
  }

  /** 展开历史条目：直接复用存下来的答案与依据，不再花 token */
  function toggleHistory(h) {
    if (ask.openId === h.id) {
      ask.openId = 0
      return
    }
    ask.openId = h.id
    ask.answer = h.answer || ''
    ask.refs = h.refs || []
    ask.track = ''
    ask.mode = ''
    ask.error = ''
  }

  /* ---------------- 相似推荐 ---------------- */
  async function loadSimilar() {
    if (similar.loading) return
    similar.loading = true
    similar.error = ''
    try {
      const r = await aiSimilar(movieId.value, 8)
      similar.items = r.items || []
    } catch (e) {
      similar.error = e.message || t('ai.failed')
    } finally {
      similar.loading = false
    }
  }

  /* ---------------- 语音对白提取（ASR，只喂给 AI） ---------------- */
  const asr = reactive({ lib_available: true, model_available: true, model: 'small', model_dir: '' })
  const dialog = reactive({
    status: 'idle', percent: 0, message: '', error: '',
    segments: 0, chars: 0, model: '', text: '', has_text: false, show: false,
    // 只有新版本提取才带逐段时间轴，能导出草稿字幕
    hasSegments: false,
  })
  let dialogTimer = 0

  async function loadAsrStatus() {
    try {
      Object.assign(asr, await aiAsrStatus())
    } catch (e) { /* 探测失败当作不可用，不打扰用户 */ }
  }

  async function loadDialog(preview = 0) {
    if (!movieId.value) return
    try {
      const r = await movieDialog(movieId.value, preview)
      applyDialog(r, preview > 0)
      if (r.status === 'running') startDialogPoll()
    } catch (e) { /* 忽略 */ }
  }

  function applyDialog(r, withText) {
    dialog.status = r.status || 'idle'
    dialog.percent = r.percent || 0
    dialog.message = r.message || ''
    dialog.error = r.error || ''
    dialog.segments = r.segments || 0
    dialog.chars = r.chars || 0
    dialog.model = r.model || ''
    if (withText && r.text !== undefined) dialog.text = r.text || ''
    dialog.has_text = (r.chars || 0) > 0 && dialog.status === 'done'
    dialog.hasSegments = !!r.has_segments
  }

  function startDialogPoll() {
    if (dialogTimer) return
    dialogTimer = setInterval(async () => {
      if (!movieId.value) {
        stopDialogPoll()
        return
      }
      try {
        const r = await movieDialog(movieId.value, dialog.show ? 20000 : 0)
        applyDialog(r, dialog.show)
        if (r.status !== 'running') stopDialogPoll()
      } catch (e) {
        stopDialogPoll()
      }
    }, 2000)
  }

  function stopDialogPoll() {
    if (dialogTimer) {
      clearInterval(dialogTimer)
      dialogTimer = 0
    }
  }

  async function startDialog(maxSeconds = 0) {
    if (dialog.status === 'running') return
    dialog.error = ''
    try {
      // max_seconds：只提取前 N 分钟（快速模式）。AI 写梗概/问答通常够用，
      // 但整片提取要按小时算，先给用户一个「快点看到结果」的选择。
      const r = await startMovieDialog(movieId.value, { max_seconds: Number(maxSeconds) || 0 })
      applyDialog({ ...r, status: r.status || 'running' }, false)
      startDialogPoll()
    } catch (e) {
      dialog.error = e.message || t('ai.failed')
    }
  }

  async function toggleDialogText() {
    dialog.show = !dialog.show
    if (dialog.show) await loadDialog(20000)
  }

  /** 导出草稿 SRT：登记后播放器字幕菜单里会多一条「日文 · ASR」 */
  async function exportSrt() {
    try {
      const r = await exportDialogSrt(movieId.value)
      const key = r.estimated ? 'ai.dialogExportedEstimated' : 'ai.dialogExported'
      toast(t(key, { n: r.cues }), 'ok', 4000)
      if (onChange) await onChange()
    } catch (e) {
      dialog.error = e.message || t('ai.failed')
    }
  }

  async function deleteDialog() {
    try {
      await deleteMovieDialog(movieId.value)
      stopDialogPoll()
      Object.assign(dialog, {
        status: 'idle', percent: 0, message: '', error: '',
        segments: 0, chars: 0, text: '', has_text: false, show: false,
      })
    } catch (e) {
      dialog.error = e.message || t('ai.failed')
    }
  }

  /* ---------------- 提示词审查 / 修改 ---------------- */
  const prompts = reactive({ items: [], loading: false, saving: false, error: '', editingId: '', draft: '' })
  /* ---------------- 调用轨迹（提示词 / 原始返回） ---------------- */
  // byKey 按卡片分开存，避免展开 A 卡片时被 B 卡片的请求覆盖；summary 用于卡片上的耗时摘要
  const traces = reactive({ open: '', loadingKey: '', error: '', byKey: {}, summary: {} })
  // 卡片 → 可能产生调用的 purpose 列表（剧情有分块摘要，字幕翻译有整篇/逐条）
  const TRACE_PURPOSES = {
    plot: ['synopsis', 'plot.subtitle', 'plot.subtitle.chunk', 'plot.dialog', 'plot.dialog.chunk'],
    tags: ['tags'],
    subs: ['subtitle.translate', 'subtitle.translate.one'],
    ask: ['plot.qa'],
  }

  async function loadPrompts() {
    prompts.loading = true
    prompts.error = ''
    try {
      prompts.items = (await aiPrompts()).items || []
    } catch (e) {
      prompts.error = e.message || t('ai.failed')
    } finally {
      prompts.loading = false
    }
  }

  function startEditPrompt(item) {
    if (!item) return
    prompts.editingId = item.id
    prompts.draft = item.system || ''
    prompts.error = ''
  }

  function cancelPrompt() {
    prompts.editingId = ''
    prompts.draft = ''
    prompts.error = ''
  }

  async function savePrompt() {
    if (!prompts.editingId || prompts.saving) return
    prompts.saving = true
    prompts.error = ''
    try {
      const r = await aiSavePrompt(prompts.editingId, prompts.draft)
      replacePrompt(r.item)
      prompts.editingId = ''
      toast(t('ai.promptSaved'), 'ok')
    } catch (e) {
      prompts.error = e.message || t('ai.failed')
    } finally {
      prompts.saving = false
    }
  }

  async function restorePrompt(id) {
    try {
      const r = await aiResetPrompt(id)
      replacePrompt(r.item)
      if (prompts.editingId === id) cancelPrompt()
      toast(t('ai.promptReset'), 'ok')
    } catch (e) {
      prompts.error = e.message || t('ai.failed')
    }
  }

  function replacePrompt(item) {
    if (!item) return
    const i = prompts.items.findIndex((x) => x.id === item.id)
    if (i >= 0) prompts.items.splice(i, 1, item)
    else prompts.items.push(item)
  }

  async function loadTrace(key) {
    const purposes = TRACE_PURPOSES[key] || []
    traces.loadingKey = key
    traces.error = ''
    try {
      const r = await aiTraces(30)
      const items = (r.items || []).filter((x) => purposes.includes(x.purpose)).slice(0, 4)
      traces.byKey[key] = items
      const top = items[0]
      traces.summary[key] = top
        ? { model: top.model, ms: top.ms, at: top.at, cached: top.cached, purpose: top.purpose }
        : null
    } catch (e) {
      traces.error = e.message || t('ai.failed')
      traces.byKey[key] = []
    } finally {
      traces.loadingKey = ''
    }
  }

  function traceItems(key) {
    return traces.byKey[key] || []
  }

  /** 展开/收起某张卡片的调用详情；展开时按需拉取 */
  function toggleTrace(key) {
    if (traces.open === key) {
      traces.open = ''
      return
    }
    traces.open = key
    loadTrace(key)
  }

  /** 重置所有结果（切换影片时调用） */
  function reset() {
    plot.draft = ''
    plot.old = ''
    plot.error = ''
    plot.source = ''
    plot.track = ''
    plot.chars = 0
    plot.degradedFrom = ''
    tags.items = []
    tags.error = ''
    subs.error = ''
    subs.tracks = []
    ask.answer = ''
    ask.refs = []
    ask.mode = ''
    ask.error = ''
    ask.question = ''
    ask.history = []
    ask.openId = 0
    similar.items = []
    similar.error = ''
    traces.open = ''
    traces.loadingKey = ''
    traces.error = ''
    traces.byKey = {}
    traces.summary = {}
    stopDialogPoll()
    Object.assign(dialog, {
      status: 'idle', percent: 0, message: '', error: '',
      segments: 0, chars: 0, text: '', has_text: false, show: false,
    })
    cancelPrompt()
  }

  /** 「12:34」/「1:02:03」→ 秒 */
  function timeToSeconds(text) {
    const parts = String(text || '').split(':').map((x) => Number(x) || 0)
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if (parts.length === 2) return parts[0] * 60 + parts[1]
    return parts[0] || 0
  }

  // 用 reactive 包裹：模板里直接写 ai.ready / ai.hasDraft 就能拿到值（嵌套 ref 不会自动解包）
  return reactive({
    ready, model, checking, probing, agentOk,
    plot, plotSource, hasDraft,
    tags, pickedTags,
    subs, aiSubs, hasSubtitles,
    ask, similar, prompts, traces, asr, dialog,
    loadPrompts, startEditPrompt, cancelPrompt, savePrompt, restorePrompt,
    toggleTrace, loadTrace,
    loadAsrStatus, loadDialog, startDialog, toggleDialogText, deleteDialog, exportSrt, stopDialogPoll,
    refreshStatus, loadTracks, reset, timeToSeconds,
    generatePlot, applyPlot, discardPlot,
    suggestTags, toggleTag, pickAllTags, applyTags,
    translateSubs, downloadSub, removeAiSub,
    loadAskHistory, sendAsk, toggleHistory,
    loadSimilar, traceItems,
  })
}
