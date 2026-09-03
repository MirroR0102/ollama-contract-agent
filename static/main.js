/* Contract AI 网页版 公共 JS：SSE 流式解析 + 通用工具 */

/** 顶部导航高亮当前页 */
function initNav(active) {
  document.querySelectorAll('.navbar nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === active);
  });
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
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    h.onError?.('无法连接服务：' + e.message);
    return;
  }
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

  fetch('/api/files').then(r => r.json()).then(j => {
    const files = (j && j.files) || [];
    if (!files.length) { listEl.innerHTML = '<div class="ctx-empty">尚未入库任何合同，请先到「合同入库」页导入</div>'; return; }
    listEl.innerHTML = files.map(f =>
      `<label class="ctx-item"><input type="checkbox" value="${esc(f.name)}">` +
      `<span class="ctx-fname">📄 ${esc(f.name)}</span>` +
      `<span class="ctx-fdir">${esc(f.dir === 'contracts' ? '演示库' : '上传库')}</span></label>`
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
  // 把初始选择（含从 sessionStorage 恢复的持久化值）同步给外部，避免页面变量仍是空数组
  if (cfg.onChange) cfg.onChange(selected.slice());
  return { get: () => selected.slice() };
}
