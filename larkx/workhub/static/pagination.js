// Shared page boundaries and controls. Lists own their data and draft state.
export function pageSlice(items, page, size) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  page = Math.max(1, Math.min(page, pages));
  return {items:items.slice((page-1)*size, page*size), page, pages, total:items.length, page_size:size};
}

export function pager(key, data) {
  const {page, pages, total, page_size:size} = data;
  const start = total ? (page-1)*size+1 : 0;
  return `<nav class="pagination" aria-label="列表分页">
    <span class="page-count" role="status">共 ${total} 条 · ${start}–${Math.min(page*size,total)} 条</span>
    <label>每页 <select data-page-size="${key}" aria-label="每页条数">${[10,20,50].map(n=>`<option value="${n}" ${size===n?'selected':''}>${n}</option>`).join('')}</select></label>
    <div class="page-buttons"><button type="button" class="button secondary" data-page-list="${key}" data-page-direction="previous" data-page="${page-1}" ${page<=1?'disabled':''}>上一页</button>
    <span aria-live="polite">${page} / ${pages}</span>
    <button type="button" class="button secondary" data-page-list="${key}" data-page-direction="next" data-page="${page+1}" ${page>=pages?'disabled':''}>下一页</button></div>
  </nav>`;
}

export function renderPager(container, key, data) {
  const active = document.activeElement;
  const focused = container.contains(active);
  const direction = focused && active.dataset.pageDirection;
  const html = pager(key, data);
  if (container.innerHTML === html) return;
  container.innerHTML = html;
  if (focused) {
    const button = direction && container.querySelector(`[data-page-direction="${direction}"]`);
    (button && !button.disabled ? button : container.querySelector('select')).focus({preventScroll:true});
  }
}
