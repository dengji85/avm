<script setup>
import { computed } from 'vue'
import { t } from '../../i18n/index.js'

/**
 * 播放器「画面」控制台：旋转 / 翻转 / 缩放（方向类，按影片记忆）+
 * 亮度 / 对比度 / 饱和度 / 色相 / 锐化（画质类，全局偏好）。
 *
 * 全部是显示层调整：CSS transform + filter（锐化用 SVG 卷积核），实时生效、
 * 不转码不碰视频文件，除锐化外都是 GPU 合成，性能开销可以忽略。
 * 当前显示哪一页由外层 PlayerSettings 的统一标签控制（受控 tab）。
 */
const props = defineProps({
  modelValue: { type: Object, required: true },
  tab: { type: String, default: 'orient' },   // 'orient' | 'picture'
})
const emit = defineEmits(['update:modelValue'])

function set(key, val) {
  emit('update:modelValue', { ...props.modelValue, [key]: val })
}
const n = (key) => Number(props.modelValue[key] || 0)

/* 旋转循环：0 → 90 → 180 → 270 → 0。270° 显示成 -90° 更符合直觉 */
function rotate() {
  set('rot', (n('rot') + 90) % 360)
}
const rotLabel = computed(() => {
  const r = n('rot')
  return r === 0 ? '0°' : r === 270 ? '-90°' : r + '°'
})

const oriented = computed(() => n('rot') || props.modelValue.flipH || props.modelValue.flipV)
function resetOrient() {
  emit('update:modelValue', { ...props.modelValue, rot: 0, flipH: false, flipV: false })
}

const adjusted = computed(() => n('zoom') !== 1 || n('bright') !== 100 || n('contrast') !== 100
  || n('sat') !== 100 || n('hue') !== 0 || n('sharpen') > 0)
function resetPicture() {
  emit('update:modelValue', {
    ...props.modelValue,
    zoom: 1, bright: 100, contrast: 100, sat: 100, hue: 0, sharpen: 0,
  })
}

/* 画质滑块行配置：范围与步进不同，集中声明避免模板里写死五份。
   zoom 属于「方向」页（配合旋转裁切看），这里不再重复出现。 */
const sliders = [
  { key: 'bright', label: 'player.vcBright', min: 0, max: 200, step: 5, fmt: (v) => Math.round(v) + '%' },
  { key: 'contrast', label: 'player.vcContrast', min: 0, max: 200, step: 5, fmt: (v) => Math.round(v) + '%' },
  { key: 'sat', label: 'player.vcSat', min: 0, max: 200, step: 5, fmt: (v) => Math.round(v) + '%' },
  { key: 'hue', label: 'player.vcHue', min: -180, max: 180, step: 5, fmt: (v) => Math.round(v) + '°' },
  { key: 'sharpen', label: 'player.vcSharpen', min: 0, max: 100, step: 10, fmt: (v) => Math.round(v) + '%' },
]
</script>

