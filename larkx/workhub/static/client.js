// Transport only: no DOM rendering and no automatic retries of mutations.
export const mutation = (method, data) => ({method, body: JSON.stringify(data)});

export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const readOnly = method === 'GET' || method === 'HEAD';
  const controller = new AbortController();
  // Aborting a mutation cannot undo a message already sent by the server.
  const timer = readOnly ? setTimeout(() => controller.abort(), 20000) : null;
  const headers = new Headers(options.headers);
  headers.set('Content-Type', 'application/json');
  headers.set('X-Workhub', '1');
  try {
    let response;
    try {
      response = await fetch(path, {...options, method, headers, cache: 'no-store', signal: controller.signal});
    } catch (error) {
      if (controller.signal.aborted) throw new Error('读取超时，请稍后刷新。');
      throw new Error(readOnly ? '无法连接工作台，请检查本地服务。' : '连接中断，操作结果尚未确认，请先核对执行记录或当前状态。');
    }
    if (response.status === 204) return null;
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (controller.signal.aborted) throw new Error('读取超时，请稍后刷新。');
      throw new Error(readOnly ? `工作台响应异常（HTTP ${response.status}），请刷新重试。` : '未能读取操作结果，请先核对执行记录或当前状态。');
    }
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : response.status === 422 ? '填写内容不符合要求，请检查后再保存。' : `操作失败（HTTP ${response.status}），请稍后再试。`;
      throw new Error(detail);
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

// Schedule after completion so a slow service never accumulates polling requests.
export function startPolling(refresh, {interval = 5000, enabled = () => true, onError = () => {}} = {}) {
  let stopped = false;
  let timer;
  async function tick() {
    try {
      if (!stopped && enabled()) await refresh();
    } catch (error) {
      onError(error);
    } finally {
      if (!stopped) timer = setTimeout(tick, interval);
    }
  }
  void tick();
  return () => { stopped = true; clearTimeout(timer); };
}
