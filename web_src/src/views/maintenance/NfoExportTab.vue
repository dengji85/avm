<script setup>
/**
 * 维护 → 导出到媒体目录：把 NFO + 封面按 Emby / Jellyfin / Kodi 的目录结构写到媒体目录。
 *
 * 为什么从设置页搬过来：这是一次「操作」而不是一项「配置」——配置只在首次使用时改，
 * 平时点的是「开始导出」。作为维护工具独立成卡，设置页只留 Emby 服务器联动。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { exportNfoBatch, exportNfoCancel, exportNfoStatus, getConfig, putConfig } from '../../api.js'
import { t } from '../../i18n/index.js'
import { toast } from '../../utils.js'

const NFO_DEFAULTS = {
  enabled: false, write_nfo: true, write_cover: true, write_fanart: true,
  image_naming: 'numbered', nfo_naming: 'match-video', on_conflict: 'skip',
  backup: true, rating_field: 'userrating', path_map: [], notify_emby: true,
}

const cfg = reactive({ ...NFO_DEFAULTS })
const pathMapText = ref('')
const loading = ref(true)
const saving = ref(false)
const exporting = ref(false)
const job = reactive({ running: false, done: 0, total: 0, message: '' })
let timer = null

const pct = computed(() => (job.total ? Math.round((job.done / job.total) * 100) : 0))

async function load() {
  loading.value = true
  try {
    const c = await getConfig()
    Object.assign(cfg, NFO_DEFAULTS, c.nfo_export || {})
    pathMapText.value = (cfg.path_map || []).map((m) => `${m.from} => ${m.to}`).join('\n')
  } catch (e) { /* 读取失败保持默认值 */ } finally { loading.value = false }
}

/* 每行一条「本地路径 => 媒体库路径」，兼容 Docker/网盘映射 */
function parsePathMap(text) {
  return String(text || '').split('\n').map((line) => line.trim()).filter(Boolean)
    .map((line) => {
      const i = line.indexOf('=>')
      if (i < 0) return null
      const from = line.slice(0, i).trim()
      const to = line.slice(i + 2).trim()
      return from && to ? { from, to } : null
    }).filter(Boolean)
}

