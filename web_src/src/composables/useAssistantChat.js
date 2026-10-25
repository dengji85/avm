import { computed, ref, reactive } from 'vue'
import { aiAgentStream, aiLibraryQa, aiLibraryRun, putConfig } from '../api.js'
import { confirmDialog, recommendReason } from '../utils.js'
import { t } from '../i18n/index.js'

/**
 * 助手会话：一句话 → 一个技能 → 一张结果卡。
 *
 * 与 useAiAssistant 的分工：
 * - useAiAssistant 管「原子动作」（生成简介 / 建议标签 / 翻译字幕 / 问答 / 相似），
 *   每个动作自己维护 loading / error / 结果；
 * - 这里只管「读一句话，决定调哪个原子动作，然后把结果摆到会话流里」。
 *
 * 为什么不把解析放到后端：这一版还没有 /api/ai/assistant，路由全是本地关键词 +
 * 显式 chip。好处是零 token、离线可用、点一下立刻有反应；等后端有了统一入口，
 * 只需要把 resolve() 换掉，会话流与结果卡都不用动。
 */

/** 自由输入 → 技能。顺序即优先级：先认明确动词，再认「看资料」，最后才认问句。 */
const RULES = [
  // 联网意图最具体，放最前：避免「网上查查这部片的资料」被资料卡（本机元数据）截走
  { kind: 'web', re: /联网|网上|上网|web搜索|联网搜/i },
  { kind: 'tags', re: /标签|打标|tag/i },
  { kind: 'subs', re: /字幕|翻译|中字|translate|subtitle/i },
  { kind: 'similar', re: /类似|相似|像这样|同款|推荐相似|差不多的/i },
  { kind: 'plot', re: /简介|剧情|讲什么|说什么|讲了什么|概括|概要|梗概|故事|重写|写一段/i },
  // 「资料」必须排在问句之前：问「这个女优是谁」「片长多久」要的是元数据，不是字幕问答
  {
    kind: 'info',
    re: /资料|信息|详情|番号|片商|厂商|厂牌|系列|女优|演员|出演|谁演|时长|多长|多久|体积|多大|大小|分辨率|清晰度|评分|几分|发行|哪年|几几年|什么时候出|入库|文件/,
  },
  { kind: 'ask', re: /[?？]|吗|为什么|结局|后来|谁|什么时候|几分钟|哪一段|转折|怎么回事/i },
]

/**
 * 整库问题本地就能答（统计 / 口味 / 体检），不该一律甩一张影片列表。
 * 这些查询全部走 /api/ai/library-run，本机 SQLite 执行，不花 token、没有模型也能用。
 */
const LIB_RULES = [
  { kind: 'health', re: /健康|体检|磁盘|容量|剩余|组件|没简介|没封面|没女优|缺简介|缺封面|没刮削|刮削失败/i },
  { kind: 'taste', re: /口味|偏好|最常看|喜欢什么|类型分布|画像|常看哪/i },
  { kind: 'watch_stats', re: /看了多少|看过多少|看了几|几场|场次|累计|看了多久|观看时长|有多少部|有几部/i },
  { kind: 'recent', re: /最近入库|最新入库|刚入库|最近添加|最近加/i },
  { kind: 'recommend', re: /今晚看什么|看什么好|推荐点|推荐几部|推荐一些/i },
]

/** 「找一下…」：明确在找片，按本地条件筛（不花 token，没配模型也能用） */
const FIND_RE = /找一下|找找|帮我找|给我找|找点|搜一下|搜搜|来点|有没有.*(片|的)|有什么.*(片|的)/

/** 结构性条件 → 检索 flag。命中后要把这些词从「题材词」里剔掉，否则会被当成 LIKE 关键词 */
const FIND_FLAGS = [
  { re: /中文字幕|中字|有字幕/, flag: 'subtitle' },
  { re: /无码|無碼|无修正/, flag: 'uncensored' },
  { re: /4k|高清|高画质/i, flag: 'hd4k' },
  { re: /收藏|收藏夹/, flag: 'favorite' },
  { re: /没看过|未看|没看/, flag: 'unwatched' },
]

