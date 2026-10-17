/**
 * 锁屏页脚本（CSP 禁止内联脚本，所以单独成文件）。
 *
 * 这个页面由服务端按锁定原因 / 是否设了解锁码 / 浏览器语言渲染，
 * 样式与 SPA 内的遮挡层（PrivacyLock.vue）保持一致；本脚本只负责：
 *   1) 跟随应用主题（localStorage 里的 avm.prefs.v2.theme）
 *   2) 明文 / 密文切换
 *   3) 提交解锁，成功后整页跳回 `/`，由服务端重新判断锁定状态
 */
(function () {
  'use strict'

  var script = document.querySelector('script[src="/lock.js"]')
  var msg = {
    needed: script ? script.dataset.msgNeeded : '请输入解锁码或访问令牌',
    expired: script ? script.dataset.msgExpired : '登录已过期',
    network: script ? script.dataset.msgNetwork : '网络错误',
    show: script ? script.dataset.msgShow : '显示输入内容',
    hide: script ? script.dataset.msgHide : '隐藏输入内容'
  }

  /* 跟随应用主题：SPA 把偏好存在 avm.prefs.v2 里 */
  try {
    var prefs = JSON.parse(localStorage.getItem('avm.prefs.v2') || '{}')
    document.documentElement.setAttribute('data-theme', prefs.theme === 'light' ? 'light' : 'dark')
    document.documentElement.style.colorScheme = prefs.theme === 'light' ? 'light' : 'dark'
  } catch (e) { /* 忽略：保持默认深色 */ }

  var input = document.getElementById('cred')
  var eye = document.getElementById('eye')
  var btn = document.getElementById('go')
  var err = document.getElementById('err')

  function setErr(text) { err.textContent = text || '' }

  if (eye) {
    eye.addEventListener('click', function () {
      var visible = input.type === 'text'
      input.type = visible ? 'password' : 'text'
      var label = visible ? msg.show : msg.hide
      eye.title = label
      eye.setAttribute('aria-label', label)
      eye.textContent = visible ? '👁' : '🙈'
      input.focus()
    })
  }

  async function submit() {
    var value = (input.value || '').trim()
    if (!value) { setErr(msg.needed); return }
    btn.disabled = true
    setErr('')
    try {
      var res = await fetch('/api/privacy/unlock', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: value })
      })
      if (res.ok) { location.replace('/'); return }
      var data = {}
      try { data = await res.json() } catch (e) { /* 非 JSON */ }
      if (res.status === 401 && data && data.code === 'NO_TOKEN') setErr(msg.expired)
      else setErr((data && data.detail) || ('HTTP ' + res.status))
    } catch (e) {
      setErr(msg.network)
    } finally {
      btn.disabled = false
      input.select()
    }
  }

  btn.addEventListener('click', submit)
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit() })
  input.focus()
})()
