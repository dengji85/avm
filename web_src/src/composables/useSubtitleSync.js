import { ref, computed } from 'vue'

/**
 * 字幕校准状态：预览只作为 VTT 的查询参数，渲染始终交给后端。
 *
 * 早期版本在浏览器里直接改 cue.startTime 做「即时预览」，实测不可靠：
 * 改完 cue 后浏览器不一定重算 activeCues，暂停时又拿不到 timeupdate，
 * 表现就是「调了没用」或「字幕消失」。成熟播放器（mpv 的 --sub-delay、
 * PotPlayer 的字幕延迟、Subtitle Edit 的 shift）都是「字幕文件 + 延迟参数」，
 * 这里照做：预览 = 换一个带 offset/scale 的 VTT 地址，浏览器原生解析，
 * 时间轴顺序与显示区间都由后端保证。
 */
export function useSubtitleSync() {
  const saved = ref(blank())
  const current = ref(blank())
  const subId = ref('')
  /* 最后一次「后端渲染」用的参数：cue 的时间就是按它生成的。
     拖动时把差量直接写到 cue 上（零延迟），松手后再让后端重渲染一次做校正。 */
  let rendered = { offset_ms: 0, scale: 1 }
  let originals = new WeakMap()

  function blank() {
    return { offset_ms: 0, scale: 1, anchor_sec: 0, pos_y: 0, method: '', confidence: 0 }
  }

  const offsetMs = computed(() => current.value.offset_ms)
  const dirty = computed(() =>
    current.value.offset_ms !== saved.value.offset_ms ||
    current.value.scale !== saved.value.scale ||
    current.value.pos_y !== saved.value.pos_y)

  function load(subIdValue, sync) {
    subId.value = String(subIdValue || '')
    const s = Object.assign(blank(), sync || {})
    saved.value = { ...s }
    current.value = { ...s }
    rendered = { offset_ms: s.offset_ms, scale: s.scale }
    originals = new WeakMap()
  }

  /** 后端刚按 current 渲染完一轮 VTT：记下基准，cue 的时间以它为原点。 */
  function markRendered() {
    rendered = { offset_ms: current.value.offset_ms, scale: current.value.scale }
    originals = new WeakMap()
  }

  /**
   * 把「预览值 − 已渲染值」的差量直接写到 cue 时间上。
   * 拖动滑块时靠它做到零延迟跟随；松手后后端会重渲染一次做校正，
   * 所以这里只求快，不需要考虑累积误差（每次都以 rendered 为原点重算）。
   */
  function applyToTracks(trackList) {
    if (!trackList) return 0
    const rOff = (rendered.offset_ms || 0) / 1000
    const rScale = rendered.scale || 1
    const cOff = (current.value.offset_ms || 0) / 1000
    const cScale = current.value.scale || 1
    if (Math.abs(cOff - rOff) < 1e-9 && Math.abs(cScale - rScale) < 1e-9) return 0
    let n = 0
    for (let i = 0; i < trackList.length; i++) {
      const tt = trackList[i]
      if (!tt || (tt.kind !== 'subtitles' && tt.kind !== 'captions')) continue
      const cues = tt.cues || []
      let changed = 0
      for (let j = 0; j < cues.length; j++) {
        const cue = cues[j]
        if (!cue) continue
        if (!originals.has(cue)) originals.set(cue, [cue.startTime, cue.endTime])
        const [s, e] = originals.get(cue)
        // 关键：scale 是乘性的，必须先把 cue 反解回「原始时间」再按当前参数正向映射。
        // 直接对已渲染时间再乘一次，会在 scale≠1 时整体偏移 (rendered_offset × (scale−1))，
        // 表现为「拖动时看着对、保存后整体错开几秒」。
        try {
          const ns = Math.max(0, ((s - rOff) / rScale) * cScale + cOff)
          const ne = Math.max(ns + 0.1, ((e - rOff) / rScale) * cScale + cOff)
          if (cue.startTime !== ns || cue.endTime !== ne) {
            cue.startTime = ns
            cue.endTime = ne
            changed++
          }
        } catch (err) {
          // 少数浏览器把 cue 时间做成只读：忽略，松手后由后端 VTT 兜底
        }
      }
      n += changed
      // 改完 cue 后浏览器不一定重算 activeCues：同一帧内切一次 mode 强制重排
      if (changed && tt.mode === 'showing') {
        try {
          tt.mode = 'hidden'
          tt.mode = 'showing'
        } catch (err) { /* 忽略 */ }
      }
    }
    return n
  }

  /** 给 VTT 地址追加预览参数（只在有未保存改动时追加）。 */
  function trackUrl(base) {
    if (!base || !dirty.value) return base
    const c = current.value
    const sep = base.indexOf('?') >= 0 ? '&' : '?'
    return base + sep
      + 'offset_ms=' + Math.round(c.offset_ms || 0)
      + '&scale=' + Number(c.scale || 1).toFixed(5)
      + '&anchor=' + Number(c.anchor_sec || 0).toFixed(2)
  }

  function setOffset(ms) {
    current.value = {
      ...current.value,
      offset_ms: Math.round(Math.max(-600000, Math.min(600000, ms))),
      method: 'manual',
    }
  }
  function nudge(ms) { setOffset(current.value.offset_ms + ms) }
  function setScale(v) {
    current.value = {
      ...current.value,
      scale: Math.max(0.9, Math.min(1.1, Number(v) || 1)),
      method: 'manual',
    }
  }
  function setPosY(v) {
    current.value = {
      ...current.value,
      pos_y: Math.max(-40, Math.min(40, Number(v) || 0)),
    }
  }
  function applySuggestion(s) {
    current.value = {
      ...current.value,
      offset_ms: Math.round(s.offset_ms || 0),
      scale: Number(s.scale) || 1,
      anchor_sec: Number(s.anchor_sec) || 0,
      method: s.method || 'auto',
      confidence: Number(s.confidence) || 0,
    }
  }
  function markSaved(result) {
    saved.value = { ...current.value, ...(result || {}) }
    current.value = { ...saved.value }
  }
  function payload() {
    return {
      offset_ms: current.value.offset_ms,
      scale: current.value.scale,
      anchor_sec: current.value.anchor_sec,
      pos_y: current.value.pos_y,
      method: current.value.method || 'manual',
      confidence: current.value.confidence || 0,
    }
  }

  /**
   * 取「当前画面正在显示的那条字幕」在原始时间轴上的起点。
   * 当前的 VTT 是按 current 渲染的，所以用 current 反推回原始时间。
   */
  function activeCueOriginalStart(trackList, now) {
    if (!trackList) return null
    for (let i = 0; i < trackList.length; i++) {
      const tt = trackList[i]
      if (!tt || tt.mode !== 'showing') continue
      const cues = tt.cues || []
      for (let j = 0; j < cues.length; j++) {
        const c = cues[j]
        if (!c) continue
        if (now < c.startTime || now > c.endTime) continue
        // cue 是按 rendered 参数渲染的（或已被拖动改写），按 same 基准反解
        if (originals.has(c)) return originals.get(c)[0]
        const rOff = (rendered.offset_ms || 0) / 1000
        return (c.startTime - rOff) / (rendered.scale || 1)
      }
    }
    return null
  }

  return { saved, current, subId, offsetMs, dirty, load, trackUrl,
           markRendered, applyToTracks, activeCueOriginalStart,
           setOffset, nudge, setScale, setPosY,
           applySuggestion, markSaved, payload }
}