export function useAssistantChat({ ai, movie }) {
  const messages = ref([])
  const input = ref('')
  const busy = ref(false)
  const current = ref('')
  /** 会话清空 / 上下文切换后递增，面板据此把输入框重新聚焦 */
  const focusRequest = ref(0)
  let seq = 0

  /** 问库结果：只有一张卡在用，所以不挂在消息上，避免每张卡各存一份 */
  const library = reactive({
    loading: false, error: '', note: '', items: [], rows: [], answer: '',
    privacy: 'off', polishError: '', polishing: false, done: false,
    kind: '', params: {}, question: '', warning: '',
    // agent 的执行步骤 / 是否正在流式输出：步骤实时上屏靠这两个字段
    steps: [], streaming: false,
  })

  const hasMovie = computed(() => !!(movie.value && movie.value.id))

  const busyText = computed(() => {
    if (!busy.value) return ''
    const keys = {
      plot: 'ai.chat.busyPlot',
      tags: 'ai.chat.busyTags',
      similar: 'ai.chat.busySimilar',
      ask: 'ai.chat.busyAsk',
      library: 'ai.chat.busyLibrary',
    }
    return t(keys[current.value] || 'ai.chat.thinking')
  })

  /** 输入框上方的快捷问题：随上下文换，点一下直接跑（不猜意图、不花 token） */
  const quickChips = computed(() => {
    if (!hasMovie.value) {
      const find = (label, flags, sort = 'added_desc') => ({
        label, kind: 'library', preset: { kind: 'search', params: { flags, sort, page_size: 8 } },
      })
      return [
        { label: t('ai.libTonight'), kind: 'library', preset: { kind: 'recommend', params: { limit: 8 } } },
        { label: t('ai.chat.chipWatchN'), kind: 'library', preset: { kind: 'watch_stats', params: {} } },
        { label: t('ai.libP_taste'), kind: 'library', preset: { kind: 'taste', params: {} } },
        { label: t('ai.chat.chipTasteY'), kind: 'library', preset: { kind: 'taste', params: { year: new Date().getFullYear() } } },
        { label: t('ai.libRecent'), kind: 'library', preset: { kind: 'recent', params: { limit: 8 } } },
        { label: t('ai.libP_health'), kind: 'library', preset: { kind: 'health', params: {} } },
        find(t('ai.libP_topUnwatched'), ['unwatched'], 'rating_desc'),
        find(t('ai.chat.chipCnSub'), ['subtitle']),
        find(t('ai.chat.chipHd4k'), ['hd4k']),
        find(t('ai.libP_nocover'), ['nocover']),
      ]
    }
    const out = [
      { label: t('ai.chat.chipInfo'), kind: 'info' },
      { label: t('ai.chat.chipWeb'), kind: 'web', ask: t('ai.chat.chipWebQ') },
      { label: t('ai.chat.chipPlot'), kind: 'plot' },
      { label: t('ai.chat.chipSimilar'), kind: 'similar' },
      { label: t('ai.chat.chipTags'), kind: 'tags' },
      { label: t('ai.chat.chipSubs'), kind: 'subs' },
    ]
    if (ai.hasSubtitles) out.push({ label: t('ai.askQuick1'), kind: 'ask', ask: t('ai.askQuick1') })
    return out
  })

  /* ---------------- 消息流 ---------------- */

  function pushUser(text) {
    messages.value.push({ id: ++seq, role: 'user', text })
  }

  /** 追加一条助手消息；同类结果卡只留最新一张，旧卡换成一句「已收起」 */
  function pushBot(say, cards = []) {
    for (const m of messages.value) {
      if (m.role !== 'bot' || !m.cards || !m.cards.length) continue
      const keep = m.cards.filter((c) => !cards.includes(c))
      if (keep.length !== m.cards.length) {
        m.cards = keep
        m.archived = true
      }
    }
    const msg = { id: ++seq, role: 'bot', say, cards, archived: false }
    messages.value.push(msg)
    return msg
  }

  function clearMessages() {
    messages.value = []
  }

  /** 换片：卡片读的是 ai.* 的实时状态，旧卡必须收起，否则会拿新片的数据骗人 */
  function invalidate() {
    for (const m of messages.value) {
      if (!m.cards || !m.cards.length) continue
      const keep = m.cards.filter((c) => c === 'library')
      if (keep.length !== m.cards.length) {
        m.cards = keep
        m.archived = true
      }
    }
  }

  function reset() {
    clearMessages()
    input.value = ''
    busy.value = false
    current.value = ''
    resetLibrary()
  }

  function focus() {
    if (typeof window !== 'undefined' && window.innerWidth <= 720) return
    focusRequest.value++
  }

  /* ---------------- 技能路由 ---------------- */

  function resolve(text) {
    if (!hasMovie.value) return 'library'
    const s = String(text || '')
    for (const r of RULES) if (r.re.test(s)) return r.kind
    // 没有明确动词：有字幕就当成「问剧情」，没字幕只能写简介
    return ai.hasSubtitles ? 'ask' : 'plot'
  }

  function libIntent(text) {
    const s = String(text || '')
    for (const r of LIB_RULES) if (r.re.test(s)) return r.kind
    return ''
  }

  /**
   * 有影片上下文时，只有明确指向「这一部」的才按影片处理。
   * 「我看了多少」「有多少部没简介」这类即使开着详情页也问的是整库。
   */
  function isLibraryQuestion(text) {
    if (!hasMovie.value) return true
    return !/这部|这片|该片|本片|这一部|当前片|本片/.test(String(text || ''))
  }

  function presetOf(kind) {
    const withLimit = kind === 'recommend' || kind === 'recent'
    return { kind, params: withLimit ? { limit: 8 } : {} }
  }

  /**
   * 「这部讲什么」的指代问题：没打开影片时「这部」无法指代。
   * 此类句子若掉进找片分支，「讲什么」会被当成题材词去搜影片——答非所问。
   * 两个条件都要满足：指代词（这部/这个片）+ 在问内容（讲什么/怎么样），避免误伤
   * 「这个库有多少部」这类整库问题。
   */
  const THIS_RE = /这部|这片|这个片|这个影片|本片|该片|这一部|当前片|当前这部/
  const CONTENT_Q_RE = /讲什么|讲的是|什么剧情|什么故事|什么内容|剧情|简介|结局|好不好看|值得看|怎么样|如何|什么意思|字幕|女优|评分|多少分钟|多久/

  /** 统一入口：用户按回车 */
  function send(raw) {
    const text = String(raw == null ? input.value : raw).trim()
    if (!text || busy.value) return
    input.value = ''
    pushUser(text)
    const libKind = libIntent(text)
    const libOk = isLibraryQuestion(text)
    // 统计类问题先走本机：问「多少部没简介」却回一堆影片，是最容易被骂的一种答非所问
    if (libKind && libKind !== 'recommend' && libOk) return runPreset(presetOf(libKind))
    // 找片：「50岁以上」「丝袜」这类条件本地就能解析，不必先烧一次模型
    if (FIND_RE.test(text) && libOk) return runFind(text)
    if (libKind === 'recommend' && libOk) return runPreset(presetOf(libKind))
    if (!hasMovie.value) {
      // 没开影片却问「这部讲什么」：指代落空，直接说清楚，绝不拿去当检索词
      if (THIS_RE.test(text) && CONTENT_Q_RE.test(text)) {
        pushBot(t('ai.chat.noMovieCtx'), ['setup'])
        return
      }
      // 库模式下短词本身就是检索条件
      if (libOk && text.length <= 10 && !/[?？]/.test(text)) return runFind(text)
      return runQa(text)
    }
    return runSkill(resolve(text), { askText: text })
  }

  /** 资料卡上的「同女优 / 同系列 / 同片商」：直接查库，不用再打字 */
  function runMore(target, name) {
    const m = movie.value
    if (!m || busy.value) return
    const params = { sort: 'added_desc', page_size: 8 }
    let label = ''
    if (target === 'actress') {
      const a = name || (m.actresses || [])[0]
      if (!a) return
      params.actress = a
      label = `${t('ai.chat.moreActress')} · ${a}`
    } else if (target === 'series' && m.series) {
      params.series = m.series
      label = `${t('ai.chat.moreSeries')} · ${m.series}`
    } else if (target === 'studio' && m.studio) {
      params.studio = m.studio
      label = `${t('ai.chat.moreStudio')} · ${m.studio}`
    } else {
      return
    }
    pushUser(label)
    return runPreset({ kind: 'search', params })
  }

  /** 点 chip：chip 自带技能，不再猜意图 */
  function runChip(c) {
    if (!c || busy.value) return
    pushUser(c.label)
    if (c.kind === 'library') return runPreset(c.preset || { kind: 'recommend', params: {} })
    return runSkill(c.kind, { askText: c.ask || '', force: c.force !== false })
  }

  /** 外部入口（详情页按钮）：打开面板并直接跑一个技能 */
  function start(kind) {
    if (!kind) return focus()
    const labels = {
      info: t('ai.chat.chipInfo'),
      tags: t('ai.chat.chipTags'),
      subs: t('ai.chat.chipSubs'),
    }
    pushUser(labels[kind] || t('ai.chat.chipPlot'))
    // 从详情页点进来时，已有草稿就只展示，不再白花一次调用
    return runSkill(kind, { force: false })
  }

  /** 状态探测是异步的：刚打开面板就点 chip 时等一会儿，别误报「未接入模型」 */
  async function ensureReady() {
    if (ai.ready) return true
    for (let i = 0; i < 8 && ai.checking; i++) {
      await new Promise((r) => setTimeout(r, 150))
    }
    return !!ai.ready
  }

  /** 联网查资料：走 agent（它自己决定搜什么、抓哪几页），结论必须带来源 */
  async function runWeb(text) {
    if (busy.value) return
    if (!(await ensureReady())) {
      pushBot(t('ai.chat.sayNotReady'), ['setup'])
      return
    }
    resetLibrary()
    busy.value = true
    current.value = 'library'
    library.loading = true
    pushBot(t('ai.chat.sayWeb'), ['library'])
    try {
      const res = await streamAgent(text)
      applyLibrary(res || { items: [], rows: [], steps: [], answer: '', note: '' },
        { kind: '', params: {}, question: text })
    } catch (e) {
      library.error = e.message || t('ai.failed')
    } finally {
      busy.value = false
      current.value = ''
      library.loading = false
    }
  }

  async function runSkill(kind, { force = true, askText = '' } = {}) {
    if (busy.value) return

    // 联网查资料：需要模型与搜索后端，agent 会把未配置的原因转述出来
    if (kind === 'web') {
      if (!(await ensureReady())) {
        pushBot(t('ai.chat.sayNotReady'), ['setup'])
        return
      }
      return runWeb(askText || t('ai.chat.chipWebQ'))
    }

    // 资料卡是纯本机元数据：不花 token，也不需要模型，所以放在「未接入模型」判断之前
    if (kind === 'info') {
      pushBot(t('ai.chat.sayInfo'), ['info'])
      return
    }

    // 没有字幕时问答用不了：降级成简介，并把原因说清楚，而不是弹一个空答案
    if (kind === 'ask' && !ai.hasSubtitles && !ai.probing) {
      runPlotWithSay(t('ai.chat.noSubsAsk'), { force: false })
      return
    }
    // 刚打开面板时状态探测还没回来，别急着说「未接入模型」
    if (!(await ensureReady())) {
      pushBot(t('ai.chat.sayNotReady'), ['setup'])
      return
    }

    busy.value = true
    current.value = kind
    try {
      if (kind === 'plot') {
        pushBot(t('ai.chat.sayPlot'), ['plot'])
        if (force || !ai.plot.draft) await ai.generatePlot()
      } else if (kind === 'tags') {
        pushBot(t('ai.chat.sayTags'), ['tags'])
        if (force || !ai.tags.items.length) await ai.suggestTags()
      } else if (kind === 'subs') {
        // 翻译会落盘并消耗额度，只展示配置，由用户点按钮触发
        pushBot(t('ai.chat.saySubs'), ['subs'])
      } else if (kind === 'similar') {
        pushBot(t('ai.chat.saySimilar'), ['similar'])
        if (force || !ai.similar.items.length) await ai.loadSimilar()
      } else if (kind === 'ask') {
        pushBot(t('ai.chat.sayAsk'), ['ask'])
        ai.ask.question = askText || t('ai.askQuick1')
        await ai.sendAsk()
        ai.ask.question = ''
      }
    } finally {
      busy.value = false
      current.value = ''
    }
  }

  /** 简介的降级路径：先说原因，再出卡（不经过 runSkill，避免递归与 busy 互锁） */
  async function runPlotWithSay(say, { force = false } = {}) {
    busy.value = true
    current.value = 'plot'
    try {
      pushBot(say, ['plot'])
      if (force || !ai.plot.draft) await ai.generatePlot()
    } finally {
      busy.value = false
      current.value = ''
    }
  }

  /* ---------------- 问库（没有影片上下文 / 整库问题） ---------------- */

  function resetLibrary() {
    Object.assign(library, {
      loading: false, error: '', note: '', items: [], rows: [], answer: '',
      privacy: 'off', polishError: '', polishing: false, done: false,
      kind: '', params: {}, question: '', plan: null, repaired: false,
      steps: [], warning: '', streaming: false,
    })
  }

  function applyLibrary(res, meta) {
    library.note = res.note || ''
    library.items = (res.items || []).map((m) => ({
      ...m,
      __reason: m && m.reasons ? recommendReason(m.reasons, t) : '',
    }))
    library.rows = res.rows || []
    library.answer = res.answer || ''
    library.privacy = res.privacy || 'off'
    library.polishError = res.polish_error || ''
    library.kind = (meta && meta.kind) || ''
    library.params = (meta && meta.params) || {}
    library.question = (meta && meta.question) || ''
    // 模型对这句话的理解（可能经过一次自动修正），直接展示，避免「它到底搜了什么」不可见
    library.plan = res.plan || null
    library.repaired = !!res.repaired
    // agent 的执行过程：调了哪些工具、各拿到什么；折叠展示，默认不打扰
    library.steps = res.steps || []
    library.warning = res.warning || ''
    library.done = true
  }

  async function runPreset(p) {
    if (busy.value) return
    resetLibrary()
    busy.value = true
    current.value = 'library'
    library.loading = true
    pushBot(t('ai.chat.sayLibrary'), ['library'])
    try {
      const res = await aiLibraryRun({ kind: p.kind || 'search', params: p.params || {} })
      applyLibrary(res, { kind: p.kind, params: p.params, question: '' })
    } catch (e) {
      library.error = e.message || t('ai.failed')
    } finally {
      busy.value = false
      current.value = ''
      library.loading = false
    }
  }

  async function runQa(text) {
    if (busy.value) return
    resetLibrary()
    busy.value = true
    current.value = 'library'
    library.loading = true
    pushBot(t('ai.chat.sayLibrary'), ['library'])
    const fallback = { page_size: 8, q: [text] }
    try {
      // 有模型走 agent（多步），没有就退到关键词检索，不让用户看到报错
      const res = ai.ready
        ? await runModelQuery(text, fallback)
        : await aiLibraryRun({ kind: 'search', params: fallback })
      applyLibrary(res, { kind: '', params: {}, question: text })
    } catch (e) {
      library.error = e.message || t('ai.failed')
    } finally {
      busy.value = false
      current.value = ''
      library.loading = false
    }
  }

  /** 去掉动词与虚词，留下能拿去匹配类型/标签的词：「穿丝袜的」→「丝袜」 */
  function cleanTerm(raw) {
    const t = String(raw || '')
      .replace(/那种|这种|喜欢|爱看|想看|帮我|给我|找一下|找找|找点|搜一下|搜搜|来点|有没有|有什么|推荐|一些|几部|这部|这片|看看|哪些|的|穿|戴|有|是|个|部|一点|大一点|小一点|片|影片|片子|作品|电影|我|想|要/g, '')
      .replace(/[^\p{L}\p{N}]+/gu, '')
      .trim()
    return t && t.length <= 8 ? t : ''
  }

  /**
   * 从一句话里抽检索条件：年龄（数字或口语）+ 题材词 + 结构性 flag。
   * 全在本地完成——「找一下 50 岁以上的」不该先花一次模型调用。
   *
   * 关键约束：**绝不把整句塞进 q**。以前「我找一下穿丝袜的、年纪大一点的」会被当成
   * 一个关键词去 LIKE，必然 0 条；现在按分隔符拆成多个题材词。
   */
  function localFindParams(text) {
    const s = String(text || '')
    const params = { page_size: 8, sort: 'added_desc' }
    let rest = s

    const range = /(\d{2,3})\s*岁?\s*[-~到至]\s*(\d{2,3})\s*岁/.exec(s)
    if (range) {
      params.actress_age_min = Number(range[1])
      params.actress_age_max = Number(range[2])
      rest = rest.replace(range[0], ' ')
    } else {
      const over = /(\d{2,3})\s*岁?\s*(?:以上|及以上|起|往上|\+)/.exec(s)
      if (over) {
        params.actress_age_min = Number(over[1])
        rest = rest.replace(over[0], ' ')
      }
      const under = /(\d{2,3})\s*岁?\s*(?:以下|以内|及以下)/.exec(s)
      if (under) {
        params.actress_age_max = Number(under[1])
        rest = rest.replace(under[0], ' ')
      }
    }

    // 没有数字的口语年龄：阈值是经验值，结果卡上会写明按什么筛的
    if (!params.actress_age_min && /年纪大|年龄大|上了年纪|年长|熟女|中年|偏大|大一点/.test(s)) {
      params.actress_age_min = 40
      rest = rest.replace(/年纪大一点|年纪大|年龄大|上了年纪|年长|熟女|中年|偏大|大一点/g, ' ')
    }
    if (!params.actress_age_max && /年纪小|年轻|小姑娘|少女|嫩/.test(s)) {
      params.actress_age_max = 25
      rest = rest.replace(/年纪小|年轻|小姑娘|少女|嫩/g, ' ')
    }

    const flags = []
    for (const f of FIND_FLAGS) {
      if (f.re.test(s)) {
        flags.push(f.flag)
        // 命中的词要从「题材词」里剔掉，否则「中文字幕」会被当成类型去 LIKE
        rest = rest.replace(new RegExp(f.re.source, f.re.flags.includes('i') ? 'gi' : 'g'), ' ')
      }
    }
    if (flags.length) params.flags = flags

    const terms = rest
      .split(/[、,，。;；/|]|和|跟|还有|以及/)
      .map(cleanTerm)
      .filter(Boolean)
      .slice(0, 3)
    if (terms.length) params.like = terms
    return params
  }

  /**
   * 有模型时的查询顺序：内置 agent（多步）→ 单步规划 → 本地条件。
   * 逐级降级的原因：agent 需要端点支持 tools，规划需要端点能吐合法 JSON，
   * 两条都不行时至少还能按本地条件给结果，而不是报错。
   *
   * agent 返回 0 条时不直接认命：本地条件往往更"死"但更准，能补一次。
   */
  /**
   * 流式跑 agent：每完成一次工具调用就把步骤推上屏，结论逐字出现。
   * 返回 done 载荷，供 applyLibrary 收尾；期间 library.steps / library.answer 已经实时更新。
   */
  async function streamAgent(text) {
    library.steps = []
    library.answer = ''
    library.streaming = false
    // 把「当前影片」告诉 agent：没有元数据时它才能拿番号去联网查
    const m = movie.value
    const ctx = (m && (m.code || m.title)) ? { code: m.code || '', title: m.title || '' } : null
    try {
      return await aiAgentStream(text, (ev) => {
        if (ev.type === 'step' && ev.step) {
          library.steps = library.steps.concat([ev.step])
        } else if (ev.type === 'delta') {
          library.streaming = true
          library.answer += ev.text || ''
        } else if (ev.type === 'answer') {
          library.answer = ev.text || ''
        }
      }, ctx)
    } finally {
      library.streaming = false
    }
  }

  async function runModelQuery(text, localParams) {
    let err = ''
    if (ai.ready) {
      try {
        const res = (await streamAgent(text))
          || { items: [], rows: [], steps: [], answer: '', note: '' }
        const empty = !(res.items || []).length && !(res.rows || []).length
        if (!empty || !hasFindFilter(localParams)) return res
        const local = await aiLibraryRun({ kind: 'search', params: localParams })
        return (local.items || []).length ? local : res
      } catch (e) {
        err = (e && e.message) || String(e)
      }
      try {
        return await aiLibraryQa(text)
      } catch (e) {
        err = err || (e && e.message) || String(e)
      }
    }
    const local = await aiLibraryRun({ kind: 'search', params: localParams })
    // 模型路径全挂时把原因带回卡片：否则用户只看到「没结果」，会以为助手根本不会查
    return err ? { ...local, warning: String(err).slice(0, 200) } : local
  }

  function hasFindFilter(p) {
    return !!(p.actress_age_min || p.actress_age_max || (p.like && p.like.length)
      || (p.flags && p.flags.length) || (p.q && p.q.length))
  }

  /**
   * 找片。两条路：
   * - 短句且本地能解析出条件（「50岁以上」「丝袜」）→ 直接查库，零 token、瞬时；
   * - 长句 / 本地抽不出条件 → 交给模型，它会看到「库字典」里的真实类型名，
   *   跨语言对上（丝袜 → パンスト・タイツ），0 条时还会自动修正一次。
   */
  async function runFind(text) {
    if (busy.value) return
    const local = localFindParams(text)
    const canLocal = hasFindFilter(local)
    if (!canLocal && !ai.ready) {
      // 既抽不出条件、又没有模型：宁可说清楚，也不要返回整个库
      pushBot(t('ai.chat.localHint'), [])
      return
    }
    // 有模型就一律先交给模型（它会看到库字典、可以多步）；本地条件只作为兜底。
    // 之前用「短句走本地」省 token，结果是短句反而最容易解析歪——本末倒置。
    const useModel = ai.ready
    resetLibrary()
    busy.value = true
    current.value = 'library'
    library.loading = true
    pushBot(t('ai.chat.sayLibrary'), ['library'])
    try {
      const res = useModel
        ? await runModelQuery(text, local)
        : await aiLibraryRun({ kind: 'search', params: local })
      applyLibrary(res, { kind: '', params: {}, question: text })
    } catch (e) {
      library.error = e.message || t('ai.failed')
    } finally {
      busy.value = false
      current.value = ''
      library.loading = false
    }
  }

  /** 「AI 解读」：首次点击要说明会外发什么，确认后开开关并重跑上一次查询 */
  async function enablePolish() {
    if (library.polishing || busy.value) return
    const ok = await confirmDialog(t('ai.chat.polishTitle'), t('ai.chat.polishConfirm'), { danger: false })
    if (!ok) return
    library.polishing = true
    library.polishError = ''
    try {
      await putConfig({ ai: { insights: true } })
      let res
      if (library.kind) {
        res = await aiLibraryRun({
          kind: library.kind, params: library.params,
          polish: true, question: library.question,
        })
      } else if (library.question) {
        res = await aiLibraryQa(library.question)
      } else {
        return
      }
      applyLibrary(res, { kind: library.kind, params: library.params, question: library.question })
    } catch (e) {
      library.polishError = e.message || String(e)
    } finally {
      library.polishing = false
    }
  }

  return {
    messages, input, busy, busyText, quickChips, library, focusRequest, hasMovie,
    send, runChip, runMore, start, reset, invalidate, focus, enablePolish, clearMessages,
  }
}
