/* Contract AI 网页版 公共 JS：SSE 流式解析 + 用户认证 + 通用工具 */

/* ==================== 用户认证 ==================== */
const TOKEN_KEY = 'ct_token';

/** 读取本地 token */
function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }

/** 保存 / 清除 token */
function setToken(t) { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); }

/** 页面登录守卫：无 token 跳登录页 */
function requireAuth() {
  if (!getToken()) { location.replace('/login'); return false; }
  return true;
}

/** 组装带 Authorization 的请求头 */
function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const t = getToken();
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}

/** 带鉴权的 fetch：401 自动跳登录页 */
async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    setToken('');
    location.replace('/login');
    throw new Error('未登录或登录已过期');
  }
  return resp;
}

/** 带鉴权的 JSON 请求，失败抛错 */
async function apiJSON(url, opts) {
  const r = await apiFetch(url, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error((j && (j.detail || j.message)) || ('请求失败 ' + r.status));
  return j;
}

/** 在导航栏右侧注入 用户名 + 退出按钮 */
async function initUserBar() {
  const nav = document.querySelector('.navbar');
  if (!nav || nav.querySelector('.user-bar')) return;
  const right = document.createElement('div');
  right.className = 'user-bar';
  right.innerHTML = '<span class="user-name"></span>' +
    '<button type="button" class="btn btn-ghost btn-sm" id="logoutBtn">退出</button>';
  nav.appendChild(right);
  const logoutBtn = right.querySelector('#logoutBtn');
  logoutBtn.addEventListener('click', async () => {
    try { await apiFetch('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    setToken('');
    location.href = '/login';
  });
  try {
    const me = await apiJSON('/api/me');
    right.querySelector('.user-name').textContent = '👤 ' + (me.display_name || me.username);
  } catch (e) {
    right.querySelector('.user-name').textContent = '';
  }
}

/* ==================== 页面基础 ==================== */
/** 顶部导航高亮当前页 */
function initNav(active) {
  document.querySelectorAll('.navbar nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === active);
  });
}

/** 每个页面统一入口：登录守卫 + 导航高亮 + 用户栏（先登录再渲染） */
function initPage(active) {
  if (!requireAuth()) return;
  initNav(active);
  initUserBar();
}

/**
 * 通用 SSE 流式请求：
 *   fetch POST JSON，逐 data: 事件解析后回调 handlers.event(type, payload)
 * @param {string} url
 * @param {object} body
 * @param {object} h  { onOpen?, onEvent(type,payload), onDone?, onError(msg) }
 */
async function streamSSE(url, body, h) {
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
  } catch (e) {
    h.onError?.('无法连接服务：' + e.message);
    return;
  }
  if (resp.status === 401) { setToken(''); location.replace('/login'); h.onError?.('未登录'); return; }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { const j = await resp.json(); detail = j.detail || detail; } catch (e) { /* ignore */ }
    h.onError?.('请求失败(' + resp.status + ')：' + detail);
    return;
  }
  h.onOpen?.();

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          let evt;
          try { evt = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
          if (evt.type === 'done') { h.onDone?.(); continue; }
          if (evt.type === 'error') { h.onError?.(evt.data?.message || '未知错误'); continue; }
          h.onEvent?.(evt.type, evt.data ?? {});
        }
      }
    }
  } catch (e) {
    h.onError?.('读取流失败：' + e.message);
  }
}

/** HTML 转义（防 XSS） */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/** 生成随机会话 id */
function newThreadId() { return 'web_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

/**
 * 挂载「上下文合同范围」选择器（知识库问答页 / 智能体对话页共用）。
 * 点击按钮展开下拉，勾选要作为检索上下文的合同文件；不勾选任何文件 = 全部合同。
 * 选择结果保存在 sessionStorage（storageKey 区分页面），刷新后仍保留。
 * @param {object} cfg { container: Element, storageKey: string, onChange(selected: string[]) }
 *   selected 为空数组表示「全部合同」（不限制）。
 */
function mountCtxPicker(cfg) {
  const holder = cfg.container;
  let selected = [];
  try {
    const saved = JSON.parse(sessionStorage.getItem(cfg.storageKey) || '[]');
    if (Array.isArray(saved)) selected = saved.filter(s => typeof s === 'string');
  } catch (e) { selected = []; }

  holder.innerHTML = `
    <div class="ctx-picker">
      <button type="button" class="ctx-trigger btn btn-ghost" title="选择作为上下文的合同文件">
        📂 <span class="ctx-label"></span><span class="ctx-caret">▾</span>
      </button>
      <div class="ctx-panel hidden">
        <div class="ctx-head">
          <label class="ctx-all"><input type="checkbox" class="ctx-allbox"> 全部合同（默认，不限制）</label>
        </div>
        <div class="ctx-list"><div class="ctx-empty">加载合同清单中…</div></div>
      </div>
    </div>`;

  const trigger = holder.querySelector('.ctx-trigger');
  const panel = holder.querySelector('.ctx-panel');
  const labelEl = holder.querySelector('.ctx-label');
  const allBox = holder.querySelector('.ctx-allbox');
  const listEl = holder.querySelector('.ctx-list');

  function refreshLabel() {
    labelEl.textContent = selected.length === 0 ? '全部合同' : '已选 ' + selected.length + ' 份合同';
  }
  function refreshPanel() {
    allBox.checked = selected.length === 0;
    listEl.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = selected.includes(cb.value); });
  }
  function commit() {
    try { sessionStorage.setItem(cfg.storageKey, JSON.stringify(selected)); } catch (e) { /* ignore */ }
    refreshLabel();
    refreshPanel();  // 同步面板内「全部」与各文件的勾选状态
    if (cfg.onChange) cfg.onChange(selected.slice());
  }

  apiJSON('/api/files').then(j => {
    const files = (j && j.files) || [];
    if (!files.length) { listEl.innerHTML = '<div class="ctx-empty">你的合同库为空，请先到「合同入库」页导入</div>'; return; }
    listEl.innerHTML = files.map(f =>
      `<label class="ctx-item"><input type="checkbox" value="${esc(f.name)}">` +
      `<span class="ctx-fname">📄 ${esc(f.name)}</span>` +
      `<span class="ctx-fdir">${esc(f.dir === 'contracts' ? '演示' : '上传')}</span></label>`
    ).join('');
    refreshPanel();
  }).catch(() => { listEl.innerHTML = '<div class="ctx-empty">加载合同清单失败</div>'; });

  // 展开 / 收起
  trigger.addEventListener('click', e => {
    e.stopPropagation();
    const willOpen = panel.classList.contains('hidden');
    if (willOpen) refreshPanel();
    panel.classList.toggle('hidden', !willOpen);
  });
  // 勾选「全部」→ 清空已选文件
  allBox.addEventListener('change', () => {
    if (allBox.checked) { selected = []; commit(); }
  });
  // 勾选 / 取消单个文件
  listEl.addEventListener('change', e => {
    const cb = e.target;
    if (!cb.matches('input[type=checkbox]')) return;
    const v = cb.value;
    if (cb.checked) { if (!selected.includes(v)) selected.push(v); }
    else selected = selected.filter(s => s !== v);
    commit();
  });
  // 点击外部收起
  document.addEventListener('click', ev => {
    if (!holder.contains(ev.target)) panel.classList.add('hidden');
  });

  refreshLabel();
  return { get: () => selected.slice() };
}
