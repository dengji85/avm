// 统一 API 请求封装。开发期由 vite proxy 转发 /api 到后端。
import { ref } from 'vue'
export const API = '/api'
const TOKEN_KEY = 'avm_access_token'
// 后端不可达（服务未启动 / 连接被拒）时的全局状态，供 UI 显示离线横幅
export const serverOffline = ref(false)

/**
 * 访问令牌不再保存在浏览器中。
 *
 * 改为「令牌换会话」：登录时把令牌交给后端，由后端下发 **HttpOnly 会话 Cookie**
 * （`avm_session`）。浏览器会自动携带该 Cookie —— 包括 <img> / <video> 这类无法
 * 自定义请求头的请求，所以封面与播放不会再 401。令牌本身只留在服务端，前端读不到，
 * 会话的过期、登出与「容器重启失效」才能真正生效（见 docs/安全与隐私设计.md）。
 */
export function getToken() {
  return '' // 兼容旧调用：不再提供已保存的令牌
}

export function setToken() {
  clearLegacyToken() // 兼容旧调用：立即清除残留的本地令牌
}

/** 清理旧版本遗留的长期凭据（localStorage 令牌 + 可被 JS 读取的 avm_token Cookie）。 */
export function clearLegacyToken() {
  try { localStorage.removeItem(TOKEN_KEY) } catch (e) {}
  try { document.cookie = 'avm_token=; path=/; max-age=0; SameSite=Lax' } catch (e) {}
}

// 打开页面即清理一次历史遗留令牌
clearLegacyToken()
// 远程访问被拒时由 App 注册的回调触发登录模态
let _onNoToken = null
export function onNoToken(cb) { _onNoToken = cb }
// 服务端隐私锁定时（HTTP 423）触发遮挡层
let _onLocked = null
export function onLocked(cb) { _onLocked = cb }

export async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options)
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.body)
  }
  // 认证改走 HttpOnly 会话 Cookie（浏览器自动携带），不再手动注入令牌头
  let res
  try {
    res = await fetch(API + path, opts)
  } catch (e) {
    // 网络层失败：通常是后端服务未启动或连接被拒绝
    serverOffline.value = true
    const err = new Error('无法连接服务器，请确认后端服务（avm）已启动')
    err.code = 'NETWORK'
    err.offline = true
    throw err
  }
  // 能成功拿到响应即说明后端在线，清除离线标记
  serverOffline.value = false
  let data = null
  try { data = await res.json() } catch (e) { data = null }
  if (!res.ok) {
    if (res.status === 401 && data && data.code === 'NO_TOKEN') {
      if (_onNoToken) _onNoToken()
      const err = new Error('需要访问令牌')
      err.code = 'NO_TOKEN'
      throw err
    }
    if (res.status === 423) {
      // 服务端隐私锁定：内容被拒绝返回，前端切到遮挡层（刷新也无法绕过）
      if (_onLocked) _onLocked()
      const err = new Error('内容已锁定')
      err.code = 'LOCKED'
      throw err
    }
    const detail = data && (data.detail || (data.errors && data.errors[0] && data.errors[0].msg))
    throw new Error(typeof detail === 'string' ? detail : `请求失败 (${res.status})`)
  }
  return data
}

/** 构造查询串，自动跳过空值（避免后端 422） */
function qstr(qs) {
  if (!qs) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(qs)) {
    if (v === '' || v == null) continue
    sp.append(k, v)
  }
  const s = sp.toString()
  return s ? '?' + s : ''
}

export const get = (p, qs) => api(p + qstr(qs))
export const post = (p, body) => api(p, { method: 'POST', body })
export const put = (p, body) => api(p, { method: 'PUT', body })
export const del = (p, qs) => api(p + qstr(qs), { method: 'DELETE' })

/* ---------------- 静态资源地址 ---------------- */
export const coverUrl = (id) => `${API}/cover/${id}`
export const coverThumbUrl = (id, w = 360) => `${API}/cover/${id}?w=${w}`
export const previewUrl = (movieId, fname) => `${API}/covers/preview/${movieId}/${encodeURIComponent(fname)}`
export const streamUrl = (id) => `${API}/stream/${id}`
/* .strm 的 HLS 播放列表（服务端已把分片改写成同源地址，供 video.js / VHS 直接播） */
export const strmHlsUrl = (id) => `${API}/strm/hls/${id}`

