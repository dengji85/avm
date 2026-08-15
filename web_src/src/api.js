// 统一 API 请求封装。开发期由 vite proxy 转发 /api 到后端。
const API = '/api'
const TOKEN_KEY = 'avm_access_token'

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch (e) { return '' }
}
export function setToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY) } catch (e) {}
}
// 远程访问被拒时由 App 注册的回调触发登录模态
let _onNoToken = null
export function onNoToken(cb) { _onNoToken = cb }

export async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options)
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.body)
  }
  const tok = getToken()
  if (tok) opts.headers['X-Access-Token'] = tok
  const res = await fetch(API + path, opts)
  let data = null
  try { data = await res.json() } catch (e) { data = null }
  if (!res.ok) {
    if (res.status === 401 && data && data.code === 'NO_TOKEN') {
      if (_onNoToken) _onNoToken()
      const err = new Error('需要访问令牌')
      err.code = 'NO_TOKEN'
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
export const csvUrl = () => `${API}/export/csv`

/* ---------------- 服务/访问控制 ---------------- */
export const getServerInfo = () => get('/server/info')
export const resetToken = () => post('/server/reset-token')
export const checkUpdate = (channel = 'stable') => get(`/server/check-update?channel=${encodeURIComponent(channel)}`)

/* 女优头像 / 影片背景大图（fanart）：统一走后端接口，兼容本地文件名与远程 URL（墙内代理） */
export const avatarUrl = (avatar) => avatar ? `${API}/avatar/${encodeURIComponent(avatar)}` : ''
export const fanartUrl = (movieId) => `${API}/fanart/${movieId}`

/* ---------------- 影片 ---------------- */
export const listMovies = (params) => get('/movies', params)
export const getMovie = (id) => get(`/movies/${id}`)
export const updateMovie = (id, patch) => put(`/movies/${id}`, patch)
export const deleteMovie = (id, deleteFile = false) => del(`/movies/${id}`, { delete_file: deleteFile || undefined })
export const toggleFlag = (id, field) => post(`/movies/${id}/toggle`, { field })
export const playMovie = (id, body = {}) => post(`/movies/${id}/play`, body)
export const markPlayed = (id) => post(`/movies/${id}/played`)
export const scrapeOne = (id, body = {}) => post(`/movies/${id}/scrape`, body)
export const exportNfo = (id) => post(`/movies/${id}/nfo`, {})
export const getPreviews = (id, generate = false) => get(`/movies/${id}/previews`, { generate: generate || undefined })
export const getSimilar = (id, limit = 12) => get(`/movies/${id}/similar`, { limit })
export const setProgress = (id, body) => put(`/movies/${id}/progress`, body)
export const batchMovies = (body) => post('/movies/batch', body)

/* ---------------- 封面 ---------------- */
export const setCover = (id, body) => post(`/movies/${id}/cover`, body)
export const clearCover = (id) => del(`/movies/${id}/cover`)
export const sniffCovers = () => post('/covers/local-sniff', {})
export function uploadCover(id, file) {
  const fd = new FormData()
  fd.append('file', file)
  return api(`/movies/${id}/cover/upload`, { method: 'POST', body: fd })
}

/* ---------------- 观看会话 / 进度 ---------------- */
export const startSession = (id, body = {}) => post(`/movies/${id}/session/start`, body)
export const updateSession = (id, sid, body) => post(`/movies/${id}/session/${sid}/update`, body)
export const endSession = (id, sid, body = {}) => post(`/movies/${id}/session/${sid}/end`, body)
export const getSessions = (id, limit = 60) => get(`/movies/${id}/sessions`, { limit })
export const getWatchAnalytics = () => get('/watch-analytics')
export const getContinueWatching = (limit = 20) => get('/continue-watching', { limit })
export const clearContinueWatching = () => del('/continue-watching')

/* ---------------- 分面 / 女优 ---------------- */
export const getFacets = (limit = 300) => get('/facets', { limit })
export const listActresses = (params) => get('/actresses', params)
export const getActress = (id, page = 1, pageSize = 24) => get(`/actresses/${encodeURIComponent(id)}`, { page, page_size: pageSize })
export const updateActress = (id, patch) => put(`/actresses/${id}`, patch)
export const toggleActressFav = (ident) => post(`/actresses/${encodeURIComponent(ident)}/favorite`, {})
export const toggleActressFollow = (ident) => post(`/actresses/${encodeURIComponent(ident)}/follow`, {})

/* ---------------- 片单 ---------------- */
export const listCollections = () => get('/collections')
export const createCollection = (body) => post('/collections', body)
export const updateCollection = (cid, body) => put(`/collections/${cid}`, body)
export const deleteCollection = (cid) => del(`/collections/${cid}`)
export const getCollection = (cid) => get(`/collections/${cid}`)
export const addToCollection = (cid, movieId) => post(`/collections/${cid}/movies`, { movie_id: movieId })
export const removeFromCollection = (cid, movieId) => del(`/collections/${cid}/movies/${movieId}`)

/* ---------------- 标签字典 ---------------- */
export const listTags = async () => {
  const r = await get('/tags')
  return (r && r.items) || []
}
export const renameTag = (oldName, newName) => post('/tags/rename', { old: oldName, new: newName })
export const deleteTag = (name) => post('/tags/delete', { name })

/* ---------------- 统计 / 存储 ---------------- */
export const getStats = () => get('/stats')
export const getStatsEnhanced = () => get('/stats-enhanced')
export const getRankings = (kind = 'watched', limit = 30) => get('/rankings', { kind, limit })
export const getWatchHistory = (page = 1, size = 50) => get('/watch-history', { page, size })
export const getStorage = () => get('/storage')
export const getIntegrity = () => get('/integrity')
export const getHealthCheck = () => get('/health-check')
export const getDedup = () => get('/dedup')
export const resolveDedup = (body) => post('/dedup/resolve', body)
export const getQuality = () => get('/quality')
export const cacheAvatars = () => post('/media/cache-avatars', {})
export const fillActressAvatars = () => post('/actresses/cache-avatars', {})
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
export const maintenanceSummary = () => get('/maintenance/summary')
export const reparseCodes = (body = {}) => post('/reparse-codes', body)
export const matchSubtitles = (body) => post('/subtitles/match', body)
export const alignSubtitles = (body) => post('/subtitles/align', body)
export const uploadAndMatchSubtitles = (formData) =>
  post('/subtitles/upload-and-match', formData, { headers: { 'Content-Type': 'multipart/form-data' } })

/* ---------------- AI 增强 ---------------- */
export const aiStatus = () => get('/ai/status')
export const aiGenerateSynopsis = (id) => post(`/ai/generate-synopsis/${id}`, {})
export const aiSuggestTags = (id) => post(`/ai/suggest-tags/${id}`, {})
export const aiSearchIntent = (query) => post('/ai/search-intent', { query })

/* ---------------- 配置 ---------------- */
export const getConfig = () => get('/config')
export const putConfig = (patch) => put('/config', patch)
export const listProviders = () => get('/providers')
export const testScraper = (body) => post('/scraper/test', body)
export const parsePreview = (names) => post('/parse-preview', { names })
export const fsList = (path = '') => get('/fs/list', { path })
