// ==UserScript==
// @name         AVM Companion（片匣浏览器接入）
// @name:en      AVM Companion
// @namespace    https://github.com/dengji85/avm
// @version      0.5.8
// @description  把在线观看的影片与真实观看时长同步到本地 AVM 片库；仅在你主动点击后上报，页面正文不上传。
// @author       AVM
// @license      MIT
// @match        *://*/*
// 说明：全站启用（@match *://*/*）。若要排除个别站点，请在下一行按正则写 @exclude，
// 例如：// @exclude      /^https?:\/\/([^\/]*\.)?mail[^\/]*\//
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_setClipboard
// @grant        GM_info
// @connect      *
// @run-at       document-start
// ==/UserScript==

/*
 * 隐私与边界（见 docs/浏览器插件与在线资源设计.md）：
 *  - 默认不自动上报：只有点击浮标/菜单后才读取页面元信息与播放器状态；
 *    唯一例外是「已在库的页面」自动续期：捕获到新的播放地址时只 POST 该地址
 *    与页面 URL，用于更新已有来源（不新建影片、不传正文），可在面板里关闭；
 *  - 只上报番号、标题、页面地址、站内 ID、可播放地址与播放秒数，不上传页面正文、Cookie 或 DOM；
 *  - 令牌与待补报队列只存在脚本自己的存储里，不写日志、不回显；
 *  - 播放地址候选只取 URL，不读取响应内容；界面上一律脱敏显示；
 *  - 不抓取、不下载、不转存任何媒体内容，不绕过付费或 DRM。
 */