/* ---------------- 播放兼容：浏览器不认的容器/音轨 → 重封装为 MP4 后再播 ---------------- */
export const playbackInfo = (id) => get(`/movies/${id}/playback-info`)
/* 轻量判断：是否网络串流 / 是否 HLS（不做 ffprobe，播放前调用不拖慢） */
export const streamInfo = (id) => get(`/movies/${id}/stream-info`)
export const prepareStream = (id) => post(`/movies/${id}/prepare-stream`, {})
export const prepareStreamStatus = (id) => get(`/movies/${id}/prepare-stream/status`)
export const streamCacheStats = () => get('/stream-cache')
export const clearStreamCache = () => post('/stream-cache/clear', {})
export const csvUrl = () => `${API}/export/csv`

/* ---------------- 登录 / 会话（见 docs/安全与隐私设计.md） ---------------- */
export const login = (token, totp = '', remember = false) => post('/auth/login', { token, totp, remember })
export const trustedDevices = () => get('/auth/trusted')
export const revokeTrusted = () => post('/auth/trusted/revoke-all', {})
export const logout = () => post('/auth/logout', {})
export const logoutAll = () => post('/auth/logout-all', {})
export const getSession = () => get('/auth/session')
export const listSessions = () => get('/auth/sessions')
export const revokeSession = (sid) => del(`/auth/sessions/${encodeURIComponent(sid)}`)
export const getAudit = (limit = 50) => get(`/auth/audit?limit=${encodeURIComponent(limit)}`)
/* 隐私解锁：快速解锁码（只存哈希在服务端；校验统一走 /privacy/unlock） */
export const setPin = (pin) => post('/auth/set-pin', { pin })
/* 两步验证（TOTP）：密钥与备用码只存在服务端，前端仅提交验证码 */
export const totpStatus = () => get('/auth/totp')
export const totpSetup = () => post('/auth/totp/setup', {})
export const totpEnable = (code) => post('/auth/totp/enable', { code })
export const totpDisable = (code) => post('/auth/totp/disable', { code })
/* 事务 API Key：明文只在创建时返回一次 */
export const listApiKeys = () => get('/auth/keys')
export const createApiKey = (name, scope) => post('/auth/keys', { name, scope })
export const revokeApiKey = (id) => del(`/auth/keys/${encodeURIComponent(id)}`)

/* 隐私锁定：状态存服务端，刷新页面无法绕过 */
export const privacyState = () => get('/privacy/state')
export const privacyLock = (reason = 'manual') => post('/privacy/lock', { reason })
export const privacyUnlock = (credential = '') => post('/privacy/unlock', { credential })

/* ---------------- 服务/访问控制 ---------------- */
export const getServerInfo = () => get('/server/info')
/* 容器内已挂载的目录（宿主机 X → 容器 Y），供首次向导 / 设置页挑选媒体库路径 */
export const fsMounts = () => get('/fs/mounts')
/* 查看 / 更换令牌都要再输一次当前令牌（防会话被劫持后抄走长期凭据） */
export const revealToken = (token) => post('/server/reveal-token', { token })
export const rotateToken = (token) => post('/server/reset-token', { token })
export const checkUpdate = (channel = 'stable') => get(`/server/check-update?channel=${encodeURIComponent(channel)}`)

/* 女优头像 / 影片背景大图（fanart）：统一走后端接口，兼容本地文件名与远程 URL（墙内代理） */
export const avatarUrl = (avatar) => avatar ? `${API}/avatar/${encodeURIComponent(avatar)}` : ''
export const fanartUrl = (movieId) => `${API}/fanart/${movieId}`

/* ---------------- 影片 ---------------- */
export const listMovies = (params) => get('/movies', params)
export const getMovie = (id) => get(`/movies/${id}`)
export const updateMovie = (id, patch) => put(`/movies/${id}`, patch)
export const deleteMovie = (id, deleteFile = false, trashFile = false) =>
  del(`/movies/${id}`, { delete_file: deleteFile || undefined, trash_file: trashFile || undefined })
