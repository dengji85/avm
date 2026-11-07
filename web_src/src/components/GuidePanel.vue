<script setup>
/**
 * 统一的「? 上手指南」面板：起步清单 + 常见问题 + 重跑向导 + 手册入口。
 * 以前这些救命入口分散在设置页底部、扩展组件折叠区和一次性提示条里，
 * 新用户根本不知道去哪找，这里把它们收敛到一处且随时可回看。
 */
import { computed } from 'vue'
import { state } from '../state.js'
import { putConfig } from '../api.js'
import { t } from '../i18n/index.js'
import { confirmDialog, toast } from '../utils.js'
import { checklistHidden, setChecklistHidden, resetGuide } from '../composables/useGuide.js'
import GettingStarted from './GettingStarted.vue'

const MANUAL_URL = 'https://github.com/dengji85/avm/blob/main/docs/%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C.md'

const faqs = computed(() => [1, 2, 3, 4, 5].map((n) => ({
  q: t(`guide.faq${n}`), a: t(`guide.faq${n}A`),
})))

function close() { state.guideOpen = false }

async function rerun() {
  const ok = await confirmDialog(t('guide.rerun'), t('guide.rerunDesc'))
  if (!ok) return
  try {
    await putConfig({ server: { setup_done: false } })
    state.config = state.config || {}
    state.config.server = Object.assign({}, state.config.server, { setup_done: false })
    resetGuide()
    state.onboarded = false
    state.guideOpen = false
    toast(t('guide.rerunDone'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}
</script>

<template>
  <div class="gp-mask" @click.self="close">
    <div class="gp-panel" role="dialog" :aria-label="$t('guide.title')">
      <div class="gp-head">
        <div>
          <h2>{{ $t('guide.title') }}</h2>
          <p class="muted">{{ $t('guide.welcomeSub') }}</p>
        </div>
        <button class="btn tiny ghost" @click="close">✕ {{ $t('guide.closePanel') }}</button>
      </div>

      <div class="gp-body">
        <GettingStarted v-if="!checklistHidden" embedded />
        <div v-else class="gp-hidden">
          <span class="muted">{{ $t('guide.hiddenHint') }}</span>
          <button class="btn tiny" @click="setChecklistHidden(false)">{{ $t('guide.reopen') }}</button>
        </div>

        <section class="gp-faq">
          <h3>{{ $t('guide.faqTitle') }}</h3>
          <details v-for="(f, i) in faqs" :key="i">
            <summary>{{ f.q }}</summary>
            <p>{{ f.a }}</p>
          </details>
        </section>
      </div>

      <div class="gp-foot">
        <button class="btn tiny" @click="rerun">{{ $t('guide.rerun') }}</button>
        <a class="btn tiny ghost" :href="MANUAL_URL" target="_blank" rel="noopener">{{ $t('guide.manual') }}</a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.gp-mask {
  position: fixed; inset: 0; z-index: 950;
  background: var(--c-overlay); backdrop-filter: blur(6px);
  display: flex; justify-content: flex-end;
}
.gp-panel {
  width: min(620px, 100%); height: 100%; overflow: auto;
  display: flex; flex-direction: column;
  background: var(--c-surface); color: var(--c-text);
  border-left: 1px solid var(--c-line); box-shadow: var(--sh-4, 0 8px 32px rgba(0,0,0,.3));
}
.gp-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  padding: 18px 20px 12px; border-bottom: 1px solid var(--c-line);
}
.gp-head h2 { margin: 0 0 4px; font-size: var(--fs-xl); }
.gp-head p { margin: 0; font-size: var(--fs-sm); }
.gp-body { flex: 1; overflow: auto; padding: 16px 20px; }
.gp-hidden {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 12px; border: 1px dashed var(--c-line); border-radius: var(--r-md, 10px);
  font-size: var(--fs-sm);
}
.gp-faq { margin-top: 18px; }
.gp-faq h3 { margin: 0 0 8px; font-size: var(--fs-md); }
.gp-faq details {
  padding: 8px 10px; border-bottom: 1px dashed var(--c-line); font-size: var(--fs-sm);
}
.gp-faq summary { cursor: pointer; color: var(--c-text); }
.gp-faq p { margin: 6px 0 2px; color: var(--c-text-2); line-height: 1.6; }
.gp-foot {
  display: flex; gap: 8px; flex-wrap: wrap;
  padding: 12px 20px; border-top: 1px solid var(--c-line);
}
.btn.tiny {
  display: inline-flex; align-items: center; padding: 5px 10px; font-size: var(--fs-sm);
  border-radius: var(--r-sm, 6px); cursor: pointer;
  background: var(--c-surface-2); color: var(--c-text); border: 1px solid var(--c-line);
  text-decoration: none;
}
.btn.ghost { background: transparent; }
.muted { color: var(--c-text-2); }
</style>
