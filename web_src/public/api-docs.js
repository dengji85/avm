/**
 * 片匣 API 文档页（自托管，零外部资源）。
 *
 * 为什么不用 FastAPI 自带的 Swagger UI：它的脚本 / 样式来自 cdn.jsdelivr.net，
 * 既会被本项目的 CSP（script-src 'self'）拦截，国内网络也经常打不开。
 * 这里用原生 JS 读 /api/openapi.json 渲染，纯 http、离线、内网都能用。
 */
(function () {
  'use strict'

  var listEl = document.getElementById('list')
  var metaEl = document.getElementById('meta')
  var qEl = document.getElementById('q')

  var spec = null
  var ops = []          // { method, path, op, tag }

  /* ------------------------------------------------------------ 小工具 */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
  }

  function typeOf(schema) {
    if (!schema) return ''
    if (schema.$ref) return String(schema.$ref).split('/').pop()
    if (schema.anyOf) return schema.anyOf.map(typeOf).filter(Boolean).join(' | ')
    if (schema.type === 'array') return typeOf(schema.items) + '[]'
    var t = schema.type || ''
    if (schema.format) t += '(' + schema.format + ')'
    if (t === 'string' && schema.enum) t += ' ∈ [' + schema.enum.join(', ') + ']'
    return t
  }

  function schemaProps(schema) {
    if (!schema) return { props: [], required: [] }
    if (schema.$ref) {
      var name = String(schema.$ref).split('/').pop()
      var target = (spec.components && spec.components.schemas && spec.components.schemas[name]) || {}
      return { props: Object.keys(target.properties || {}).map(function (k) {
        return { name: k, schema: target.properties[k] }
      }), required: target.required || [], name: name }
    }
    return {
      props: Object.keys(schema.properties || {}).map(function (k) {
        return { name: k, schema: schema.properties[k] }
      }),
      required: schema.required || [],
    }
  }

  function hasQuery(op) {
    return (op.parameters || []).some(function (p) { return p.in === 'query' })
  }

  function pathParams(op) {
    return (op.parameters || []).filter(function (p) { return p.in === 'path' })
  }

  /* ------------------------------------------------------------ 渲染 */
  function detailsHtml(item) {
    var op = item.op
    var parts = []

    if (op.description) {
      parts.push('<div class="hint" style="white-space:pre-wrap">' + esc(op.description) + '</div>')
    }

    var params = op.parameters || []
    if (params.length) {
      parts.push('<div class="sec-title">参数</div><table><thead><tr>' +
        '<th style="width:180px">名称</th><th style="width:70px">位置</th>' +
        '<th style="width:110px">类型</th><th>说明</th></tr></thead><tbody>')
      params.forEach(function (p) {
        parts.push('<tr><td class="mono">' + esc(p.name) +
          (p.required ? ' <span class="req">*</span>' : '') + '</td>' +
          '<td>' + esc(p.in) + '</td><td class="mono">' + esc(typeOf(p.schema)) + '</td>' +
          '<td>' + esc(p.description || '') + '</td></tr>')
      })
      parts.push('</tbody></table>')
    }

    var body = op.requestBody && op.requestBody.content && op.requestBody.content['application/json']
    if (body) {
      var info = schemaProps(body.schema)
      parts.push('<div class="sec-title">请求体' +
        (info.name ? ' · <span class="mono">' + esc(info.name) + '</span>' : '') + '</div>')
      if (info.props.length) {
        parts.push('<table><thead><tr><th style="width:180px">字段</th><th style="width:110px">类型</th><th>说明</th></tr></thead><tbody>')
        info.props.forEach(function (p) {
          var req = info.required.indexOf(p.name) >= 0
          parts.push('<tr><td class="mono">' + esc(p.name) +
            (req ? ' <span class="req">*</span>' : '') + '</td>' +
            '<td class="mono">' + esc(typeOf(p.schema)) + '</td>' +
            '<td>' + esc((p.schema && p.schema.description) || '') + '</td></tr>')
        })
        parts.push('</tbody></table>')
      } else {
        parts.push('<div class="hint">' + esc(typeOf(body.schema) || '自由格式') + '</div>')
      }
    }

    var responses = op.responses || {}
    var codes = Object.keys(responses)
    if (codes.length) {
      parts.push('<div class="sec-title">响应</div><table><thead><tr>' +
        '<th style="width:80px">状态码</th><th>说明</th></tr></thead><tbody>')
      codes.forEach(function (code) {
        parts.push('<tr><td class="mono">' + esc(code) + '</td><td>' +
          esc((responses[code] && responses[code].description) || '') + '</td></tr>')
      })
      parts.push('</tbody></table>')
    }

    // curl 示例（GET 且无 path 参数时可直接发送）
    var sample = location.origin + item.path
    var curl = 'curl -X ' + item.method.toUpperCase() + " '" + sample + "'" +
      " -H 'Accept: application/json'"
    parts.push('<div class="sec-title">调用示例</div><pre class="curl mono">' + esc(curl) + '</pre>')
    parts.push('<div class="row" style="margin-top:8px">' +
      '<button class="btn" data-copy="' + esc(curl) + '">复制 curl</button>')
    if (item.method === 'get' && !pathParams(op).length) {
      parts.push('<button class="btn" data-send="' + esc(item.path) + '">发送请求</button>' +
        (hasQuery(op) ? '<span class="hint">查询参数取文档默认值，可先用「复制 curl」自行调整</span>' : ''))
    }
    parts.push('</div><div class="resp"></div>')

    return '<div class="body">' + parts.join('') + '</div>'
  }

  function render() {
    var q = (qEl.value || '').trim().toLowerCase()
    var groups = {}
    var total = 0

    ops.forEach(function (item) {
      var hay = (item.method + ' ' + item.path + ' ' + (item.op.summary || '') + ' ' +
        (item.op.description || '')).toLowerCase()
      if (q && hay.indexOf(q) < 0) return
      total++
      var tag = item.tag
      if (!groups[tag]) groups[tag] = []
      groups[tag].push(item)
    })

    if (!total) {
      listEl.innerHTML = '<div class="empty">没有匹配的接口</div>'
      return
    }

    var html = ''
    Object.keys(groups).sort().forEach(function (tag) {
      html += '<h2>' + esc(tag) + ' · ' + groups[tag].length + '</h2>'
      groups[tag].forEach(function (item, i) {
        var summary = item.op.summary || ''
        html += '<details class="op" data-method="' + esc(item.method) + '" data-path="' + esc(item.path) + '">' +
          '<summary><span class="m ' + esc(item.method) + '">' + esc(item.method.toUpperCase()) + '</span>' +
          '<span class="path">' + esc(item.path) + '</span>' +
          '<span class="sum">' + esc(summary) + '</span></summary>' +
          detailsHtml(item) + '</details>'
      })
    })
    listEl.innerHTML = html
  }

  /* ------------------------------------------------------------ 事件 */
  listEl.addEventListener('click', function (ev) {
    var copyBtn = ev.target.closest('[data-copy]')
    if (copyBtn) {
      var text = copyBtn.getAttribute('data-copy') || ''
      var done = function () { copyBtn.textContent = '已复制' ; setTimeout(function () { copyBtn.textContent = '复制 curl' }, 1200) }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done)
      } else {
        var ta = document.createElement('textarea')
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        try { document.execCommand('copy') } catch (e) { /* 忽略 */ }
        document.body.removeChild(ta)
        done()
      }
      return
    }

    var sendBtn = ev.target.closest('[data-send]')
    if (sendBtn) {
      var box = sendBtn.closest('.body').querySelector('.resp')
      var path = sendBtn.getAttribute('data-send')
      box.innerHTML = '<div class="hint">请求中…</div>'
      fetch(path, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) {
          return r.text().then(function (t) {
            var pretty = t
            try { pretty = JSON.stringify(JSON.parse(t), null, 2) } catch (e) { /* 非 JSON */ }
            box.innerHTML = '<pre class="curl mono">' + esc('HTTP ' + r.status + '\n' + pretty) + '</pre>'
          })
        })
        .catch(function (e) {
          box.innerHTML = '<div class="err">' + esc(e && e.message ? e.message : '请求失败') + '</div>'
        })
    }
  })

  qEl.addEventListener('input', render)

  /* ------------------------------------------------------------ 启动 */
  fetch('/api/openapi.json', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
    .then(function (r) {
      if (r.status === 401) throw new Error('需要登录：请先在片匣页面登录（本页继承了浏览器的登录会话）')
      if (r.status === 423) throw new Error('内容已锁定：请先在片匣页面解锁遮挡层')
      if (!r.ok) throw new Error('接口定义读取失败（HTTP ' + r.status + '）')
      return r.json()
    })
    .then(function (data) {
      spec = data
      var info = spec.info || {}
      metaEl.textContent = (info.title || '') + ' · v' + (info.version || '?')
      document.title = 'API 文档 · ' + (info.title || '片匣')
      ops = []
      var paths = spec.paths || {}
      Object.keys(paths).sort().forEach(function (p) {
        Object.keys(paths[p]).forEach(function (m) {
          var op = paths[p][m]
          if (!op || typeof op !== 'object' || !op.responses) return
          ops.push({ method: m, path: p, op: op, tag: (op.tags && op.tags[0]) || '其他' })
        })
      })
      render()
    })
    .catch(function (e) {
      metaEl.textContent = '读取失败'
      listEl.innerHTML = '<div class="empty err">' + esc(e && e.message ? e.message : String(e)) + '</div>'
    })
})()