export const toggleFlag = (id, field) => post(`/movies/${id}/toggle`, { field })
export const playMovie = (id, body = {}) => post(`/movies/${id}/play`, body)
export const markPlayed = (id) => post(`/movies/${id}/played`)
export const startSession = (id, body = {}) => post(`/movies/${id}/session/start`, body)
export const updateSession = (id, sid, body = {}) => post(`/movies/${id}/session/${sid}/update`, body)
export const endSession = (id, sid, body = {}) => post(`/movies/${id}/session/${sid}/end`, body)
export const scrapeOne = (id, body = {}) => post(`/movies/${id}/scrape`, body)
export const exportNfo = (id, body = {}) => post(`/movies/${id}/nfo`, body)
/* 批量导出 NFO/封面到影片目录（给 Emby / Jellyfin 用），后台任务 + 轮询 */
export const exportNfoBatch = (body = {}) => post('/nfo/export', body)
export const exportNfoStatus = () => get('/nfo/export/status')
export const exportNfoCancel = () => post('/nfo/export/cancel', {})
/* Emby 联动（只做「刮削后刷新媒体库」，不参与播放） */
export const embyStatus = () => get('/emby/status')
export const embyTest = (body = {}) => post('/emby/test', body)
export const embyRefresh = () => post('/emby/refresh', {})
export const getPreviews = (id, generate = false) => get(`/movies/${id}/previews`, { generate: generate || undefined })
export const genPreviews = (id, force = false) => post(`/movies/${id}/previews/generate`, { force: force || undefined })
export const regenAllPreviews = () => post('/previews/regenerate-all', {})
export const getSimilar = (id, limit = 12) => get(`/movies/${id}/similar`, { limit })
export const setProgress = (id, body) => put(`/movies/${id}/progress`, body)
export const batchMovies = (body) => post('/movies/batch', body)

/* ---------------- 封面 ---------------- */
export const setCover = (id, body) => post(`/movies/${id}/cover`, body)
export const clearCover = (id) => del(`/movies/${id}/cover`)
export const sniffCovers = () => post('/covers/local-sniff', {})
export const extractCover = (id) => post(`/movies/${id}/extract-cover`, {})
export const extractAllCovers = () => post('/covers/extract-all', {})
export function uploadCover(id, file) {
  const fd = new FormData()
  fd.append('file', file)
  return api(`/movies/${id}/cover/upload`, { method: 'POST', body: fd })
}

/* ---------------- 观看会话 / 进度 ---------------- */
export const getSessions = (id, limit = 60) => get(`/movies/${id}/sessions`, { limit })
export const getWatchAnalytics = () => get('/watch-analytics')
export const getContinueWatching = (limit = 20) => get('/continue-watching', { limit })
export const getRecentWatched = (params = {}) => get('/recent-watch', params)
export const clearContinueWatching = () => del('/continue-watching')

/* ---------------- 分面 / 女优 ---------------- */
export const getFacets = (limit = 300) => get('/facets', { limit })
export const listActresses = (params) => get('/actresses', params)
export const getActress = (id, page = 1, pageSize = 24, sort = 'date') => get(`/actresses/${encodeURIComponent(id)}`, { page, page_size: pageSize, sort })
export const updateActress = (id, patch) => put(`/actresses/${id}`, patch)
export const renameActress = (id, name) => post(`/actresses/${id}/rename`, { name })
export const mergeActress = (targetId, sourceId) => post(`/actresses/${targetId}/merge`, { source_id: sourceId })
export const pluginFetch = (actressId, pluginId) => post(`/actresses/${actressId}/plugin-fetch`, { plugin_id: pluginId })
export const fillAvatarsGfriends = (limit = 0) => post(`/actresses/fill-avatars-gfriends?limit=${limit}`, {})
export const actressFetchStatus = () => get('/actresses/fetch-status')
export const actressFetchCancel = () => post('/actresses/fetch-cancel', {})
export const listPlugins = () => get('/plugins')
export const togglePlugin = (pluginId) => post(`/plugins/${pluginId}/toggle`, {})
export const toggleActressFav = (ident) => post(`/actresses/${encodeURIComponent(ident)}/favorite`, {})
export const toggleActressFollow = (ident) => post(`/actresses/${encodeURIComponent(ident)}/follow`, {})