<template>
  <div class="sc-panel" @click.stop>
    <!-- 方向：旋转 / 翻转 / 放大裁切，按影片记忆 -->
    <div v-if="tab === 'orient'" class="sc-body">
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.vcRotate') }}</span>
        <div class="sc-btns">
          <button class="sc-btn" @click="rotate">↻ {{ rotLabel }}</button>
          <button class="sc-btn" :class="{ on: modelValue.flipH }" @click="set('flipH', !modelValue.flipH)">
            {{ $t('player.vcFlipH') }}
          </button>
          <button class="sc-btn" :class="{ on: modelValue.flipV }" @click="set('flipV', !modelValue.flipV)">
            {{ $t('player.vcFlipV') }}
          </button>
        </div>
      </div>
      <div class="sc-grid">
        <span class="sc-label">{{ $t('player.vcZoom') }}</span>
        <input class="sc-range" type="range" min="0.5" max="2" step="0.05"
               :value="n('zoom')" @input="set('zoom', Number($event.target.value))" />
        <span class="sc-val">{{ n('zoom').toFixed(2) }}×</span>
      </div>
      <button class="sc-btn sc-full" :disabled="!oriented" @click="resetOrient">
        {{ $t('player.vcResetOrient') }}
      </button>
      <div class="sc-hint">{{ $t('player.vcOrientHint') }}</div>
    </div>

    <!-- 画质：亮度 / 对比度 / 饱和度 / 色相 / 锐化，全局偏好 -->
    <div v-if="tab === 'picture'" class="sc-body">
      <div v-for="s in sliders" :key="s.key" class="sc-grid">
        <span class="sc-label">{{ $t(s.label) }}</span>
        <input class="sc-range" type="range" :min="s.min" :max="s.max" :step="s.step"
               :value="n(s.key)" @input="set(s.key, Number($event.target.value))" />
        <span class="sc-val">{{ s.fmt(n(s.key)) }}</span>
      </div>
      <button class="sc-btn sc-full" :disabled="!adjusted" @click="resetPicture">
        {{ $t('player.vcResetPicture') }}
      </button>
      <div class="sc-hint">{{ $t('player.vcPictureHint') }}</div>
    </div>
  </div>
</template>

<style scoped>
/* 与 SubtitleConsole 同一套视觉语言（sc-* 是 scoped 的，两边按同参数维护）。
   面板外壳（定位 / 背景 / 阴影 / 动画）由 PlayerSettings 统一提供。 */
.sc-panel { display: flex; flex-direction: column; gap: 12px; }

.sc-body { display: flex; flex-direction: column; gap: 10px; }

/* 滑块行：标签 / 滑条 / 数值 三列对齐 */
.sc-grid { display: grid; grid-template-columns: 64px 1fr 52px; align-items: center; gap: 10px; }
.sc-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sc-label { color: #c3c7cf; }
.sc-btns { flex: 1; display: flex; gap: 6px; flex-wrap: wrap; }

.sc-btn { padding: 6px 11px; min-height: 28px; border: 1px solid rgba(255,255,255,.18); border-radius: 6px; background: rgba(255,255,255,.07); color: #e3e6ec; cursor: pointer; font-size: 12.5px; transition: background .12s; }
.sc-btn:hover:not(:disabled) { background: rgba(255,255,255,.16); }
.sc-btn:disabled { opacity: .4; cursor: default; }
.sc-btn.on { background: var(--c-primary, #4c8bf5); border-color: transparent; color: #fff; }
.sc-btn.sc-full { width: 100%; margin-top: 2px; }

/* 滑条：自绘轨道 + 圆形滑块，深色面板里默认原生样式几乎看不见 */
.sc-range { -webkit-appearance: none; appearance: none; width: 100%; height: 18px; margin: 0; background: transparent; cursor: pointer; }
.sc-range::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: rgba(255,255,255,.25); }
.sc-range::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 14px; height: 14px; margin-top: -5px; border: 0; border-radius: 50%; background: var(--c-primary, #4c8bf5); box-shadow: 0 1px 4px rgba(0,0,0,.5); }
.sc-range:hover::-webkit-slider-thumb { box-shadow: 0 0 0 4px rgba(76,139,245,.25); }
.sc-range::-moz-range-track { height: 4px; border-radius: 2px; background: rgba(255,255,255,.25); }
.sc-range::-moz-range-thumb { width: 14px; height: 14px; border: 0; border-radius: 50%; background: var(--c-primary, #4c8bf5); box-shadow: 0 1px 4px rgba(0,0,0,.5); }

.sc-val { text-align: right; color: #9ec1ff; font-variant-numeric: tabular-nums; font-size: 12px; }

.sc-hint { padding: 8px 10px; border-radius: 6px; background: rgba(255,255,255,.05); color: #a6aab3; font-size: 11.5px; line-height: 1.6; }
</style>
