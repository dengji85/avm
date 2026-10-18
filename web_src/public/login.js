/**
 * 登录页脚本（CSP 禁止内联脚本，所以单独成文件）。
 *
 * 页面由服务端渲染，样式与锁屏页 / SPA 遮挡层一致（同一套 .pv-* 规格）。
 * 本脚本负责：
 *   1) 跟随应用主题（localStorage 里的 avm.prefs.v2.theme）
 *   2) 明文 / 密文切换
 *   3) 两步验证：令牌通过后（need_totp）在同一张卡片里切到验证码，不刷新页面
 *   4) 登录成功整页跳回 `/`，由服务端判断是否还需要别的步骤
 */
(function () {
  'use strict'

  var script = document.querySelector('script[src="/login.js"]')
  var d = (script && script.dataset) || {}
  var msg = {
    needToken: d.msgNeedToken || '请输入访问令牌',
    needCode: d.msgNeedCode || '请输入验证码',
    network: d.msgNetwork || '网络错误',
    show: d.msgShow || '显示输入内容',
    hide: d.msgHide || '隐藏输入内容'
  }
  var text = {
    token: {
      title: d.titleToken || '', desc: d.descToken || '',
      ph: d.phToken || '', btn: d.btnToken || ''
    },
    totp: {
      title: d.titleTotp || '', desc: d.descTotp || '',
      ph: d.phTotp || '', btn: d.btnTotp || ''
    }
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
  var icon = document.getElementById('icon')
  var title = document.getElementById('title')
  var desc = document.getElementById('desc')
  var back = document.getElementById('back')

  var step = 'token'   // token | totp
  var token = ''       // 第一步验证通过后暂存，第二步一起提交
  var rememberEl = document.getElementById('remember')
  var remember = false // 「记住此设备 30 天」勾选状态（跨两步保持）

  function setErr(t) { err.textContent = t || '' }

  function render() {
    var t = text[step]
    if (t.title) title.textContent = t.title
    if (t.desc) desc.textContent = t.desc
    if (t.ph) input.placeholder = t.ph
    if (t.btn) btn.textContent = t.btn
    icon.textContent = step === 'totp' ? '🔐' : '🔑'
    input.value = ''
    input.type = 'password'
    input.maxLength = step === 'totp' ? 32 : 64
    back.hidden = step !== 'totp'
    setErr('')
    input.focus()
  }

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
    if (!value) { setErr(step === 'token' ? msg.needToken : msg.needCode); return }
    btn.disabled = true
    setErr('')
    try {
      if (step === 'token') remember = !!(rememberEl && rememberEl.checked)
      var body = step === 'token'
        ? { token: value, remember: remember }
        : { token: token, totp: value, remember: remember }
      var res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
      var data = {}
      try { data = await res.json() } catch (e) { /* 非 JSON */ }
      if (!res.ok) {
        setErr((data && data.detail) || ('HTTP ' + res.status))
        return
      }
      if (data && data.need_totp) {
        token = value
        step = 'totp'
        render()
        return
      }
      location.replace('/')
    } catch (e) {
      setErr(msg.network)
    } finally {
      btn.disabled = false
    }
  }

  btn.addEventListener('click', submit)
  back.addEventListener('click', function () {
    step = 'token'
    token = ''
    render()
  })
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit() })
  input.focus()
})()