/* ---------------- 片单 ---------------- */
export const listCollections = () => get('/collections')
export const createCollection = (body) => post('/collections', body)
export const updateCollection = (cid, body) => put(`/collections/${cid}`, body)
export const deleteCollection = (cid) => del(`/collections/${cid}`)
export const getCollection = (cid, qs) => get(`/collections/${cid}`, qs)
export const addToCollection = (cid, movieId) => post(`/collections/${cid}/movies`, { movie_id: movieId })
export const removeFromCollection = (cid, movieId) => del(`/collections/${cid}/movies/${movieId}`)
export const reorderCollection = (cid, ids) => post(`/collections/${cid}/order`, { ids })
export const setCollectionPlayhead = (cid, movieId) => post(`/collections/${cid}/playhead`, { movie_id: movieId })

/* ---------------- 标签字典 ---------------- */
export const listTags = async () => {
  const r = await get('/tags')
  return (r && r.items) || []
}
export const renameTag = (oldName, newName) => post('/tags/rename', { old: oldName, new: newName })
export const deleteTag = (name) => post('/tags/delete', { name })

/* ---------------- 统计 / 存储 ---------------- */
export const getProfile = () => get('/profile')
export const getStats = () => get('/stats')
export const getStatsEnhanced = () => get('/stats-enhanced')
export const getTasteProfile = (year = 0) => get('/stats/taste-profile', { year: year || undefined })
export const getRecommend = (params = {}) => get('/movies/recommend', params)
export const getYearInReview = (year) => get('/year-in-review?year=' + encodeURIComponent(year))
export const getRankings = (kind = 'watched', limit = 30) => get('/rankings', { kind, limit })
export const getWatchHistory = (params = {}) => get('/watch-history', params)
export const getStorage = () => get('/storage')
export const getIntegrity = () => get('/integrity')
export const getHealthCheck = () => get('/health-check')
export const getSoftwareStorage = () => get('/storage/software')
export const cleanSoftwareStorage = (targets) => post('/storage/software/clean', { targets })
export const getDedup = () => get('/dedup')
export const resolveDedup = (body) => post('/dedup/resolve', body)
export const getQuality = () => get('/quality')
export const cacheAvatars = () => post('/media/cache-avatars', {})
export const fillActressAvatars = () => post('/actresses/cache-avatars', {})
export const fetchActressProfile = (limit = 0) => post(`/actresses/fetch-profile?limit=${limit}`, {})
export const fetchOneActressProfile = (id) => post(`/actresses/${id}/fetch-profile`, {})
export const rescanLocalCovers = () => post('/scan/local-covers', {})

/* ---------------- 扫描 / 刮削任务 ---------------- */
export const startScan = (body = {}) => post('/scan', body)
export const scanStatus = () => get('/scan/status')
export const cancelScan = () => post('/scan/cancel', {})
export const startScrape = (body = {}) => post('/scrape', body)
export const scrapeStatus = () => get('/scrape/status')
export const cancelScrape = () => post('/scrape/cancel', {})
export const scrapeTasks = (limit = 50) => get('/scrape/tasks', { limit })
export const scrapeLogs = (qs = {}) => get('/scrape/logs', qs)
export const scrapeLogsClear = (qs = {}) => del('/scrape/logs', qs)
export const scrapeSkips = () => get('/scrape/skips')
export const scrapeSkipsClear = (movie_id = 0) => del('/scrape/skips', { movie_id })
export const scrapeFailures = (qs = {}) => get('/scrape/failures', qs)
export const scrapeRetryNeterr = (body = {}) => post('/scrape/retry-neterr', body)
export const scrapeRetryWithProvider = (body = {}) => post('/scrape/retry-with-provider', body)
export const openFile = (path) => post('/open-file', { path })
export const maintenanceSummary = () => get('/maintenance/summary')
export const checkMissing = () => post('/maintenance/check-missing', {})
export const reparseCodes = (body = {}) => post('/reparse-codes', body)
export const matchSubtitles = (body) => post('/subtitles/match', body)
export const alignSubtitles = (body) => post('/subtitles/align', body)
export const uploadAndMatchSubtitles = (formData) =>
  post('/subtitles/upload-and-match', formData, { headers: { 'Content-Type': 'multipart/form-data' } })

