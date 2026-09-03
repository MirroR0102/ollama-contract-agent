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