async function save() {
  saving.value = true
  try {
    await putConfig({ nfo_export: {
      enabled: !!cfg.enabled, write_nfo: !!cfg.write_nfo, write_cover: !!cfg.write_cover,
      write_fanart: !!cfg.write_fanart, image_naming: cfg.image_naming || 'numbered',
      nfo_naming: cfg.nfo_naming || 'match-video', on_conflict: cfg.on_conflict || 'skip',
      backup: !!cfg.backup, rating_field: cfg.rating_field || 'userrating',
      path_map: parsePathMap(pathMapText.value), notify_emby: !!cfg.notify_emby,
    } })
    toast(t('settings.exportSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

async function startExport() {
  if (exporting.value) return
  exporting.value = true
  try {
    const r = await exportNfoBatch({})
    if (!r.total) {
      toast(t('settings.exportNothing'), 'ok')
      exporting.value = false
      return
    }
    job.running = true
    job.total = r.total
    job.done = 0
    job.message = ''
    poll()
  } catch (e) {
    exporting.value = false
    toast(e.message, 'err')
  }
}

/** 停止导出：当前这部写完即停，已导出的不受影响 */
async function stopExport() {
  try {
    await exportNfoCancel()
    toast(t('maint.exportStopping'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function poll() {
  clearTimeout(timer)
  try {
    const s = await exportNfoStatus()
    job.running = !!s.running
    job.done = Number(s.done) || 0
    job.total = Number(s.total) || job.total
    job.message = s.message || ''
    if (job.running) {
      timer = setTimeout(poll, 900)
      return
    }
    exporting.value = false
    toast(s.message || t('settings.exportDone'), 'ok', 5000)
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) {
    exporting.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div class="nfo-tab">
    <section class="panel">
      <div class="panel-head">{{ $t('settings.nfoExport') }} <span class="sub">{{ $t('settings.nfoExportSub') }}</span></div>
      <div class="panel-body">
        <p class="muted">{{ $t('settings.nfoExportHint') }}</p>

      <!-- 主操作：开始导出 + 进度 -->
      <div class="nfo-run">
        <button class="btn primary" :disabled="exporting || loading" @click="startExport">
          {{ exporting
            ? $t('settings.exporting') + (job.total ? ` ${job.done}/${job.total}` : '')
            : $t('settings.exportAllBtn') }}
        </button>
        <button v-if="exporting" class="btn ghost" @click="stopExport">{{ $t('common.cancel') }}</button>
        <span v-if="job.running" class="hint">{{ job.done }}/{{ job.total }} {{ job.message }}</span>
      </div>
      <div v-if="job.total" class="xp-bar"><i :style="{ width: pct + '%' }"></i></div>
      <p v-if="job.message && !job.running" class="hint">{{ job.message }}</p>

      <!-- 导出设置：收进折叠，首次使用才展开 -->
      <details class="nfo-set">
        <summary>{{ $t('maint.exportSettings') }}</summary>
        <div class="nfo-fields">
          <div class="frow">
            <label>{{ $t('settings.nfoExportEnable') }}</label>
            <label class="toggle"><input type="checkbox" v-model="cfg.enabled" /><span class="track"></span></label>
          </div>
          <div class="frow">
            <label>{{ $t('settings.writeNfo') }}</label>
            <label class="toggle"><input type="checkbox" v-model="cfg.write_nfo" /><span class="track"></span></label>
          </div>
          <div class="frow">
            <label>{{ $t('settings.writeCover') }}</label>
            <label class="toggle"><input type="checkbox" v-model="cfg.write_cover" /><span class="track"></span></label>
          </div>
          <div class="frow">
            <label>{{ $t('settings.writeFanart') }}</label>
            <label class="toggle"><input type="checkbox" v-model="cfg.write_fanart" /><span class="track"></span></label>
          </div>
          <div class="field">
            <label>{{ $t('settings.imageNaming') }}</label>
            <select v-model="cfg.image_naming" class="sel">
              <option value="numbered">{{ $t('settings.imageNamingNumbered') }}</option>
              <option value="plain">{{ $t('settings.imageNamingPlain') }}</option>
            </select>
          </div>
          <div class="field">
            <label>{{ $t('settings.nfoNaming') }}</label>
            <select v-model="cfg.nfo_naming" class="sel">
              <option value="match-video">{{ $t('settings.nfoNamingMatch') }}</option>
              <option value="movie">{{ $t('settings.nfoNamingMovie') }}</option>
            </select>
          </div>
          <div class="field">
            <label>{{ $t('settings.onConflict') }}</label>
            <select v-model="cfg.on_conflict" class="sel">
              <option value="skip">{{ $t('settings.conflictSkip') }}</option>
              <option value="merge">{{ $t('settings.conflictMerge') }}</option>
              <option value="overwrite">{{ $t('settings.conflictOverwrite') }}</option>
            </select>
            <span class="hint">{{ $t('settings.conflictHint') }}</span>
          </div>
          <div class="field">
            <label>{{ $t('settings.pathMap') }}</label>
            <textarea v-model="pathMapText" rows="2" :placeholder="$t('settings.pathMapPh')"></textarea>
            <span class="hint">{{ $t('settings.pathMapHint') }}</span>
          </div>
          <div class="frow">
            <label>{{ $t('settings.notifyEmby') }}</label>
            <label class="toggle"><input type="checkbox" v-model="cfg.notify_emby" /><span class="track"></span></label>
          </div>
          <p class="hint">{{ $t('maint.exportEmbyHint') }}</p>
          <div class="nfo-save">
            <button class="btn" :disabled="saving" @click="save">{{ $t('common.save') }}</button>
          </div>
        </div>
      </details>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 与其他维护标签一致：panel 全局样式；单卡内容在页内居中，宽度不超过 720px */
.nfo-tab { display: flex; flex-direction: column; align-items: center; }
.nfo-tab .panel { width: 100%; max-width: 720px; }
.muted { margin: 0; }
.nfo-run { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.xp-bar { height: 6px; border-radius: 3px; background: var(--c-surface-3); overflow: hidden; }
.xp-bar i { display: block; height: 100%; background: var(--c-primary); transition: width .4s; }
.nfo-set { border-top: 1px dashed var(--c-line); padding-top: 12px; }
.nfo-set summary { cursor: pointer; font-size: var(--fs-sm); color: var(--c-text-2); }
.nfo-fields { display: flex; flex-direction: column; gap: 14px; padding-top: 12px; }
.frow { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.frow > label:first-child { font-size: var(--fs-sm); }
.field { display: flex; flex-direction: column; gap: 4px; }
.field > label { font-size: var(--fs-sm); }
.nfo-save { display: flex; justify-content: flex-end; }
</style>