/* ---------------- AI 增强 ---------------- */
export const aiStatus = () => get('/ai/status')
export const aiGenerateSynopsis = (id, preview = false) => post(`/ai/generate-synopsis/${id}`, { preview })
/* 采用预览生成的简介：写入 plot 并标记来源（不重新调用模型） */
export const aiApplyPlot = (id, plot, source) => post(`/ai/plot/apply/${id}`, { plot, source })
export const aiSuggestTags = (id) => post(`/ai/suggest-tags/${id}`, {})

/* AI 字幕翻译：单部同步生成 */
export const aiTranslate = (id, target = 'zh-Hans', bilingual = false) =>
  post(`/ai/translate/${id}?target=${encodeURIComponent(target)}&bilingual=${bilingual ? '1' : '0'}`, {})
export const aiDeleteSubtitle = (id, subId) => del(`/movies/${id}/subtitles/${subId}`)

/* ---- 模型池 ---- */
export const aiModels = () => get('/ai/models')
export const aiModelSave = (body) => post('/ai/models', body)
export const aiModelDelete = (name) => del(`/ai/models/${encodeURIComponent(name)}`)
export const aiModelSetDefault = (name) => post(`/ai/models/${encodeURIComponent(name)}/default`, {})
export const aiModelTest = (name) => post(`/ai/models/${encodeURIComponent(name)}/test`, {})
/* 播放器字幕轨（内嵌轨 + 已登记字幕，均转 VTT）与片头片尾跳过点 */
export const subtitleTracks = (id) => get(`/movies/${id}/subtitle-tracks`)
/* 扫描视频同目录的外挂字幕并登记（幂等） */
export const scanMovieSubtitles = (id) => post(`/movies/${id}/subtitles/scan`, {})
export const setSkipPoints = (id, body) => put(`/movies/${id}/skip`, body)
/* AI 剧情问答与相似推荐 */
export const aiAsk = (id, question) => post(`/ai/ask/${id}`, { question })
export const aiAskHistory = (id, limit = 10) => get(`/ai/ask/${id}`, { limit })
export const aiSimilar = (id, limit = 5) => get(`/ai/similar/${id}`, { limit })
/* 语音对白提取（只喂给 AI 剧情，不产出字幕） */
export const aiAsrStatus = () => get('/ai/asr/status')
/* 可选扩展组件（ffmpeg / 语音识别依赖 / 模型） */
export const extStatus = () => get('/ext/status')
export const extTask = () => get('/ext/task')
export const extInstall = (cid, source = 'auto') => post(`/ext/${cid}/install`, { source })
export const extCancel = () => post('/ext/task/cancel', {})
export const extImport = (cid, dir) => post(`/ext/${cid}/import`, { dir })
export const extUninstall = (cid) => post(`/ext/${cid}/uninstall`, {})
export const movieDialog = (id, preview = 0) => get(`/movies/${id}/dialog`, { preview })
export const startMovieDialog = (id, body = {}) => post(`/movies/${id}/dialog`, body)
export const deleteMovieDialog = (id) => del(`/movies/${id}/dialog`)
/* 把提取的对白导出成草稿 SRT，并登记为字幕 */
export const exportDialogSrt = (id) => post(`/movies/${id}/dialog/export-srt`, {})
/* AI 提示词审查 / 修改，以及每次调用的提示词与原始返回 */
export const aiPrompts = () => get('/ai/prompts')
export const aiSavePrompt = (id, system) => put(`/ai/prompts/${id}`, { system })
export const aiResetPrompt = (id) => del(`/ai/prompts/${id}`)
export const aiTraces = (limit = 20, purpose = '') => get('/ai/traces', { limit, purpose })
export const aiTracesClear = () => del('/ai/traces')
/* DLNA 投屏 */
export const dlnaDevices = () => get('/dlna/devices')
export const dlnaAddDevice = (ip, port = 1900) => post('/dlna/devices', { ip, port })
export const dlnaCast = (id, usn) => post('/dlna/cast', { movie_id: id, usn })
export const dlnaStop = (usn) => post('/dlna/stop', { usn })
export const subtitleFileUrl = (id, subId) => `${API}/movies/${id}/subtitles/${subId}/file`
/* AI 批量任务 */
/* AI 批量任务：批次为单位的预览 / 提交 / 明细 / 重试 / 取消 / 删除 */
export const aiJobs = (params = {}) => get('/ai/jobs', params)
export const aiJobsCreate = (body) => post('/ai/jobs', body)
export const aiJobsPreview = (body = {}) => post('/ai/jobs/preview', body)
export const aiJobsDetail = (batchId, params = {}) => get(`/ai/jobs/${encodeURIComponent(batchId)}`, params)
export const aiJobsRetry = (batchId, body = {}) => post(`/ai/jobs/${encodeURIComponent(batchId)}/retry`, body)
export const aiJobsCancelBatch = (batchId) => post(`/ai/jobs/${encodeURIComponent(batchId)}/cancel`, {})
export const aiJobsDelete = (batchId) => del(`/ai/jobs/${encodeURIComponent(batchId)}`)
export const aiJobsCancel = () => post('/ai/jobs/cancel', {})
/* 术语表 */
export const aiGlossary = () => get('/ai/glossary')
export const aiGlossarySave = (items) => put('/ai/glossary', { items })
/* 从库里的女优 / 系列生成术语候选（只返回建议，不落库） */
export const aiGlossarySuggest = (params = {}) => get('/ai/glossary/suggest', params)
export const aiSearchIntent = (query) => post('/ai/search-intent', { query })
export const aiLibraryQa = (question) => post('/ai/library-qa', { question })
/* 内置 agent：多步工具调用（查库字典 → 检索 → 换词重试 → 结论），返回 items/rows/steps */
export const aiAgent = (question) => post('/ai/agent', { question })
export const aiWebsearchStatus = () => get('/ai/websearch/status')
export const aiWebsearchCheck = (query) => post('/ai/websearch/check', { query })

