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

/** 带鉴权的 JSON 请求，失败抛错（附带 status 与 detail 供调用方区分） */
async function apiJSON(url, opts) {
  const r = await apiFetch(url, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) {
    const msg = j && (typeof j.detail === 'string' ? j.detail : j.message);
    const err = new Error(msg || ('请求失败 ' + r.status));
    err.status = r.status;
    err.detail = j && j.detail;
    throw err;
  }
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
  initModelPicker(right);   // 模型引擎切换（本地 ⇄ 云端，全站可用）
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

/**
 * 轻量 Markdown 渲染（安全版：先整体转义再套标签）。
 * 支持：``` 代码块 / `行内码` / **加粗** / # 标题 / - * 或 1. 列表。
 * 供智能体回答等流式文本展示使用（模型常输出 **、-、### 等标记）。
 */
function mdRender(src) {
  const h = s => esc(s)
    .replace(/`([^`\n]+)`/g, (m, c) => '<code>' + c + '</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  const blocks = [];
  const B = '\u0000';
  let s = String(src ?? '').replace(/```([\s\S]*?)```/g, (m, c) => {
    blocks.push(c.replace(/^\n/, '').replace(/\s+$/, ''));
    return B + 'B' + (blocks.length - 1) + B;
  });
  const out = [];
  let list = null;
  const flushList = () => {
    if (list) { out.push('<ul class="md-ul">' + list.map(x => '<li>' + x + '</li>').join('') + '</ul>'); list = null; }
  };
  for (const rawLine of s.split('\n')) {
    const bm = rawLine.match(new RegExp('^' + B + 'B(\\d+)' + B + '$'));
    if (bm) { flushList(); out.push('<pre class="md-pre">' + esc(blocks[+bm[1]]) + '</pre>'); continue; }
    const t = rawLine.trim();
    if (!t) { flushList(); continue; }
    const hm = t.match(/^(#{1,4})\s+(.*)$/);
    if (hm) {
      flushList();
      const lv = Math.min(4, hm[1].length + 2);
      out.push('<div class="md-h md-h' + lv + '">' + h(hm[2]) + '</div>');
      continue;
    }
    if (/^[-*•]\s+/.test(t)) { (list = list || []).push(h(t.replace(/^[-*•]\s+/, ''))); continue; }
    if (/^\d+[.、)]\s+/.test(t)) { (list = list || []).push(h(t.replace(/^\d+[.、)]\s+/, ''))); continue; }
    flushList();
    out.push('<div class="md-p">' + h(t) + '</div>');
  }
  flushList();
  return out.join('');
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
    // 按所属文件夹分组展示（每文件夹一个小标题）
    const groups = new Map();
    files.forEach(f => {
      const k = f.folder_name || '未分组';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(f);
    });
    let html = '';
    groups.forEach((arr, name) => {
      html += `<div class="ctx-fgroup">📁 ${esc(name)}（${arr.length}）</div>`;
      html += arr.map(f =>
        `<label class="ctx-item"><input type="checkbox" value="${esc(f.name)}">` +
        `<span class="ctx-fname">📄 ${esc(f.name)}</span>` +
        `<span class="ctx-fdir">${esc(f.dir === 'contracts' ? '演示' : '上传')}</span></label>`
      ).join('');
    });
    listEl.innerHTML = html;
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
  // 初始化后主动同步一次：让页面在「刷新/重开」后立即恢复上次勾选（如处理对象提示条）
  if (cfg.onChange) cfg.onChange(selected.slice());
  return { get: () => selected.slice() };
}

/* ==================== 模型引擎切换（本地 Ollama ⇄ 云端 API） ==================== */
let MODEL_ST = null;   // /api/settings/model 的最近状态缓存
let mmEl = null;       // 云端设置弹窗 DOM（懒创建）

/** 轻提示（底部浮条） */
let _toastTimer = null;
function toast(msg, ok = true) {
  let el = document.getElementById('toastMsg');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toastMsg';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'show' + (ok ? '' : ' warn');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = ''; }, 2800);
}

function loadModelStatus() {
  return apiJSON('/api/settings/model').then(st => { MODEL_ST = st; return st; });
}

/** 把状态渲染到顶部选择器上（含模型名与是否已配置） */
function applyModelStatus(st) {
  const sel = document.getElementById('modelSelect');
  if (!st || !sel) return;
  sel.querySelector('option[value="local"]').textContent = '🏠 本地 · ' + st.local.model;
  sel.querySelector('option[value="cloud"]').textContent =
    '☁️ 云端 · ' + st.cloud.model + (st.cloud.ready ? '' : '（未配置）');
  sel.title = '云端接口：' + st.cloud.base_url + '（切换对后续请求生效）';
  sel.value = st.provider;
}

/** 挂载模型切换器到用户栏（所有页面生效） */
function initModelPicker(userBar) {
  const wrap = document.createElement('div');
  wrap.className = 'model-bar';
  wrap.innerHTML = `
    <select class="model-select" id="modelSelect" title="选择模型引擎">
      <option value="local">🏠 本地模型</option>
      <option value="cloud">☁️ 云端模型</option>
    </select>
    <button type="button" class="btn btn-ghost btn-sm" id="modelCfgBtn"
            title="云端模型设置（API Key / 模型名）">⚙️</button>`;
  userBar.insertBefore(wrap, userBar.firstChild);
  const sel = wrap.querySelector('#modelSelect');

  loadModelStatus().then(applyModelStatus).catch(() => { /* 未登录等场景忽略 */ });

  sel.addEventListener('change', async () => {
    const want = sel.value;
    try {
      let st = MODEL_ST || await loadModelStatus();
      if (want === 'cloud' && !st.cloud.ready) {
        const saved = await openModelModal();   // 未配置 → 弹设置框
        if (!saved) sel.value = st.provider;    // 取消 → 还原
        return;
      }
      const res = await apiJSON('/api/settings/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: want }),
      });
      MODEL_ST = res; applyModelStatus(res);
      toast(want === 'cloud' ? '已切换到云端模型（联网）' : '已切换到本地模型（离线）');
    } catch (e) {
      toast('切换失败：' + e.message, false);
      if (MODEL_ST) sel.value = MODEL_ST.provider;
    }
  });

  wrap.querySelector('#modelCfgBtn').addEventListener('click', async () => {
    try { if (!MODEL_ST) await loadModelStatus(); } catch (e) { /* ignore */ }
    await openModelModal();
  });
}

/** 云端设置弹窗：填写/清除个人 Key、修改模型名、保存并启用云端 */
function openModelModal() {
  return new Promise(resolve => {
    if (!mmEl) {
      mmEl = document.createElement('div');
      mmEl.className = 'modal-mask hidden';
      mmEl.innerHTML = `
        <div class="modal">
          <h3>☁️ 云端模型设置</h3>
          <div class="mm-meta" id="mmMeta"></div>
          <label>API Key</label>
          <input type="password" id="mmKey" autocomplete="off">
          <label>模型名</label>
          <input type="text" id="mmModel" autocomplete="off">
          <div class="mm-note">⚠️ 联网模式下，检索到的合同片段会发送至云端服务商，请勿用于敏感数据；
            个人 Key 仅保存在本机数据库中、按账号隔离；留空则使用服务器 .env 配置的共享 Key。</div>
          <div class="mm-foot">
            <button type="button" class="btn btn-ghost btn-sm left" id="mmClear">清除个人 Key</button>
            <button type="button" class="btn btn-ghost" id="mmCancel">取消</button>
            <button type="button" class="btn btn-primary" id="mmSave">保存并启用云端</button>
          </div>
        </div>`;
      document.body.appendChild(mmEl);
      mmEl.querySelector('#mmCancel').addEventListener('click', () => close(false));
      mmEl.addEventListener('click', e => { if (e.target === mmEl) close(false); });
      mmEl.querySelector('#mmClear').addEventListener('click', async () => {
        try {
          const res = await apiJSON('/api/settings/model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: '' }),
          });
          MODEL_ST = res; applyModelStatus(res); fill();
          toast('已清除个人 Key（改用服务器共享 Key）');
        } catch (e) { toast('清除失败：' + e.message, false); }
      });
      mmEl.querySelector('#mmSave').addEventListener('click', async () => {
        const key = mmEl.querySelector('#mmKey').value.trim();
        const model = mmEl.querySelector('#mmModel').value.trim();
        const payload = { provider: 'cloud' };
        if (key) payload.api_key = key;
        if (model && (!MODEL_ST || model !== MODEL_ST.cloud.model)) payload.model = model;
        try {
          const res = await apiJSON('/api/settings/model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          MODEL_ST = res; applyModelStatus(res);
          if (!res.cloud.ready) { toast('尚未配置 Key，云端不可用', false); return; }
          toast('已启用云端模型：' + res.cloud.model);
          close(true);
        } catch (e) { toast('保存失败：' + e.message, false); }
      });
    }
    const fill = () => {
      const st = MODEL_ST;
      if (!st) return;
      const keyEl = mmEl.querySelector('#mmKey');
      const modelEl = mmEl.querySelector('#mmModel');
      mmEl.querySelector('#mmMeta').textContent =
        '接口：' + st.cloud.base_url + ' ｜ 当前模型：' + st.cloud.model +
        (st.cloud.has_personal_key ? '（已保存个人 Key）'
          : st.cloud.has_env_key ? '（使用服务器共享 Key）' : '（尚未配置 Key）');
      keyEl.value = '';
      keyEl.placeholder = st.cloud.has_personal_key
        ? '已保存个人 Key：留空保持不变' : 'sk-...（留空则用服务器共享 Key）';
      modelEl.value = '';
      modelEl.placeholder = st.cloud.model;
    };
    const close = ok => { mmEl.classList.add('hidden'); resolve(ok); };
    fill();
    mmEl.classList.remove('hidden');
    mmEl.querySelector('#mmKey').focus();
  });
}
