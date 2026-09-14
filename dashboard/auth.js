/* dashboard/auth.js — P0 统一 Hub token 注入（安全底线）
 * localStorage 持久化 token，提供统一 fetch 封装 + 无 token 输入门。
 * 所有 dashboard 页面引入本文件；token 不落任何日志。
 */
(function () {
  var KEY = 'hub_token';
  var API = window.location.origin;
  var gateShown = false;

  function getToken() {
    return localStorage.getItem(KEY) || localStorage.getItem('wiki_api_key') || '';
  }
  function setToken(t) {
    localStorage.setItem(KEY, t || '');
    if (t) localStorage.removeItem('wiki_api_key');
  }
  function hasToken() { return !!getToken(); }
  function authHeaders(extra) {
    var h = Object.assign({}, extra || {});
    var t = getToken();
    if (t) h['Authorization'] = 'Bearer ' + t;
    return h;
  }
  /* fetch 封装：自动带 token；401 抛 authRequired 错误 */
  function fetchJson(path, opts) {
    opts = opts || {};
    return fetch(API + path, Object.assign({}, opts, {
      headers: authHeaders(opts.headers || {})
    })).then(function (r) {
      if (r.status === 401) {
        var e = new Error('AUTH_REQUIRED');
        e.authRequired = true;
        throw e;
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  /* 无 token 输入门：全屏覆盖层，输入后存 localStorage 并刷新 */
  function showGate(msg) {
    if (gateShown) return;
    gateShown = true;
    var d = document.createElement('div');
    d.id = 'hub-token-gate';
    d.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(8,9,10,.92);' +
      'display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif;';
    d.innerHTML =
      '<div style="background:#131416;border:1px solid #2a2d33;border-radius:12px;padding:28px 32px;' +
      'width:420px;max-width:90%;color:#e8eaed;box-shadow:0 12px 40px rgba(0,0,0,.5)">' +
      '<h2 style="margin:0 0 6px;font-size:18px">Hub 连接令牌</h2>' +
      '<p style="margin:0 0 16px;font-size:13px;color:#9aa0a6">' + (msg || '此页面需要 Hub 令牌才能加载数据。') +
      '<br>令牌保存在本机浏览器，不会上传。</p>' +
      '<input id="hub-token-input" type="password" placeholder="粘贴 hub_token 或 Agent API Key" ' +
      'style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #3a3d42;' +
      'background:#0d0e10;color:#e8eaed;font-size:14px;outline:none">' +
      '<button id="hub-token-save" style="margin-top:14px;width:100%;padding:10px;border-radius:8px;border:none;' +
      'background:#5e6ad2;color:#fff;font-size:14px;font-weight:600;cursor:pointer">保存并加载</button>' +
      '</div>';
    document.body.appendChild(d);
    var input = d.querySelector('#hub-token-input');
    input.focus();
    d.querySelector('#hub-token-save').addEventListener('click', function () {
      var t = input.value.trim();
      if (!t) return;
      setToken(t);
      location.reload();
    });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') d.querySelector('#hub-token-save').click();
    });
  }
  /* 页面启动守卫：无 token 则显示输入门并返回 false，调用方应停止加载数据 */
  function guard() {
    if (hasToken()) return true;
    showGate();
    return false;
  }
  /* 处理 fetch 链里的 401：捕获后显示门 */
  function handleAuthError(err) {
    if (err && err.authRequired) { showGate(); return true; }
    return false;
  }

  window.DashAuth = {
    getToken: getToken,
    setToken: setToken,
    hasToken: hasToken,
    authHeaders: authHeaders,
    fetchJson: fetchJson,
    showGate: showGate,
    guard: guard,
    handleAuthError: handleAuthError
  };
})();