/**
 * 内置 agent 的流式版本：每完成一次工具调用回调一条 step，结论逐字回调 delta。
 *
 * 用 fetch + ReadableStream 而不是 EventSource——EventSource 只能 GET，发不了问题。
 * 返回 done 事件的载荷；onEvent 会收到所有事件（step / delta / answer / done）。
 */
export async function aiAgentStream(question, onEvent, movie = null) {
  const res = await fetch(API + '/ai/agent/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(movie ? { question, movie } : { question }),
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const d = await res.json()
      if (d && d.detail) msg = d.detail
    } catch (e) { /* 非 JSON 错误体，保留状态码 */ }
    const err = new Error(msg)
    err.code = res.status === 401 ? 'NO_TOKEN' : 'HTTP'
    throw err
  }
  if (!res.body) throw new Error('当前浏览器不支持流式响应')

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let final = null
  let failure = ''

  const handle = (raw) => {
    let ev
    try { ev = JSON.parse(raw) } catch (e) { return }
    if (ev.type === 'done') final = ev
    else if (ev.type === 'error') failure = ev.message || 'AI 调用失败'
    if (onEvent) onEvent(ev)
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data:')) handle(line.slice(5).trim())
      }
    }
  }
  if (failure) throw new Error(failure)
  return final
}
/* 按结构化规格执行问库查询（快捷提问用）：不走模型，AI 关闭时也能用 */
export const aiLibraryRun = (body) => post('/ai/library-run', body)

/* ---------------- 配置 ---------------- */
export const getConfig = () => get('/config')
export const putConfig = (patch) => put('/config', patch)
export const setupDone = () => put('/config', { server: { setup_done: true } })
export const listProviders = () => get('/providers')
export const testScraper = (body) => post('/scraper/test', body)
export const parsePreview = (names) => post('/parse-preview', { names })
export const fsList = (path = '') => get('/fs/list', { path })