(function () {
  'use strict';

  // 本机地址（AVM 自己的页面）不注入：那里没有可入库的第三方影片，
  // 注入的浮标只会碍事。直接退出，不产生任何日志与 DOM。
  if (/^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1)$/i.test(location.hostname)) return;

  // 版本号唯一来源是 @version（经 GM_info 读取），避免两处不同步
  const AVM_VER = (typeof GM_info !== 'undefined' && GM_info && GM_info.script)
    ? GM_info.script.version : '?';

  // 入库结果文案：与 /api/ingest/link 的 status（created / source_added / duplicate）
  // 一一对应。插件跑在第三方站点、读不到 AVM 前端的语言设置，按浏览器语言就近匹配。
  const L10N = (() => {
    const lang = String((typeof navigator !== 'undefined' && navigator.language) || 'zh-CN').toLowerCase();
    if (lang.startsWith('ja')) {
      return {
        created: (id) => '追加しました（新規作品 ' + id + '）',
        added: (n) => '新しいソースとして追加しました（この作品は ' + n + ' 件）',
        duplicate: 'ライブラリに登録済み（同じソースのため追加していません）',
        updated: 'ライブラリに登録済み（再生アドレスを更新しました）',
        scraping: ' · 自動スクレイピング中',
        openDetail: 'AVM の詳細を開く',
        linkNew: 'ライブラリに追加',
        linkUpdate: '既存の作品を更新',
        linkAddSource: 'この作品に新しいソースとして追加',
      };
    }
    if (lang.startsWith('zh') && /hant|tw|hk/.test(lang)) {
      return {
        created: (id) => '已加入（新建作品 ' + id + '）',
        added: (n) => '已作為新來源加入（該作品現有 ' + n + ' 個來源）',
        duplicate: '已在片庫（同一來源，未重複加入）',
        updated: '已在片庫（播放地址已更新）',
        scraping: ' · 自動刮削中',
        openDetail: '已加入片庫 · 打開 AVM 詳情',
        linkNew: '立即加入片庫',
        linkUpdate: '更新已有作品',
        linkAddSource: '作為新來源加入本作品',
      };
    }
    if (lang.startsWith('zh')) {
      return {
        created: (id) => '已加入（新建作品 ' + id + '）',
        added: (n) => '已作为新来源加入（该作品现有 ' + n + ' 个来源）',
        duplicate: '已在片库（同一来源，未重复添加）',
        updated: '已在片库（播放地址已更新）',
        scraping: ' · 自动刮削中',
        openDetail: '已在片库 · 打开 AVM 详情',
        linkNew: '立即加入片库',
        linkUpdate: '更新已有作品',
        linkAddSource: '作为新来源加入本作品',
      };
    }
    return {
      created: (id) => 'Added (new title ' + id + ')',
      added: (n) => 'Added as a new source (this title now has ' + n + ' sources)',
      duplicate: 'Already in library (same source, not added again)',
      updated: 'Already in library (playback URL updated)',
      scraping: ' · auto-scraping',
      openDetail: 'In library · open AVM details',
      linkNew: 'Add to library',
      linkUpdate: 'Update existing title',
      linkAddSource: 'Add as a new source to this title',
    };
  })();

  // 阶段诊断日志：用户报告「浮标不出现」时，控制台里断在哪个编号就是哪一步的问题。
  // 每条日志一个字符的代价，换来不再盲猜。
  const dlog = (step, extra) => {
    try { console.info('[AVM-DIAG]', step, extra || ''); } catch (e) { /* 忽略 */ }
  };
  dlog('1 IIFE 启动', location.host + (window.top === window.self ? '' : '（子帧）'));

  // 错误收集：CSP 拦截、初始化异常、浮标不可见……都记下来，
  // 油猴菜单「诊断信息」一键弹出并复制，用户不用开控制台也能反馈证据。
  const avmErrors = [];
  function recordError(msg) {
    try {
      const s = String(msg).slice(0, 160);
      if (avmErrors[avmErrors.length - 1] !== s) avmErrors.push(s);
      if (avmErrors.length > 8) avmErrors.shift();
    } catch (e) { /* 忽略 */ }
  }
  try {
    window.addEventListener('securitypolicyviolation', (e) => {
      recordError('CSP拦截:' + (e.violatedDirective || '?') + ' ' +
        String(e.blockedURI || '').slice(0, 60));
    });
  } catch (e) { /* 忽略 */ }

  const DEFAULTS = {
    baseUrl: 'http://127.0.0.1:8770', token: '', autoWatch: true,
    autoSync: true,          // 已在库的页面捕获到新签名地址时自动更新来源
    manualPlayUrl: '',
  };
  const HEARTBEAT_MS = 10000;
  const QUEUE_MAX = 200;
  const cfg = Object.assign({}, DEFAULTS, GM_getValue('cfg', {}));
  // 自动续期状态提前声明：PerformanceObserver 的 buffered 回调可能在脚本
  // 后半段的状态声明之前就触发 remember()；访问未初始化的 let 会抛 TDZ 异常，
  // 被捕获回调的 catch 吞掉后表现为「浮标还在，但自动同步永远不触发」。
  let existing = null;       // { movie_id } —— 当前页面已在库
  let lastSyncKey = '';
  let lastSyncAt = 0;
  let syncing = false;
  const isTop = (() => { try { return window.top === window; } catch (e) { return false; } })();
  const deviceId = (() => {
    let v = GM_getValue('device_id', '');
    if (!v) { v = 'dev-' + Math.random().toString(36).slice(2, 10); GM_setValue('device_id', v); }
    return v;
  })();

  function save() { GM_setValue('cfg', cfg); }
  function mask(u) {
    try { const x = new URL(u); return x.protocol + '//' + x.host + x.pathname; }
    catch (e) { return String(u || '').split('?')[0]; }
  }
  function esc(s) {
    return String(s || '').replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---------------------------------------------------------------- 凭据 / 请求
  // 访问令牌走 X-Access-Token；设置页生成的专用 API Key 以 avm_ 开头，走 X-API-Key。
  function authHeaders() {
    const key = String(cfg.token || '').trim();
    const h = { 'Content-Type': 'application/json' };
    h[key.startsWith('avm_') ? 'X-API-Key' : 'X-Access-Token'] = key;
    return h;
  }
  // 最近一次与 AVM 的请求结果：用于面板里的「脚本诊断」，
  // 区分「脚本没注入」与「请求被广告拦截扩展挡了」。
  let lastApi = { status: 0, ok: null, at: 0 };

  function api(path, method, body) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: method || 'GET',
        url: cfg.baseUrl.replace(/\/$/, '') + '/api/' + path.replace(/^\//, ''),
        headers: authHeaders(),
        data: body ? JSON.stringify(body) : undefined,
        timeout: 15000,
        onload: (r) => {
          let data = null;
          try { data = JSON.parse(r.responseText); } catch (e) { /* 非 JSON 响应 */ }
          lastApi = { status: r.status, ok: r.status >= 200 && r.status < 300, at: Date.now() };
          resolve({ ok: r.status >= 200 && r.status < 300, status: r.status, data });
        },
        onerror: () => {
          lastApi = { status: 0, ok: false, at: Date.now() };
          resolve({ ok: false, status: 0, data: null });
        },
        ontimeout: () => {
          lastApi = { status: 0, ok: false, at: Date.now() };
          resolve({ ok: false, status: 0, data: null });
        },
      });
    });
  }

  // ---------------------------------------------------------------- 离线补报
  // 断网 / 后端未启动时失败的心跳按原顺序排队，联网后重放；服务端按
  // dedupe_key 幂等复用同一场次，重放不会造成重复计时。
  function loadQueue() { try { return GM_getValue('queue', []) || []; } catch (e) { return []; } }
  function saveQueue(q) { try { GM_setValue('queue', q.slice(-QUEUE_MAX)); } catch (e) { /* 存储不可用 */ } }
  function enqueue(path, method, body) {
    const q = loadQueue();
    q.push({ path: path, method: method || 'GET', body: body || null });
    saveQueue(q);
  }
  let flushing = false;
  async function flushQueue() {
    if (flushing) return;
    const q = loadQueue();
    if (!q.length) return;
    flushing = true;
    let done = 0;
    try {
      for (const item of q) {
        const r = await api(item.path, item.method, item.body);
        if (!r.ok) break;              // 还没恢复：剩下的保持原顺序留到下次
        done++;
      }
    } finally {
      saveQueue(q.slice(done));
      flushing = false;
    }
  }
  setInterval(flushQueue, HEARTBEAT_MS);
  window.addEventListener('online', flushQueue);

  // ---------------------------------------------------------------- 播放地址捕获
  // document-start 就装钩子，早于播放器发请求。hls.js / MSE 用 blob: 作为
  // video.src，地址本身拿不到，但 playlist 通常先经 fetch / XHR，在这里能捕获。
  // 候选按 { u, at } 记录捕获时间：自动选择要依赖「最近 / 最像播放列表」
  let captured = [];
  function isPlaylistLike(u) {
    if (!/^https?:/i.test(u) || /^blob:/i.test(u)) return false;
    if (/\.(js|css|json|png|jpe?g|gif|webp|svg|woff2?)(\?|$)/i.test(u)) return false;
    return /m3u8|\.mp4(\?|$)|playlist/i.test(u);
  }
  function remember(raw) {
    if (!raw || typeof raw !== 'string') return;
    if (isGenericVideoSite()) return;   // 通用视频站的流不是「影片来源」，不捕获
    let abs;
    try { abs = new URL(raw, location.href).href; } catch (e) { return; }
    if (!isPlaylistLike(abs)) return;
    const now = Date.now();
    const exist = captured.find((c) => c.u === abs);
    if (exist) {
      exist.at = now;                    // 最近出现时间更新，但保留首次出现
    } else {
      captured.unshift({ u: abs, at: now, firstAt: now });
      if (captured.length > 12) captured.length = 12;
    }
    // 子帧里的播放器也能把地址交给顶层面板（GM 存储跨帧共享）
    try {
      GM_setValue('captured_play_url', abs);
      GM_setValue('captured_at', Date.now());
    } catch (e) { /* 存储不可用 */ }
    refreshVis();   // 一旦捕获到地址，浮标立刻恢复正常亮度
    maybeAutoSync(); // 已在库的页面换了新的签名地址 → 自动更新来源
  }
  // 捕获方式：只用 PerformanceObserver 观察资源请求，**不包装原生 fetch / XHR**。
  //
  // 包装原生函数会破坏它的 toString 指纹，Cloudflare 这类人机校验会据此判定
  // 「非真实浏览器」，页面卡在「请稍候…」——浮标也就跟着不工作。
  // resource entry 同样能看到 fetch/XHR 发出的请求地址，且不碰任何原生实现。
  try {
    const po = new PerformanceObserver((list) => {
      list.getEntries().forEach((e) => { try { remember(e.name); } catch (err) { /* 忽略 */ } });
    });
    po.observe({ type: 'resource', buffered: true });
    dlog('2 PerformanceObserver 已注册');
  } catch (e) { /* 老浏览器不支持则退回 performance 条目轮询 */ }


  // ---------------------------------------------------------------- 页面信息
  function pageUrl() { return location.href.split('#')[0]; }
  function pageTitle() { return (document.title || '').trim().slice(0, 200); }
  function guessCode() {
    // 只从标题提取：URL 里的目录名 / 文件名（assets、plain11 …）会贡献
    // 「合法格式」的假番号，绕过无番号确认，还会建出错片。
    // 播放页若标题真没有番号，后端会从 page_url / play_url 再兜底。
    const m = (pageTitle() || '').match(/\b([A-Za-z]{2,6})[-_\s]?(\d{2,5})\b/);
    return m ? (m[1] + '-' + m[2]).toUpperCase() : '';
  }
  function guessSiteId() {
    try {
      const u = new URL(pageUrl());
      for (const k of ['id', 'vid', 'video_id', 'v', 'aid', 'cid', 'av']) {
        const v = u.searchParams.get(k);
        if (v && /^[\w-]{1,64}$/.test(v)) return v;
      }
      const m = u.pathname.match(/\/(\d{3,12})(?=\/|\.|$)/);
      if (m) return m[1];
    } catch (e) { /* 忽略 */ }
    return '';
  }
  // 地址打分：自动选「最可能能播」的那条，绝大多数时候用户不用选
  function scoreCandidate(c) {
    const u = c.u;
    let s = 0;
    if (/m3u8/i.test(u)) s += 40;
    if (/master|index|playlist|manifest/i.test(u)) s += 8;
    if (/\.mp4(\?|$)/i.test(u)) s += 12;
    // 密钥 / init 段 / 广告 / 雪碧图不是播放地址
    if (/(^|[./])(key|init)(\?|\.|$)|\/ad[s_-]?|beacon|sprite|thumbnails?/i.test(u)) s -= 120;
    // 主视频总是先于「推荐 / 猜你喜欢」的预览加载：首次出现越早越可能是主片。
    // 曾按「越新越高」排序，结果 hover 推荐卡片后预览地址反超主片，
    // 入库绑错了流——播放时显示的是另一部片子。
    s += Math.max(0, 30 - (Date.now() - (c.firstAt || c.at || 0)) / 60000);
    return s;
  }

  function rankedCandidates() {
    const seen = new Map();
    const add = (u, at, from) => {
      if (!u || !isPlaylistLike(u) || seen.has(u)) return;
      seen.set(u, { u: u, at: at || 0, from: from || '' });
    };
    captured.forEach((c) => add(c.u, c.at, 'player'));
    const v = document.querySelector('video');
    if (v) {
      add(v.currentSrc || v.src, Date.now(), 'video');
      const s = v.querySelector && v.querySelector('source');
      if (s) add(s.src, Date.now(), 'video');
    }
    try {
      (performance.getEntriesByType('resource') || []).forEach((r) => add(r.name, 0, 'page'));
    } catch (e) { /* 某些页面禁用 performance */ }
    try {
      const shared = GM_getValue('captured_play_url', '');
      const at = Number(GM_getValue('captured_at', 0)) || 0;
      if (shared && Date.now() - at < 10 * 60 * 1000) add(shared, at, 'frame');
    } catch (e) { /* 忽略 */ }
    const arr = Array.from(seen.values());
    arr.forEach((c) => { c.score = scoreCandidate(c); });
    arr.sort((a, b) => b.score - a.score);
    return arr;
  }

  function allCandidates() { return rankedCandidates().map((c) => c.u); }
  function bestCandidate() { return rankedCandidates()[0] || null; }
  function guessPlayUrl() {
    const manual = String(cfg.manualPlayUrl || '').trim();
    return manual || (bestCandidate() || {}).u || '';
  }
  function movieEvidence() {
    return {
      schema_version: 1,
      page_url: pageUrl(),
      play_url: guessPlayUrl(),
      code: guessCode(),
      title: pageTitle(),
      site: location.hostname.replace(/^www\./, '').toLowerCase(),
      site_id: guessSiteId(),
      confidence: guessPlayUrl() ? 0.7 : 0.5,
    };
  }

  // ---------------------------------------------------------------- 观看采集
  let watch = null;
  let pendingTask = null;
  function watchTask() {
    try { return GM_getValue('watch_task', null); } catch (e) { return null; }
  }
  function claimTask(id) {
    try {
      if (GM_getValue('watch_claimed', '') === id) return false;
      GM_setValue('watch_claimed', id);
      return true;
    } catch (e) { return false; }
  }
  function releaseTask() {
    try { GM_setValue('watch_task', null); GM_setValue('watch_claimed', ''); } catch (e) { /* 忽略 */ }
  }
  function startWatch(task) {
    if (!cfg.autoWatch || watch || !task || !task.id) return;
    const v = document.querySelector('video');
    if (!v) {
      // 播放器可能是动态插入的（SPA），顶层先记下任务等 video 出现；子帧不占坑
      if (isTop) pendingTask = task;
      return;
    }
    // 认领后才开始采集：同一任务只允许一个帧计时，防止双份计入
    if (!claimTask(task.id)) return;
    const started = Date.now();
    watch = {
      movieId: task.movieId, video: v, sessionId: 0,
      dedupeKey: task.dedupeKey,
      started: started, lastTs: 0, watched: 0, segments: [], segStart: null,
      lastReport: 0, timer: 0,
    };
    const onPlay = () => { report('start'); };
    const onPause = () => { flush(); report('pause'); };
    const onEnded = () => { flush(); report('end'); stopWatch(); };
    const onTime = () => {
      const now = v.currentTime;
      const dt = now - watch.lastTs;
      // 只累计真实播放：拖动 / 缓冲 / 跳章 / 后台标签不计入
      if (dt > 0 && dt < 2 && !v.paused && !v.seeking && document.visibilityState === 'visible') {
        watch.watched += dt;
        if (watch.segStart === null) watch.segStart = watch.lastTs;
      } else if (watch.segStart !== null) {
        watch.segments.push([Math.round(watch.segStart), Math.round(now)]);
        watch.segStart = null;
      }
      watch.lastTs = now;
      if (Date.now() - watch.lastReport > HEARTBEAT_MS) flush();
    };
    watch.cleanup = () => {
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('ended', onEnded);
      v.removeEventListener('timeupdate', onTime);
      clearInterval(watch.timer);
    };
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('ended', onEnded);
    v.addEventListener('timeupdate', onTime);
    watch.timer = setInterval(flush, HEARTBEAT_MS);
    report('start');
  }
  function stopWatch() {
    if (!watch) return;
    watch.cleanup();
    watch = null;
    releaseTask();
    pendingTask = null;
  }
  function playerState() {
    const v = watch ? watch.video : document.querySelector('video');
    return {
      position: v ? Number(v.currentTime.toFixed(2)) : 0,
      duration: v && isFinite(v.duration) ? Number(v.duration.toFixed(2)) : 0,
      playing: v ? !v.paused : false,
      watched_sec: watch ? Math.round(watch.watched) : 0,
      segments: watch ? watch.segments.slice(-200) : [],
    };
  }
  function flush() { if (watch) report('heartbeat'); }
  function report(event) {
    if (!watch) return;
    watch.lastReport = Date.now();
    const body = {
      schema_version: 1,
      event: event,
      time: Date.now(),
      dedupe_key: watch.dedupeKey,
      device_id: deviceId,
      source_ref: location.hostname,
      movie_id: watch.movieId,
      session_id: watch.sessionId || undefined,
      player: playerState(),
    };
    api('ingest/events', 'POST', body).then((r) => {
      if (r.ok && r.data && r.data.session_id) watch.sessionId = r.data.session_id;
      if (!r.ok) enqueue('ingest/events', 'POST', body);
      else if (loadQueue().length) flushQueue();
    });
  }

  // 便于在控制台自查（用户在站点里执行 __avm 即可看到脚本是否注入、在哪一帧、
  // 捕获到几个地址、最近一次请求结果），不用猜「到底有没有运行」。
  try {
    window.__avm = {
      version: AVM_VER,
      injected: true,
      top: !!isTop,
      get fab() { return !!dotEl; },
      get captured() { return allCandidates().length; },
      get api() { return lastApi; },
      get cover() { return coverInfo; },
      get fabMode() { return fabMode; },
      get errors() { return avmErrors.slice(); },
    };
  } catch (e) { /* 忽略 */ }

  // ---------------------------------------------------------------- 交互浮卡
  let panel = null;
  function toast(msg) {
    try { GM_notification({ text: 'AVM：' + msg, timeout: 3500 }); } catch (e) { /* 可选权限 */ }
    const tip = document.getElementById('avm-tip');
    if (tip) tip.textContent = msg;
  }
  function refreshPanel() {
    if (!panel) return;
    const $ = (id) => panel.querySelector('#' + id);
    const list = rankedCandidates();
    const best = list[0] || null;
    $('avm-code').textContent = '识别番号：' + (guessCode() || '未识别（可手动补填）');
    const sc = pageScore();
    const sgn = $('avm-signal');
    if (sgn) {
      sgn.textContent = isListPage()
        ? '页面识别：列表 / 搜索页（单击仍会加入当前页面，注意别点错）'
        : (sc >= 40 ? '页面识别：影片 / 播放器页'
          : (sc >= 15 ? '页面识别：可能有播放器（信号偏弱）'
            : '页面识别：普通页面（仍可加入）'));
    }
    const diag = $('avm-diag');
    if (diag) {
      let ver = '?';
      try { ver = (GM_info && GM_info.script && GM_info.script.version) || '?'; } catch (e) { /* 忽略 */ }
      const apiTxt = !lastApi.at
        ? '尚未请求'
        : (lastApi.ok ? ('成功 ' + lastApi.status) : ('失败 ' + (lastApi.status || '无响应')));
      diag.textContent = '脚本 v' + ver + ' · 捕获 ' + list.length + ' 个地址 · 最近请求：' + apiTxt;
    }
    $('avm-current').textContent = best
      ? '推荐：' + mask(best.u) + '（共 ' + list.length + ' 个候选）'
      : '还没捕获到播放地址：先播放几秒，或展开手动填写';
    let html = '';
    list.forEach((c, i) => {
      html += '<option value="' + esc(c.u) + '">' + (i === 0 ? '推荐：' : '') + esc(mask(c.u)) + '</option>';
    });
    if (cfg.manualPlayUrl && !list.some((c) => c.u === cfg.manualPlayUrl)) {
      html += '<option value="' + esc(cfg.manualPlayUrl) + '">手动：' + esc(mask(cfg.manualPlayUrl)) + '</option>';
    }
    $('avm-cands').innerHTML = html;
    $('avm-cands').value = cfg.manualPlayUrl || (best ? best.u : '');
    const known = !!(existing && existing.movie_id);
    const openBtn = $('avm-open');
    if (openBtn) {
      openBtn.style.display = known ? 'block' : 'none';
      openBtn.textContent = L10N.openDetail;
    }
    // 已在库时给两个动作，不是二选一：
    //   更新已有作品 = 把当前页面的播放地址写回已有来源（同一条来源时不会重复插入）；
    //   作为新来源加入 = 当前页面来自另一个站点 / 另一个可播页面时，挂成这部作品的新来源。
    // 两者都走 /api/ingest/link，由后端三态（created / source_added / duplicate）决定结果。
    const linkBtn = $('avm-link');
    if (linkBtn) linkBtn.textContent = known ? L10N.linkUpdate : L10N.linkNew;
    const addBtn = $('avm-addsrc');
    if (addBtn) {
      addBtn.style.display = known ? 'block' : 'none';
      addBtn.textContent = L10N.linkAddSource;
    }
    const confirmBox = $('avm-confirm');
    if (confirmBox) {
      confirmBox.style.display =
        (pendingNoCode && !(existing && existing.movie_id)) ? 'block' : 'none';
    }
  }
  // 统一入库：FAB 单击与面板按钮共用；silent=true 表示失败时自动展开面板
  let adding = false;
  let linkedKey = '';
  async function doLink(silent, extraTip, force, opts) {
    const asSource = !!(opts && opts.asSource);
    if (adding) return { ok: false, busy: true };
    if (!cfg.token) {
      toast('请先填写访问令牌');
      openPanel();
      return { ok: false };
    }
    const ev = movieEvidence();
    // 没有播放地址也照样登记（先记下页面身份，播放地址之后可在面板补填），
    // 这样任何页面都能一键加入；误操作由「8 秒内再点撤销」兜底。
    // 但识别不到番号时必须二次确认——列表页 / 随手一个普通页不该被默默入库。
    if (!ev.code && !force) {
      pendingNoCode = true;
      openPanel();
      refreshPanel();
      toast('未识别到番号：在面板里确认是否仍要加入');
      return { ok: false, needConfirm: true };
    }
    const key = ev.page_url + '|' + ev.play_url;
    // 「作为新来源加入」不吃本地缓存：同一来源必须问一次后端，才能如实提示「已存在」
    if (!asSource && linkedKey === key) {
      toast(L10N.duplicate);
      return { ok: true, dup: true };
    }
    adding = true;
    const r = await api('ingest/link', 'POST', ev);
    adding = false;
    if (!r.ok || !r.data || !r.data.ok) {
      toast('加入失败：' + ((r.data && r.data.detail) || r.status) + '（检查地址与令牌）');
      if (!silent) togglePanel();
      return { ok: false };
    }
    linkedKey = key;
    pendingNoCode = false;
    // 状态统一在成功后落地：↩ 撤销窗口只给「本次新建的影片」，
    // 复用已有来源不提供撤销，避免误删用户已有的来源
    if (r.data.status === 'created' && r.data.source_id) {
      saveLastAdded({ source_id: r.data.source_id, movie_id: r.data.movie_id, at: Date.now() });
    }
    existing = { movie_id: r.data.movie_id };
    applyFabState();
    popFab();
    // 面板开着时立刻按「已在库」刷新按钮（主按钮 → 更新已有作品，并出现「作为新来源加入」）
    if (panel && panel.style.display !== 'none') refreshPanel();
    // 三种结果分别提示：新建作品 / 已作为新来源加入（带来源总数）/ 重复已存在
    let msg;
    if (r.data.status === 'created') msg = L10N.created(r.data.movie_id);
    else if (r.data.status === 'source_added') msg = L10N.added(r.data.source_count || 0);
    else if (r.data.play_url_updated) msg = L10N.updated;
    else msg = L10N.duplicate;
    toast(msg + (r.data.scrape === 'queued' ? L10N.scraping : '') + (extraTip || ''));
    // 广播采集任务：顶层 / 子帧谁有播放器谁计时，dedupeKey 全局一致，
    // 服务端按它幂等复用同一场次（不会重复计时）
    const task = {
      id: String(Date.now()) + Math.random().toString(36).slice(2, 6),
      movieId: r.data.movie_id,
      dedupeKey: deviceId + ':m' + r.data.movie_id + ':' + Date.now(),
    };
    try { GM_setValue('watch_task', task); } catch (e) { /* 忽略 */ }
    startWatch(task);
    return { ok: true, data: r.data };
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'avm-companion';
    Object.assign(panel.style, {
      position: 'fixed', right: '16px', bottom: '16px', zIndex: '2147483000',
      width: '320px', font: '13px/1.5 system-ui, sans-serif',
      background: '#1f2430', color: '#e8eaf0', borderRadius: '10px',
      boxShadow: '0 8px 28px rgba(0,0,0,.35)', padding: '12px', display: 'none',
    });
    panel.innerHTML = `
      <div style="font-weight:600;margin-bottom:6px">片匣 AVM</div>
      <div id="avm-confirm" style="display:none;background:#3d3320;border:1px solid #8a6d3b;border-radius:6px;padding:8px;margin-bottom:8px;font-size:12px">
        <b>未识别到番号</b>——仍要加入当前页面吗？<br>
        <span style="opacity:.75">会先登记页面身份，番号与播放地址之后可在面板补填。</span>
        <button id="avm-confirm-yes" style="display:block;width:100%;margin-top:6px;padding:6px;font-weight:600;cursor:pointer">仍要加入</button>
      </div>
      <div id="avm-code" style="opacity:.75;word-break:break-all;margin-bottom:4px"></div>
      <div id="avm-signal" style="opacity:.6;font-size:11px;margin-bottom:4px"></div>
      <div id="avm-diag" style="opacity:.55;font-size:11px;margin-bottom:8px"></div>
      <button id="avm-open" style="display:none;width:100%;padding:8px;margin-bottom:6px;font-weight:600;cursor:pointer;background:#1f9d55;color:#fff;border:0;border-radius:6px">已在片库 · 打开 AVM 详情</button>
      <button id="avm-link" style="width:100%;padding:8px;font-weight:600;cursor:pointer">立即加入片库</button>
      <button id="avm-addsrc" style="display:none;width:100%;padding:8px;margin-top:6px;font-weight:600;cursor:pointer;box-sizing:border-box;background:transparent;color:inherit;border:1px solid #4a5568;border-radius:6px">作为新来源加入本作品</button>
      <!-- 已在库时：主按钮变「更新已有作品」，并额外出现「作为新来源加入本作品」（由 refreshPanel 控制） -->

      <div id="avm-current" style="opacity:.6;font-size:11px;word-break:break-all;margin-top:6px"></div>
      <div id="avm-tip" style="margin-top:6px;opacity:.85;word-break:break-all"></div>
      <details style="margin-top:10px">
        <summary style="cursor:pointer;opacity:.75;font-size:12px">地址 / 服务器设置</summary>
        <div style="opacity:.8;margin:8px 0 4px">播放地址（可手动粘贴 m3u8 / 直链）</div>
        <select id="avm-cands" style="width:100%;margin-bottom:6px;box-sizing:border-box"></select>
        <input id="avm-play" placeholder="留空则自动选择" style="width:100%;margin-bottom:6px;box-sizing:border-box">
        <input id="avm-url" placeholder="AVM 地址" style="width:100%;margin-bottom:6px;box-sizing:border-box">
        <input id="avm-token" type="password" placeholder="访问令牌 / avm_ 专用令牌" style="width:100%;margin-bottom:6px;box-sizing:border-box">
        <label style="display:block;opacity:.8"><input id="avm-auto" type="checkbox"> 加入后自动记录观看时长</label>
        <label style="display:block;opacity:.8;margin-top:6px"><input id="avm-autosync" type="checkbox"> 已在库的页面自动更新播放地址（签名过期时）</label>
        <button id="avm-match" style="width:100%;margin-top:8px;cursor:pointer">查询状态</button>
        <div style="margin-top:8px;opacity:.5;font-size:11px">主动点击后上报；已在库页面仅自动同步播放地址（只更新、不建片、不传正文）。断网心跳会排队补报</div>
      </details>`;
    document.body.appendChild(panel);
    // 点击面板外任意位置关闭：pointerdown 比 click 早，不会与浮标/面板内部
    // 的点击（textarea 选词、按钮）冲突；浮标自身的点击由浮标逻辑处理。
    document.addEventListener('pointerdown', (e) => {
      if (!panel || panel.style.display === 'none') return;
      const t = e.target;
      if (!(t instanceof Node)) return;
      if (panel.contains(t)) return;
      // 浮标在 shadow 里：contains 查不到，用根节点判断
      if (dotEl && (t === dotEl || (t.getRootNode && t.getRootNode() === dotEl.getRootNode()))) return;
      if (hostEl && (t === hostEl || (t.getRootNode && t.getRootNode() === hostEl.getRootNode()))) return;
      panel.style.display = 'none';
    });
    const $ = (id) => panel.querySelector('#' + id);
    $('avm-url').value = cfg.baseUrl;
    $('avm-token').value = cfg.token;
    $('avm-auto').checked = cfg.autoWatch;
    $('avm-autosync').checked = cfg.autoSync;
    $('avm-play').value = cfg.manualPlayUrl || '';
    $('avm-url').addEventListener('change', (e) => { cfg.baseUrl = e.target.value.trim(); save(); });
    $('avm-token').addEventListener('change', (e) => { cfg.token = e.target.value.trim(); save(); });
    $('avm-auto').addEventListener('change', (e) => { cfg.autoWatch = e.target.checked; save(); });
    $('avm-autosync').addEventListener('change', (e) => { cfg.autoSync = e.target.checked; save(); });
    $('avm-cands').addEventListener('change', (e) => {
      cfg.manualPlayUrl = e.target.value || ''; save(); refreshPanel();
    });
    $('avm-play').addEventListener('change', (e) => {
      cfg.manualPlayUrl = e.target.value.trim(); save(); refreshPanel();
    });
    $('avm-match').addEventListener('click', async () => {
      refreshPanel();
      const ev = movieEvidence();
      const r = await api('ingest/match?code=' + encodeURIComponent(ev.code) +
        '&url=' + encodeURIComponent(ev.page_url) + '&title=' + encodeURIComponent(ev.title), 'GET');
      if (!r.ok) return toast('查询失败：' + (r.data && r.data.detail || r.status));
      toast(r.data.status === 'existing' ? '库中已有（ID ' + r.data.movie_id + '）' :
        r.data.status === 'ambiguous' ? '存在歧义，请在 AVM 中确认' : '库中还没有，可以加入');
    });
    $('avm-link').addEventListener('click', () => doLink(false));
    $('avm-addsrc').addEventListener('click', () => doLink(false, '', false, { asSource: true }));
    $('avm-open').addEventListener('click', () => {
      if (existing && existing.movie_id) openInAvm(existing.movie_id);
    });
    $('avm-confirm-yes').addEventListener('click', async () => {
      pendingNoCode = false;
      await doLink(true, '', true);   // 强制：用户已在确认框里点头
      refreshPanel();
    });
    return panel;
  }
  function togglePanel() {
    const p = ensurePanel();
    p.style.display = p.style.display === 'none' ? 'block' : 'none';
    if (p.style.display !== 'none') refreshPanel();
  }

  /** 确保面板打开（无番号确认 / 失败提示场景用，不做开关切换） */
  function openPanel() {
    const p = ensurePanel();
    p.style.display = 'block';
    refreshPanel();
  }

  let dotEl = null;
  let hostEl = null;         // shadow 宿主（fixed 定位与层叠的载体）
  // existing / 同步状态已提前到 IIFE 顶部（见上）
  let pendingNoCode = false; // 未识别到番号：等用户在面板里确认是否加入
  let suppressClick = false; // 拖动结束后抑制这一次 click

  /** 查一下当前页面是否已在库（有结果就把浮标改成「✓」并可直接跳 AVM） */
  async function checkExisting() {
    // 不依赖浮标：即使站点样式让浮标没挂上，自动续期仍要照常工作
    if (!cfg.token) return;
    const ev = movieEvidence();
    if (!ev.code && !ev.page_url) return;
    try {
      const r = await api('ingest/match?code=' + encodeURIComponent(ev.code || '') +
        '&url=' + encodeURIComponent(ev.page_url || '') +
        '&title=' + encodeURIComponent(ev.title || ''));
      existing = (r.ok && r.data && r.data.status === 'existing' && r.data.movie_id)
        ? { movie_id: r.data.movie_id } : null;
    } catch (e) { existing = null; }
    applyFabState();
    maybeAutoSync();   // 页面加载时若已捕获到地址（或库中地址已过期）也同步一次
  }

  // ---------------------------------------------------------------- 自动续期
  // 带时间戳签名的站点（jable 等）几小时就换一次 m3u8。页面已在库时，把新
  // 捕获到的地址交给 /ingest/sync 更新来源：只更新、不新建，不传页面正文。
  async function maybeAutoSync() {
    try {
      if (!cfg.autoSync || syncing || !cfg.token || !existing || !existing.movie_id) return;
      const u = guessPlayUrl();
      if (!u) return;
      const key = pageUrl() + '|' + u;
      const now = Date.now();
      if (key === lastSyncKey || now - lastSyncAt < 15000) return;  // 同地址只发一次 / 15s 节流
      lastSyncKey = key;
      lastSyncAt = now;
      syncing = true;
      const r = await api('ingest/sync', 'POST', {
        page_url: pageUrl(),
        play_url: u,
        code: guessCode(),
        title: document.title || '',
        site: location.hostname,
        confidence: 0.7,
      });
      if (!r.ok || !r.data || r.data.status !== 'updated') return;
      // 轻反馈：不弹窗、不打断播放；同时把状态标回可播（浮标本来就是绿色 ✓）
      toast('播放地址已自动更新');
      existing = { movie_id: r.data.movie_id || existing.movie_id };
      applyFabState();
    } catch (e) { /* 网络失败静默：下次捕获到地址会再试 */ }
    finally { syncing = false; }
  }

  function openInAvm(movieId) {
    const base = String(cfg.baseUrl || '').replace(/\/$/, '');
    if (!base || !movieId) return;
    const url = base + '/#/movie/' + movieId;
    // 必须在用户手势里同步打开；延时后调用会被浏览器当弹窗拦截。
    // 注意不能传 'noopener' 特性：那样 window.open 返回 null，会被误判成
    // 「被拦截」而再执行 location.href，结果是新标签 + 当前页各跳一次。
    const w = window.open(url, '_blank');
    if (w) {
      try { w.opener = null; } catch (e) { /* 跨域时忽略 */ }
    } else {
      location.href = url;   // 真的被拦截时才退化为当前页打开
    }
  }

  const UNDO_MS = 8000;   // 入库后的撤销窗口

  function loadLastAdded() {
    try { lastAdded = GM_getValue('last_added', null); } catch (e) { lastAdded = null; }
    if (lastAdded && (!lastAdded.at || Date.now() - lastAdded.at >= UNDO_MS)) lastAdded = null;
  }

  function saveLastAdded(item) {
    lastAdded = item;
    try { GM_setValue('last_added', item); } catch (e) { /* 忽略 */ }
  }

  function clearLastAdded() {
    lastAdded = null;
    try { GM_setValue('last_added', null); } catch (e) { /* 忽略 */ }
  }

  /** 图标即承诺：A = 点击会加入；↩ = 点击会撤销；✓ = 点击会打开 AVM */
  function applyFabState() {
    if (!dotEl) return;
    dotEl.style.transition = 'transform .12s ease, background .18s ease';
    if (lastAdded && Date.now() - lastAdded.at < UNDO_MS) {
      const left = Math.ceil((UNDO_MS - (Date.now() - lastAdded.at)) / 1000);
      dotEl.textContent = '↩';
      dotEl.style.background = '#1f9d55';
      dotEl.title = '已加入 · ' + left + ' 秒内单击可撤销；右键 / 长按：面板';
    } else if (existing && existing.movie_id) {
      dotEl.textContent = '✓';
      dotEl.style.background = '#1f9d55';
      dotEl.title = '已在片库（' + existing.movie_id + '）· 单击打开 AVM；右键 / 长按：面板';
    } else {
      dotEl.textContent = 'A';
      dotEl.style.background = '#6c5ce7';
      dotEl.title = '单击加入片库（' + Math.round(UNDO_MS / 1000) + ' 秒内可撤销）；右键 / 长按：面板';
    }
  }

  /**
   * 常见通用视频站：它们的播放器与媒体流和「影片资料站」是两回事，
   * 在这些站上误加到 AVM 的概率很高，一律不参与一键入库，也不捕获其流地址。
   */
  const SKIP_HOSTS = /(?:^|\.)(youtube\.com|youtu\.be|bilibili\.com|douyin\.com|tiktok\.com|vimeo\.com|dailymotion\.com|netflix\.com|youku\.com|iqiyi\.com|iq\.com|twitter\.com|x\.com|instagram\.com|facebook\.com|twitch\.tv)$/i;

  function isGenericVideoSite() {
    try { return SKIP_HOSTS.test(location.hostname); } catch (e) { return false; }
  }

  /** 列表 / 搜索 / 分类页：误加风险最高，单击只开面板不写库 */
  function isListPage() {
    try {
      const p = location.pathname + location.search;
      if (/\/(list|lists|search|browse|category|categories|tag|tags|genre|genres|index)(\/|$|\?|&)/i.test(p)) return true;
      const cards = document.querySelectorAll(
        'article, .card, .movie-card, .video-card, .item, li.item, .post, .thumb');
      return cards.length >= 12;
    } catch (e) { return false; }
  }

  /**
   * 页面信号评分：判断“这页值不值得显示浮标”。
   *
   * 只看当前 DOM / 自己捕获到的地址，不发请求、不读跨域 iframe 内容：
   *  - 播放器实际请求过 m3u8 / 直链（最强）；
   *  - 页面有 <video>（有尺寸或时长再加分）；
   *  - og:video / ld+json 的 VideoObject；
   *  - URL 像影片页、标题含番号。
   * SPA 的初始 HTML 常是空壳，所以这个评分要定时复算，而不是只看一次源码。
   */
  function pageScore() {
    let s = 0;
    const generic = isGenericVideoSite();
    if (!generic && captured.length) s += 50;
    try {
      if (!generic && GM_getValue('captured_play_url', '')) s += 30;   // 子帧捕获到也算
    } catch (e) { /* 忽略 */ }
    try {
      const v = document.querySelector('video');
      if (v) {
        s += 20;
        if (v.duration > 0 || v.currentTime > 0) s += 20;
        if ((v.videoWidth || 0) >= 320) s += 10;
      }
    } catch (e) { /* 忽略 */ }
    try {
      if (document.querySelector('meta[property="og:video"],meta[property="og:video:url"]')) s += 20;
      const hasVideoObject = Array.prototype.some.call(
        document.querySelectorAll('script[type="application/ld+json"]'),
        (n) => /"VideoObject"/.test(n.textContent || ''));
      if (hasVideoObject) s += 20;
    } catch (e) { /* 忽略 */ }
    if (/\/(v|video|watch|play|av|jav)\//i.test(location.pathname)) s += 15;
    if (/[A-Za-z]{2,6}[-_ ]?\d{2,5}/.test(document.title || '')) s += 25;
    return s;
  }

  /** 通用视频站：只用于「不捕获其流地址」，不影响任何页面都能加入 */

  /** 不隐藏浮标（用户仍可手动使用），只用透明度表达“本页可能没有播放器” */
  function refreshVis() {
    if (!dotEl) return;
    const s = pageScore();
    // 亮度下限提高到 .45：太淡会被当成「没启用」
    dotEl.style.opacity = s >= 40 ? '1' : (s >= 15 ? '.9' : '.8');
  }

  let coverInfo = '';   // 最近一次遮挡自检结果（__avm.cover 可查）

  /**
   * 遮挡自检：站点自己的浮层（顶栏 / 播放器容器）有时 z-index 比浮标还高，
   * 把 34px 的小圆点盖住——用户看到的就是「没有按钮」。右下被挡就自动
   * 换到左下角；两边都被挡只在控制台留下证据（__avm.cover 可查）。
   */
  function checkCovered() {
    if (!isTop || !dotEl || !dotEl.isConnected) return;
    try {
      const r = dotEl.getBoundingClientRect();
      if (!r.width && !r.height) { coverInfo = '浮标尺寸为 0'; return; }
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && !hitIsOurs(hit)) {
        coverInfo = ((hit.tagName || '') + ' ' + (hit.className || hit.id || '')).slice(0, 80);
        console.warn('[AVM] 浮标被遮挡：', coverInfo);
        // 用户手动拖过位置就尊重它——自动换位曾与手动拖动打架：
        // 拖回右下 5 秒后又被搬去左下，看起来就是「浮标又没了」
        let dragged = false;
        try { dragged = !!GM_getValue('fab_pos', null); } catch (e) { /* 忽略 */ }
        if (dragged) return;
        const anchor = fabMode === 'plain' ? dotEl : hostEl;
        if (anchor && anchor.style.right !== 'auto') {
          anchor.style.right = 'auto';
          anchor.style.left = '16px';
          const r2 = anchor.getBoundingClientRect();
          const hit2 = document.elementFromPoint(r2.left + r2.width / 2, r2.top + r2.height / 2);
          if (hit2 && !hitIsOurs(hit2)) {
            hostEl.style.left = 'auto';
            hostEl.style.right = '16px';
            console.warn('[AVM] 左下角也被遮挡，浮标保留在右下');
          } else {
            coverInfo = '';
            console.info('[AVM] 浮标被遮挡，已自动移到左下角');
          }
        }
      } else {
        coverInfo = '';
      }
    } catch (e) { /* 忽略 */ }
  }

  /** 轻量反馈：成功弹一下、失败抖一下（不依赖站点允许 <style>） */
  function popFab() {
    if (!dotEl) return;
    dotEl.style.transform = 'scale(1.18)';
    setTimeout(() => { if (dotEl) dotEl.style.transform = ''; }, 160);
  }

  function shakeFab() {
    if (!dotEl) return;
    let i = 0;
    const timer = setInterval(() => {
      if (!dotEl) { clearInterval(timer); return; }
      dotEl.style.transform = i % 2 ? 'translateX(3px)' : 'translateX(-3px)';
      if (++i > 4) {
        clearInterval(timer);
        dotEl.style.transform = '';
      }
    }, 60);
  }

  /** 浮标拖动：位置记在 GM 存储里，换页/刷新保持 */
  function makeDraggable(fab, host) {
    let drag = null;
    let pressTimer = 0;
    fab.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      const hr = host.getBoundingClientRect();
      drag = { sx: e.clientX, sy: e.clientY, ox: hr.left, oy: hr.top, moved: false, longpress: false };
      fab.style.transform = 'scale(.92)';   // 按下立即反馈
      pressTimer = setTimeout(() => {
        if (drag && !drag.moved) {
          drag.longpress = true;
          suppressClick = true;
          togglePanel();
          setTimeout(() => { suppressClick = false; }, 500);
        }
      }, 650);
      try { fab.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    });
    fab.addEventListener('pointermove', (e) => {
      if (!drag || drag.longpress) return;
      const dx = e.clientX - drag.sx;
      const dy = e.clientY - drag.sy;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 6) return;
      drag.moved = true;
      const x = Math.min(window.innerWidth - 42, Math.max(4, drag.ox + dx));
      const y = Math.min(window.innerHeight - 42, Math.max(4, drag.oy + dy));
      host.style.left = x + 'px';
      host.style.top = y + 'px';
      host.style.right = 'auto';
      host.style.bottom = 'auto';
    });
    const end = () => {
      clearTimeout(pressTimer);
      if (!drag) return;
      const moved = drag.moved;
      const held = drag.longpress;
      drag = null;
      fab.style.transform = '';
      if (held || !moved) return;
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 60);
      const hr = host.getBoundingClientRect();
      try { GM_setValue('fab_pos', { x: Math.round(hr.left), y: Math.round(hr.top) }); } catch (e) { /* 忽略 */ }
    };
    fab.addEventListener('pointerup', end);
    fab.addEventListener('pointercancel', () => { clearTimeout(pressTimer); drag = null; });
  }

  let lastAdded = null;   // { source_id, movie_id, at } —— 撤销窗口内可用

  /** FAB 单击：↩ 撤销 → ✓ 打开 AVM → A 加入片库（图标已经说明点击结果） */
  async function quickAdd() {
    if (lastAdded && Date.now() - lastAdded.at < UNDO_MS) {
      const item = lastAdded;
      clearLastAdded();
      const r = await api('sources/' + item.source_id, 'DELETE');
      existing = null;
      applyFabState();
      toast(r.ok ? '已撤销加入' : '撤销失败：' + ((r.data && r.data.detail) || r.status));
      return;
    }
    if (existing && existing.movie_id) { openInAvm(existing.movie_id); return; }
    const r = await doLink(true, ' · ' + Math.round(UNDO_MS / 1000) + ' 秒内再点可撤销');
    // 状态更新已统一收进 doLink；这里只负责失败抖动
    if (!r || (!r.ok && !r.dup && !r.needConfirm)) {
      applyFabState();
      shakeFab();
    }
  }

  /**
   * 创建（或重新挂载）浮标：默认 Shadow DOM 隔离；若 shadow 挂载后自检
   * 发现不可见（站点脚本 / CSP 干扰），自动降级为普通 div（v0.4 方式）。
   */
  let fabMode = 'shadow';      // 'shadow' | 'plain'
  let fabCheckTimer = 0;

  function clampPos(pos) {
    const w = window.innerWidth || 1920;
    const h = window.innerHeight || 1080;
    return {
      x: Math.min(Math.max(4, pos.x), w - 42),
      y: Math.min(Math.max(4, pos.y), h - 42),
    };
  }

  function applyFabSkin(el) {
    el.style.cssText =
      'pointer-events:auto;all:initial;position:fixed;right:16px;bottom:16px;' +
      'z-index:2147483647;width:34px;height:34px;border-radius:50%;background:#6c5ce7;' +
      'border:2px solid #fff;box-shadow:0 2px 10px rgba(0,0,0,.55);color:#fff;' +
      'text-align:center;line-height:30px;cursor:pointer;font-size:15px;font-weight:700;' +
      'user-select:none;touch-action:none;';
    try {
      const pos = GM_getValue('fab_pos', null);
      if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
        const c = clampPos(pos);
        el.style.right = 'auto';
        el.style.bottom = 'auto';
        el.style.left = c.x + 'px';
        el.style.top = c.y + 'px';
      }
    } catch (e) { /* 存储不可用 */ }
  }

  function bindFab() {
    loadLastAdded();
    applyFabState();
    refreshVis();
    if (dotEl.dataset.avmBound === '1') return;
    dotEl.dataset.avmBound = '1';
    makeDraggable(dotEl, hostEl || dotEl);
    // 单击立即执行（图标已说明结果）；右键 / 长按 = 面板
    dotEl.addEventListener('click', () => { if (!suppressClick) quickAdd(); });
    dotEl.addEventListener('contextmenu', (e) => { e.preventDefault(); togglePanel(); });
    // 弱信号页面浮标偏淡，鼠标移上去时恢复不透明（避免被误认为「没启用」）
    dotEl.addEventListener('mouseenter', () => { if (dotEl) dotEl.style.opacity = '1'; });
    dotEl.addEventListener('mouseleave', () => refreshVis());
  }

  function mountFabPlain() {
    let f = document.getElementById('avm-fab');
    if (!f) {
      f = document.createElement('div');
      f.id = 'avm-fab';
      f.textContent = 'A';
      document.body.appendChild(f);
    }
    dotEl = f;
    hostEl = null;
    applyFabSkin(f);
    bindFab();
  }

  function mountFab() {
    if (fabMode === 'plain') return mountFabPlain();
    let host = document.getElementById('avm-fab-host');
    if (!host || !host.shadowRoot) {
      if (!host) {
        host = document.createElement('div');
        host.id = 'avm-fab-host';
        document.body.appendChild(host);
      }
      const root = host.attachShadow({ mode: 'open' });
      root.innerHTML = '<div id="avm-fab">A</div>';
    }
    // 常驻 body 末尾：同 z-index 时 DOM 顺序靠后者在上，防被后插入元素盖住
    document.body.appendChild(host);
    hostEl = host;   // 命中测试 / 外部点击判断都依赖它（重构时曾丢失导致误降级）
    host.style.cssText =
      'all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
      'width:34px;height:34px;pointer-events:none;background:transparent;';
    try {
      const pos = GM_getValue('fab_pos', null);
      if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
        // 记忆的位置必须钳回当前视口：换显示器 / 缩放 / 改窗口大小后，
        // 旧坐标可能已在屏幕外——这就是「脚本正常但浮标看不见」的常见原因
        const c = clampPos(pos);
        host.style.right = 'auto';
        host.style.bottom = 'auto';
        host.style.left = c.x + 'px';
        host.style.top = c.y + 'px';
      }
    } catch (e) { /* 存储不可用 */ }
    dotEl = host.shadowRoot.getElementById('avm-fab');
    dotEl.style.cssText =
      'pointer-events:auto;width:34px;height:34px;border-radius:50%;background:#6c5ce7;' +
      'border:2px solid #fff;box-shadow:0 2px 10px rgba(0,0,0,.55);color:#fff;' +
      'text-align:center;line-height:30px;cursor:pointer;font-size:15px;font-weight:700;' +
      'user-select:none;touch-action:none;';
    loadLastAdded();
    applyFabState();
    refreshVis();
    if (dotEl.dataset.avmBound !== '1') {
      dotEl.dataset.avmBound = '1';
      makeDraggable(dotEl, host);
      // 单击立即执行（图标已说明结果）；右键 / 长按 = 面板
      dotEl.addEventListener('click', () => {
        if (suppressClick) return;
        quickAdd();
      });
      dotEl.addEventListener('contextmenu', (e) => { e.preventDefault(); togglePanel(); });
      // 弱信号页面浮标偏淡，鼠标移上去时恢复不透明（避免被误认为「没启用」）
      dotEl.addEventListener('mouseenter', () => { if (dotEl) dotEl.style.opacity = '1'; });
      dotEl.addEventListener('mouseleave', () => refreshVis());
    }
    // 挂上 1.5 秒后自检「是否真的看得见」：站点脚本清理 / CSP / 样式破坏
    // 都会让 shadow 方案失效，此时自动降级为普通挂载，并留下诊断记录。
    clearTimeout(fabCheckTimer);
    fabCheckTimer = setTimeout(checkFabVisible, 1500);
  }

  /** 命中测试结果是否属于浮标自身（含 shadow 宿主——宿主区域就是浮标区域） */
  function hitIsOurs(hit) {
    if (!hit) return false;
    if (dotEl && (hit === dotEl || dotEl.contains(hit))) return true;
    if (hostEl && hit === hostEl) return true;
    try {
      if (dotEl && hit.getRootNode && hit.getRootNode() === dotEl.getRootNode()) return true;
    } catch (e) { /* 忽略 */ }
    return false;
  }

  function fabVisibleNow() {
    const el = dotEl && dotEl.isConnected ? dotEl : null;
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
    if (r.left > innerWidth || r.top > innerHeight || r.right < 0 || r.bottom < 0) return false;
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hitIsOurs(hit);
  }

  function checkFabVisible() {
    if (!isTop || !dotEl) return;
    if (fabVisibleNow()) { coverInfo = ''; return; }
    // ① 位置出界（换显示器 / 缩放 / 改窗口大小）是最常见原因：先钳回视口再测
    try {
      const r = dotEl.getBoundingClientRect();
      const c = clampPos({ x: Math.round(r.left), y: Math.round(r.top) });
      const anchor = fabMode === 'plain' ? dotEl : hostEl;
      if (anchor) {
        anchor.style.right = 'auto';
        anchor.style.bottom = 'auto';
        anchor.style.left = c.x + 'px';
        anchor.style.top = c.y + 'px';
      }
      try { GM_setValue('fab_pos', c); } catch (e) { /* 忽略 */ }
    } catch (e) { /* 忽略 */ }
    if (fabVisibleNow()) {
      console.warn('[AVM] 浮标原位置在屏幕外，已自动拉回视口内');
      recordError('浮标位置出界，已钳回视口');
      return;
    }
    // ② 位置没问题还看不见 → 才是样式 / CSP / 站点脚本干扰，降级普通挂载
    if (fabMode !== 'shadow') {
      recordError('浮标仍不可见（普通挂载），详见诊断菜单');
      return;
    }
    recordError('shadow浮标不可见，已降级普通挂载');
    fabMode = 'plain';
    try {
      const h = document.getElementById('avm-fab-host');
      if (h) h.remove();
    } catch (e) { /* 忽略 */ }
    dotEl = null;
    hostEl = null;
    try { mountFabPlain(); } catch (e) { recordError('普通挂载也失败:' + e); }
  }

  function initUi() {
    try {
      if (!isTop) return;            // 子帧只负责捕获地址，不显示浮标
      dlog('3 initUi 进入，isTop=true');
      mountFab();
      dlog('4 mountFab 完成（浮标已 append 到 body）');
    } catch (e) {
      // 任何站点上的初始化异常都留一条线索，但**不再 return**：
      // 浮标挂不上时菜单命令、自动续期、看门狗仍要继续工作。
      console.error('[AVM] 浮标初始化失败：', e);
    }
    setTimeout(() => popFab(), 300);   // 首次出现弹一下，避免被当成没加载
    // 撤销窗口过期后把图标从 ↩ 收回 A / ✓
    setInterval(() => {
      if (lastAdded && Date.now() - lastAdded.at >= UNDO_MS) {
        clearLastAdded();
        applyFabState();
      }
    }, 1000);
    // SPA 的播放器可能晚于首屏插入：定时复算页面信号，决定浮标亮度
    setInterval(refreshVis, 2500);
    // 遮挡自检：站点浮层把浮标盖住时自动换位并留下证据
    setTimeout(checkCovered, 1200);
    setInterval(checkCovered, 5000);
    dlog('5 initUi 完成（事件与定时器已注册）');
    // 存活心跳：若日志止步于 5，说明脚本在后续 10 秒内被页面/扩展终止
    [3, 6, 9].forEach((t) => setTimeout(() => dlog('alive+' + t + 's'), t * 1000));
    // 看门狗：站点脚本或扩展把浮标从 DOM 里删掉时，自动重新挂载
    setInterval(() => {
      if (isTop && !document.getElementById('avm-fab-host') &&
          !document.getElementById('avm-fab')) {
        console.warn('[AVM] 浮标被页面移除，已重新挂载');
        try { mountFab(); popFab(); } catch (e) { /* 忽略 */ }
      }
    }, 3000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUi, { once: true });
  } else {
    initUi();
  }
  // 注入成功即打一条日志：用户在控制台一眼确认「脚本到底跑没跑」，
  // 不用再猜是油猴没启用还是浮标太淡。
  console.info('[AVM] v' + AVM_VER + ' 已注入 ' + location.host +
    (isTop ? '' : '（子帧）') + '；控制台输入 __avm 查看状态');

  // 诊断菜单：不需要打开控制台就能拿到「为什么浮标不显示」的证据。
  // 结果会弹通知并复制到剪贴板，反馈时直接粘贴即可。
  GM_registerMenuCommand('AVM：诊断信息（复制到剪贴板）', () => {
    const fabKind = document.getElementById('avm-fab-host')
      ? 'shadow' : (document.getElementById('avm-fab') ? 'plain' : '未挂载');
    const info = [
      'AVM v' + AVM_VER,
      '站点: ' + location.host,
      '顶层: ' + isTop + ' | readyState: ' + document.readyState,
      '浮标: ' + fabKind + (fabVisibleNow() ? '（可见）' : '（不可见）'),
      '挂载模式: ' + fabMode,
      '已在库: ' + (existing && existing.movie_id ? existing.movie_id : '否/未知'),
      '捕获地址: ' + allCandidates().length,
      '最近API: ' + JSON.stringify(lastApi),
      '错误/拦截: ' + (avmErrors.length ? avmErrors.join(' | ') : '无'),
    ].join('\n');
    try { GM_setClipboard(info, 'text'); } catch (e) { /* 剪贴板不可用时忽略 */ }
    try {
      GM_notification({ text: info, title: 'AVM 诊断（已复制到剪贴板）', timeout: 10 });
    } catch (e) { /* 忽略 */ }
    console.info('[AVM 诊断]\n' + info);
  });

  // 菜单命令与状态检查放在顶层：即使浮标因站点样式挂不上，也仍能手动同步 /
  // 加入 / 打开面板，自动续期同样不依赖浮标。
  GM_registerMenuCommand('AVM：重置浮标位置', () => {
    try { GM_setValue('fab_pos', null); } catch (e) { /* 忽略 */ }
    try {
      const h = document.getElementById('avm-fab-host');
      if (h) h.remove();
      const f = document.getElementById('avm-fab');
      if (f) f.remove();
    } catch (e) { /* 忽略 */ }
    dotEl = null;
    hostEl = null;
    mountFab();
    toast('浮标位置已重置（右下角）');
  });
  GM_registerMenuCommand('AVM：立即同步播放地址', () => {
    lastSyncKey = ''; lastSyncAt = 0;
    checkExisting();   // 重新确认已在库后由 checkExisting 触发一次同步
  });
  GM_registerMenuCommand('AVM：打开 AVM 详情 / 加入片库', () => quickAdd());
  GM_registerMenuCommand('AVM：打开设置面板', () => togglePanel());

  if (isTop) {
    checkExisting();   // 首屏查一次是否已在库（同时触发自动续期）
    let lastUrl = pageUrl();
    setInterval(() => {
      if (pageUrl() !== lastUrl) { lastUrl = pageUrl(); checkExisting(); }
    }, 5000);   // SPA 切页也能跟上
  }

  // 顶层 / 子帧通用：认领采集任务；播放器是动态插入时每秒重试绑定
  setInterval(() => {
    const task = watchTask() || pendingTask;
    if (task && task.id && !watch) startWatch(task);
  }, 1000);
  window.addEventListener('pagehide', () => {
    if (watch) { flush(); report('end'); stopWatch(); }
  });
})();
