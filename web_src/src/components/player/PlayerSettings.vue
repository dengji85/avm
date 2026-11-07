<script setup>
import { ref } from 'vue'

/**
 * 播放器「设置」统一面板：外壳 + 一级标签（轨道/同步/显示/方向/画质）+ 关闭。
 * 所有高级配置平铺在同一层，不嵌套「分区 → 二级标签」。
 * 内容用作用域插槽嵌入（SubtitleConsole / VideoConsole 按传入的 tab 渲染对应页）。
 * 停在哪个标签记在 localStorage；旧版的分区值（sub / pic）迁移到新值域。
 */
const SETTINGS_TAB_KEY = 'avm.player.settingsTab'
const TABS = [
  { v: 'tracks', label: 'player.scTracks' },
  { v: 'sync', label: 'player.scSync' },
  { v: 'display', label: 'player.scDisplay' },
  { v: 'orient', label: 'player.vcOrient' },
  { v: 'picture', label: 'player.vcPicture' },
]
let saved = ''
try { saved = JSON.parse(localStorage.getItem(SETTINGS_TAB_KEY) || '""') } catch (e) { /* 忽略 */ }
if (saved === 'sub') saved = 'tracks'   // 旧版分区值迁移
if (saved === 'pic') saved = 'orient'
const tab = ref(TABS.some((x) => x.v === saved) ? saved : 'tracks')
function pick(v) {
  tab.value = v
  try { localStorage.setItem(SETTINGS_TAB_KEY, JSON.stringify(v)) } catch (e) { /* 隐私模式忽略 */ }
}

const emit = defineEmits(['close'])
</script>

<template>
  <div class="ps-menu" @click.stop>
    <div class="ps-head">
      <div class="ps-tabs">
        <button v-for="x in TABS" :key="x.v" :class="{ on: tab === x.v }" @click="pick(x.v)">
          {{ $t(x.label) }}
        </button>
      </div>
      <button class="ps-close" :title="$t('common.close')" :aria-label="$t('common.close')"
              @click="emit('close')">✕</button>
    </div>
    <div class="ps-body">
      <slot v-if="tab === 'tracks' || tab === 'sync' || tab === 'display'" name="sub" :tab="tab" />
      <slot v-else name="pic" :tab="tab" />
    </div>
  </div>
</template>

<style scoped>
/* 与 VideoConsole / SubtitleConsole 同一套视觉语言；面板外壳只在这里定义一份 */
.ps-menu {
  position: absolute; bottom: calc(100% + 6px); right: 0; z-index: 30;
  width: 360px; max-width: 88vw; padding: 12px 14px;
  border: 1px solid rgba(255,255,255,.14); border-radius: 10px;
  background: rgba(17,17,20,.96); color: #eef0f4;
  box-shadow: 0 12px 36px rgba(0,0,0,.6);
  font-size: 13px; line-height: 1.5; text-align: left;
  animation: ps-pop .16s ease-out;
}
@keyframes ps-pop { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

.ps-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,.1); }
.ps-tabs { flex: 1; display: flex; gap: 2px; padding: 2px; border-radius: 8px; background: rgba(255,255,255,.09); }
.ps-tabs button { flex: 1; padding: 6px 2px; border: 0; border-radius: 6px; background: transparent; color: #b9bcc4; cursor: pointer; font-size: 12px; transition: background .12s, color .12s; white-space: nowrap; }
.ps-tabs button:hover { color: #fff; }
.ps-tabs button.on { background: var(--c-primary, #4c8bf5); color: #fff; font-weight: 500; }
.ps-close { flex: 0 0 auto; width: 26px; height: 26px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: #b9bcc4; cursor: pointer; font-size: 13px; line-height: 1; }
.ps-close:hover { background: rgba(255,255,255,.14); color: #fff; }

.ps-body { max-height: 60vh; overflow: auto; padding-right: 2px; }
.ps-body::-webkit-scrollbar { width: 6px; }
.ps-body::-webkit-scrollbar-thumb { border-radius: 3px; background: rgba(255,255,255,.2); }
</style>
