'use strict';
/* ============================================================================
   SpaceSight // 空间探针 — 前端应用层
   纯原生 JS，无框架、无依赖。分段：
     1. 工具函数   2. 状态与请求   3. 左栏（目标/日志）  4. 目录树
     5. 矩形图     6. 安装台账     7. 检视面板   8. 弹窗   9. 轮询与启动
   ========================================================================== */

/* ────────────────────────────────────────────────────────── 1. 工具函数 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const MB = 1024 * 1024, GB = 1024 * MB, TB = 1024 * GB;

/* 尺寸 → 颜色的"热度"标尺：冷 → 热，全部落在霓虹色域内，避免暗黄发褐 */
// ?demo=1 时用固定的演示数字渲染"系统盘"计量，供文档截图使用（不影响任何真实逻辑）
const DEMO = new URLSearchParams(location.search).get('demo') === '1';
const TIERS = [
  { min: 0, color: '#55708c' },        // 普通
  { min: 64 * MB, color: '#00a8c8' },  // 青
  { min: 256 * MB, color: '#00e8ff' }, // 亮青
  { min: 1 * GB, color: '#5cffc4' },   // 薄荷绿
  { min: 4 * GB, color: '#a86bff' },   // 紫
  { min: 16 * GB, color: '#ff2bd6' },  // 品红
  { min: 64 * GB, color: '#ff3b5c' },  // 高热红
];

function tierOf(size) {
  let t = TIERS[0];
  for (const item of TIERS) if (size >= item.min) t = item;
  return t;
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n <= 0) return '0';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  if (v >= 1000 && i >= 2 && i < units.length - 1) { v /= 1024; i++; }  // 1015 MB → 0.99 GB
  const dec = i <= 1 ? 0 : (v < 10 ? 2 : (v < 100 ? 1 : 0));
  return `${v.toFixed(dec)} ${units[i]}`;
}
const fmtInt = n => (Number(n) || 0).toLocaleString('en-US');
const fmtPct = v => `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const basename = p => {
  const parts = String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
};
const dirname = p => {
  const s = String(p || '').replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  return i > 0 ? s.slice(0, i) : '';
};
function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function fmtTime(ts) {
  const d = ts ? new Date(ts) : new Date();
  if (Number.isNaN(d.getTime())) return '--:--:--';
  return d.toTimeString().slice(0, 8);
}
function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime()) || d.getFullYear() < 1990) return '—';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function nodeIcon(node) {
  if (node.is_dir) return '▣';
  const ext = (node.name || '').split('.').pop().toLowerCase();
  if (['zip', '7z', 'rar', 'iso', 'tar', 'gz', 'xz', 'bz2'].includes(ext)) return '◫';
  if (['exe', 'msi', 'bat', 'cmd', 'app'].includes(ext)) return '◈';
  if (['mp4', 'mkv', 'mov', 'avi', 'flv', 'wmv', 'webm'].includes(ext)) return '▶';
  if (['mp3', 'flac', 'wav', 'm4a', 'ogg'].includes(ext)) return '♪';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'psd', 'bmp'].includes(ext)) return '◨';
  if (['dll', 'sys', 'so', 'lib'].includes(ext)) return '◇';
  if (['doc', 'docx', 'pdf', 'txt', 'md', 'xls', 'xlsx', 'ppt', 'pptx'].includes(ext)) return '▤';
  return '▪';
}
function weightOf(size, max, min) {
  const s = Math.max(size, 1);
  const lo = Math.log(Math.max(min, 1));
  const hi = Math.log(Math.max(max, min * 1.5));
  if (hi <= lo) return 1;
  return clamp((Math.log(s) - lo) / (hi - lo), 0, 1);
}

/* ────────────────────────────────────────────────────────── 2. 状态与请求 */
const S = {
  settings: {},
  threshold: 100 * MB,
  roots: [],
  rootMap: new Map(),
  activeRoot: null,
  view: 'tree',
  totals: {},
  detector: {},
  disk: {},
  events: [],
  maxEventId: 0,
  treeCache: new Map(),   // `${rootId}:${key}` -> {children, hidden, total, truncated}
  expanded: new Set(),
  selected: null,          // {rootId, key, path}
  apps: [],
  appsLoaded: false,
  tmPath: null,           // 矩形图当前下钻的 key
  tmRootId: null,
  booted: false,
  noteTimer: 0,
  searchTimer: 0,
};

async function api(path, body, method) {
  const opt = { method: method || (body ? 'POST' : 'GET'), headers: {} };
  if (body) {
    opt.headers['Content-Type'] = 'application/json; charset=utf-8';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: `HTTP ${res.status}` }; }
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(title, msg, kind = 'info') {
  const color = { install: 'var(--lime)', folder: 'var(--magenta)', bigfile: 'var(--amber)', error: 'var(--red)' }[kind] || 'var(--cyan)';
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span class="tk" style="color:${color}">${esc(kind.toUpperCase())}</span>
    <div class="tb"><div class="tt">${esc(title)}</div><div class="tm">${esc(msg || '')}</div></div>`;
  el.style.borderColor = color;
  $('#toasts').appendChild(el);
  const kill = () => { el.classList.add('out'); setTimeout(() => el.remove(), 320); };
  el.addEventListener('click', kill);
  setTimeout(kill, 6500);
}

function setStatus(msg) { $('#status-msg').textContent = msg; }

/* ────────────────────────────────────────────────────────── 3. 左栏 */
function renderRoots() {
  const box = $('#roots-list');
  const total = S.roots.reduce((a, r) => a + (r.total_bytes || 0), 0);
  const max = Math.max(...S.roots.map(r => r.total_bytes || 0), 1);
  $('#root-count').textContent = S.roots.length;
  if (!S.roots.length) {
    box.innerHTML = `<div class="muted-note" style="padding:10px 4px">还没有追踪目标。<br>点击下方按钮或右上角「＋ 添加追踪」。</div>`;
    return;
  }
  box.innerHTML = S.roots.map(r => {
    const t = tierOf(r.total_bytes || 0);
    const pct = clamp(((r.total_bytes || 0) / max) * 100, 2, 100);
    const scanning = r.status === 'scanning';
    const missing = r.status === 'missing';
    return `<div class="root-card ${S.activeRoot === r.id ? 'active' : ''} ${missing ? 'missing' : ''}"
        data-root="${r.id}" style="--c:${missing ? 'var(--red)' : t.color}">
      <div class="root-top">
        <span class="root-name" title="${esc(r.path)}">${esc(r.label)}</span>
        <span class="root-size">${scanning ? '…' : fmtBytes(r.total_bytes)}</span>
      </div>
      <div class="root-path">${esc(r.path)}</div>
      <div class="root-meta">
        <span class="src-tag src-${esc(r.source)}">${esc(r.source)}</span>
        ${r.is_new ? '<span class="badge-new">NEW</span>' : ''}
        ${scanning ? '<span class="spinner"></span>' : ''}
        ${missing ? '<span class="src-tag" style="color:var(--red)">缺失</span>' : ''}
        <span class="root-bar"><i style="width:${pct}%"></i></span>
        <span class="root-actions">
          <button class="icon-btn" data-act="rescan" title="重新扫描">↻</button>
          <button class="icon-btn" data-act="reveal" title="在资源管理器中打开">⌖</button>
          <button class="icon-btn danger" data-act="remove" title="移出追踪">✕</button>
        </span>
      </div>
    </div>`;
  }).join('');
  $('#metric-total').textContent = fmtBytes(total);
  $('#metric-roots').innerHTML = `${S.roots.length} <small>/ ${fmtInt(S.totals.files || 0)}</small>`;
}

function renderEvents(newOnes = []) {
  const box = $('#events-list');
  if (!S.events.length) {
    box.innerHTML = `<div class="muted-note" style="padding:8px 4px">暂无事件。安装/解压程序启动、目录变化都会记录在这里。</div>`;
    return;
  }
  const fresh = new Set(newOnes.map(e => e.id));
  box.innerHTML = S.events.slice(0, 80).map(e => `
    <div class="event ${fresh.has(e.id) ? 'fresh' : ''}" data-path="${esc(e.path)}">
      <time>${fmtTime(e.ts)}</time>
      <div>
        <div class="ev-label">
          <span class="ev-kind k-${esc(e.kind)}">${esc(e.kind)}</span>
          <b>${esc(e.label || e.kind)}</b>
        </div>
        <div class="ev-msg">${esc(e.message || '')}</div>
        ${e.path ? `<div class="ev-path" title="${esc(e.path)}">${esc(e.path)}</div>` : ''}
      </div>
    </div>`).join('');
}

/* ────────────────────────────────────────────────────────── 4. 目录树 */
function treeKey(rootId, key) { return `${rootId}:${key}`; }

function rootRowHTML(root) {
  const size = root.total_bytes || 0;
  const t = tierOf(size);
  const maxRoot = Math.max(...S.roots.map(r => r.total_bytes || 0), 1);
  const pct = clamp((size / maxRoot) * 100, 1.5, 100);
  const open = S.expanded.has(treeKey(root.id, root.key));
  const scanning = root.status === 'scanning';
  return `<div class="node" data-root="${root.id}" data-key="${esc(root.key)}" data-path="${esc(root.path)}" data-depth="0" data-size="${size}" data-isdir="1">
    <div class="row ${S.selected && S.selected.rootId === root.id && S.selected.key === root.key ? 'sel' : ''}"
         style="--w:${size ? weightOf(size, maxRoot, S.threshold) : 0};--c:${t.color};--pct:${pct}%">
      <div class="row-main" style="padding-left:0">
        <span class="chev ${open ? 'open' : ''}" data-act="toggle">▶</span>
        <span class="n-ico">◆</span>
        <span class="name"><span class="n-txt">${esc(root.label)}</span>
          <span class="src-tag src-${esc(root.source)}">${esc(root.source)}</span>
          ${root.is_new ? '<span class="badge-new">NEW</span>' : ''}
          ${scanning ? '<span class="spinner"></span>' : ''}
        </span>
      </div>
      <div class="bar"><i></i></div>
      <div class="size">${scanning ? '扫描中…' : fmtBytes(size)}</div>
    </div>
    <div class="kids"><div class="kids-inner"></div></div>
  </div>`;
}

function nodeRowHTML(node, rootId, depth, maxSibling, parentSize) {
  const t = tierOf(node.size);
  const pct = clamp((node.size / Math.max(parentSize || maxSibling, 1)) * 100, 1, 100);
  const w = weightOf(node.size, maxSibling, S.threshold);
  const k = treeKey(rootId, node.key);
  const open = S.expanded.has(k);
  const hasKids = node.is_dir && (node.dirs > 0 || node.children_total > 0 || !!node.is_dir);
  const sel = S.selected && S.selected.rootId === rootId && S.selected.key === node.key;
  return `<div class="node" data-root="${rootId}" data-key="${esc(node.key)}" data-path="${esc(node.path)}" data-depth="${depth}" data-size="${node.size}" data-isdir="${node.is_dir ? 1 : 0}">
    <div class="row ${node.is_dir ? '' : 'is-file'} ${sel ? 'sel' : ''}"
         style="--w:${w.toFixed(3)};--c:${t.color};--pct:${pct}%">
      <div class="row-main">
        <span class="chev ${open ? 'open' : ''} ${node.is_dir ? '' : 'empty'}" data-act="toggle">▶</span>
        <span class="n-ico">${nodeIcon(node)}</span>
        <span class="name"><span class="n-txt" title="${esc(node.path)}">${esc(node.name)}</span>
          ${node.flagged ? '<span class="n-flag" title="已标记待清理">⚑</span>' : ''}
          ${node.note ? `<span class="n-note" title="${esc(node.note)}">▤</span>` : ''}
        </span>
      </div>
      <div class="bar"><i></i></div>
      <div class="size">${fmtBytes(node.size)}</div>
    </div>
    <div class="kids"><div class="kids-inner"></div></div>
  </div>`;
}

function renderTree() {
  const tree = $('#tree');
  if (!S.roots.length) { tree.innerHTML = ''; return; }
  tree.innerHTML = S.roots.map(rootRowHTML).join('');
  // 恢复已展开的层级
  for (const root of S.roots) {
    if (S.expanded.has(treeKey(root.id, root.key))) {
      loadChildren(root.id, root.key, $('#tree .node[data-root="' + root.id + '"][data-key="' + cssEsc(root.key) + '"]'));
    }
  }
}

function cssEsc(s) { return String(s).replace(/["\\]/g, '\\$&'); }

async function loadChildren(rootId, key, nodeEl, { silent = false } = {}) {
  if (!nodeEl) return;
  const kids = nodeEl.querySelector(':scope > .kids');
  const inner = kids.querySelector(':scope > .kids-inner');
  const cacheKey = treeKey(rootId, key);
  const cached = S.treeCache.get(cacheKey);
  if (cached) { paintChildren(nodeEl, rootId, cached); return; }
  if (!silent) {
    inner.innerHTML = `<div class="loading-row"><span class="spinner"></span> 读取索引…</div>`;
    openKids(nodeEl, true);
  }
  try {
    const data = await api(`/api/tree?root_id=${rootId}&parent=${encodeURIComponent(key)}&threshold=${Math.round(S.threshold)}&limit=400`);
    S.treeCache.set(cacheKey, data);
    paintChildren(nodeEl, rootId, data);
  } catch (err) {
    inner.innerHTML = `<div class="loading-row" style="color:var(--red)">读取失败：${esc(err.message)}</div>`;
    openKids(nodeEl, true);
  }
}

function paintChildren(nodeEl, rootId, data) {
  const kids = nodeEl.querySelector(':scope > .kids');
  const inner = kids.querySelector(':scope > .kids-inner');
  const parentSize = data.parent ? data.parent.size : 0;
  const children = data.children || [];
  if (!children.length) {
    inner.innerHTML = data.hidden
      ? `<div class="more-row" style="cursor:default">∅ 本层 ${data.hidden} 个项目均小于 ${fmtBytes(S.threshold)}</div>`
      : `<div class="more-row" style="cursor:default">∅ 空目录 / 无子项</div>`;
    openKids(nodeEl, true);
    return;
  }
  const maxSibling = Math.max(...children.map(c => c.size), 1);
  const html = children.map((c, i) =>
    nodeRowHTML(c, rootId, Number(nodeEl.dataset.depth || 0) + 1, maxSibling, parentSize)
      .replace('<div class="node"', `<div class="row-enter" style="animation-delay:${Math.min(i * 10, 260)}ms"><div class="node"`)
      .replace(/<\/div>\s*$/, '</div></div>')
  ).join('');
  const more = data.truncated
    ? `<div class="more-row" data-act="more" data-root="${rootId}" data-key="${esc(data.parent.key)}" data-offset="${(data.offset || 0) + children.length}">＋ 显示更多（共 ${data.total} 项）</div>`
    : '';
  const hidden = data.hidden
    ? `<div class="more-row" style="cursor:default">⋯ 另有 ${data.hidden} 项小于当前阈值，已隐藏</div>`
    : '';
  inner.innerHTML = html + more + hidden;
  openKids(nodeEl, true);
}

function openKids(nodeEl, open) {
  const kids = nodeEl.querySelector(':scope > .kids');
  if (!kids) return;
  const chev = nodeEl.querySelector(':scope > .row .chev');
  if (open) {
    kids.style.height = 'auto';
    if (chev) chev.classList.add('open');
    S.expanded.add(treeKey(nodeEl.dataset.root, nodeEl.dataset.key));
  } else {
    const h = kids.scrollHeight;
    kids.style.height = h + 'px';
    void kids.offsetHeight;
    kids.style.height = '0px';
    if (chev) chev.classList.remove('open');
    S.expanded.delete(treeKey(nodeEl.dataset.root, nodeEl.dataset.key));
  }
}

function toggleNode(nodeEl) {
  if (!nodeEl) return Promise.resolve();
  const openNow = S.expanded.has(treeKey(nodeEl.dataset.root, nodeEl.dataset.key));
  if (openNow) { openKids(nodeEl, false); return Promise.resolve(); }
  return loadChildren(Number(nodeEl.dataset.root), nodeEl.dataset.key, nodeEl);
}

function selectNode(nodeEl, { scroll = false } = {}) {
  if (!nodeEl) return;
  $$('#tree .row.sel').forEach(r => r.classList.remove('sel'));
  const row = nodeEl.querySelector(':scope > .row');
  if (row) row.classList.add('sel');
  const rootId = Number(nodeEl.dataset.root);
  S.selected = { rootId, key: nodeEl.dataset.key, path: nodeEl.dataset.path };
  focusRoot(rootId, { render: false });   // 选中即聚焦该根，切到矩形图时不会还停在旧目标上
  loadInspector(rootId, nodeEl.dataset.key);
  if (scroll) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function invalidateRoot(rootId) {
  for (const k of [...S.treeCache.keys()]) {
    if (k.startsWith(`${rootId}:`)) S.treeCache.delete(k);
  }
}

async function refreshRootTree(rootId) {
  invalidateRoot(rootId);
  const root = S.rootMap.get(rootId);
  if (!root) return;
  const el = $(`#tree .node[data-root="${rootId}"][data-depth="0"]`);
  if (!el) return;
  const expandedNow = S.expanded.has(treeKey(rootId, root.key));
  const parent = el.parentElement;
  const tmp = document.createElement('div');
  tmp.innerHTML = rootRowHTML(root);
  const fresh = tmp.firstElementChild;
  el.replaceWith(fresh);
  if (expandedNow) await loadChildren(rootId, root.key, fresh);
}

/* ────────────────────────────────────────────────────────── 5. 矩形图 */
function squarify(items, x, y, w, h) {
  const out = [];
  const list = items.filter(i => i.value > 0);
  const total = list.reduce((s, i) => s + i.value, 0) || 1;
  let fx = x, fy = y, fw = w, fh = h, remaining = total, idx = 0;
  let guard = 0;
  while (idx < list.length && fw > 0.5 && fh > 0.5 && guard++ < 500) {
    const scale = (fw * fh) / Math.max(remaining, 1);
    const side = Math.min(fw, fh);
    let row = [list[idx]];
    let area = list[idx].value * scale;
    let worst = rowRatio(row, area, scale, side);
    idx++;
    while (idx < list.length) {
      const cand = row.concat([list[idx]]);
      const candArea = area + list[idx].value * scale;
      const r = rowRatio(cand, candArea, scale, side);
      if (r <= worst) { row = cand; area = candArea; worst = r; idx++; } else break;
    }
    const sum = row.reduce((s, i) => s + i.value, 0);
    if (fw >= fh) {
      const thick = Math.max(1, area / fh);
      let cy = fy;
      row.forEach((it, k) => {
        const ih = k === row.length - 1 ? Math.max(0, fy + fh - cy) : (it.value * scale) / thick;
        out.push({ item: it, x: fx, y: cy, w: thick, h: Math.max(0, ih) });
        cy += ih;
      });
      fx += thick; fw -= thick;
    } else {
      const thick = Math.max(1, area / fw);
      let cx = fx;
      row.forEach((it, k) => {
        const iw = k === row.length - 1 ? Math.max(0, fx + fw - cx) : (it.value * scale) / thick;
        out.push({ item: it, x: cx, y: fy, w: Math.max(0, iw), h: thick });
        cx += iw;
      });
      fy += thick; fh -= thick;
    }
    remaining -= sum;
  }
  return out;
}
function rowRatio(row, area, scale, side) {
  const thick = area / side;
  if (!(thick > 0)) return Infinity;
  let worst = 1;
  for (const it of row) {
    const len = (it.value * scale) / thick;
    if (!(len > 0)) return Infinity;
    const r = Math.max(thick / len, len / thick);
    if (r > worst) worst = r;
  }
  return worst;
}

/* 切换"当前聚焦的追踪目标"：左侧卡片高亮 + 矩形图的根节点。
   矩形图一次只画一个追踪目标，所以新增/选择目标时必须显式切根，否则看起来"没更新"。 */
function focusRoot(rootId, { render = true } = {}) {
  if (!rootId || !S.rootMap.has(Number(rootId))) return;
  rootId = Number(rootId);
  if (S.tmRootId !== rootId) S.tmPath = null;   // 换根时回到根层
  S.activeRoot = rootId;
  S.tmRootId = rootId;
  $$('.root-card').forEach(c => c.classList.toggle('active', Number(c.dataset.root) === rootId));
  if (render && S.view === 'treemap') renderTreemap();
}

async function renderTreemap() {
  const box = $('#treemap');
  const root = S.rootMap.get(S.tmRootId) || S.roots[0];
  if (!root) { box.innerHTML = ''; return; }
  S.tmRootId = root.id;
  const crumb = $('#tm-crumb');
  crumb.innerHTML = `<span class="cur">${esc(root.label)}</span>`;
  box.innerHTML = `<div class="loading-row"><span class="spinner"></span> 构建矩形图…</div>`;
  try {
    const key = S.tmPath || root.key;
    const data = await api(`/api/treemap?root_id=${root.id}&key=${encodeURIComponent(key)}&threshold=${Math.round(S.threshold)}&limit=90&depth=2`);
    const rect = box.getBoundingClientRect();
    const W = Math.max(rect.width - 2, 100), H = Math.max(rect.height - 2, 100);
    const pad = 3;
    // 用百分比定位：容器后续尺寸变化（字体/滚动条/窗口）时方块自动填满，不会留白
    const pct = (v, total) => `${(v / total * 100).toFixed(3)}%`;
    const cells = squarify(data.children.map(c => ({ value: c.size, node: c })), pad, pad, W - pad * 2, H - pad * 2);
    const legend = TIERS.slice(1).map(t => `<span><i style="background:${t.color}"></i>≥${fmtBytes(t.min)}</span>`).join('')
      + `<span style="margin-left:auto">双击方块下钻 · 单击选中</span>`;
    if (!cells.length) {
      const scanning = root.status === 'scanning';
      box.innerHTML = `<div class="tm-empty">
          <div class="tm-empty-ico">◈</div>
          <p>${scanning ? '正在扫描该目标，稍候…' : '该目标在当前阈值下没有可显示的内容'}</p>
          <p class="hint">${scanning ? '扫描完成后会自动出现'
            : `试试把阈值调低；若刚添加该目标，请等扫描结束（上次扫描：${esc(root.scanned_at || '尚未扫描')}）`}</p>
        </div>`;
      renderCrumb(root, data);
      $('#tm-legend').innerHTML = legend;
      return;
    }
    box.innerHTML = '';
    const frag = document.createDocumentFragment();
    cells.forEach((cell, i) => {
      const n = cell.item.node;
      const t = tierOf(n.size);
      const el = document.createElement('div');
      el.className = 'tm-cell' + (S.selected && S.selected.key === n.key ? ' sel' : '');
      el.style.cssText = `left:${pct(cell.x, W)};top:${pct(cell.y, H)};width:${pct(Math.max(0, cell.w - 1), W)};height:${pct(Math.max(0, cell.h - 1), H)};--c:${t.color};animation-delay:${Math.min(i * 8, 200)}ms`;
      el.dataset.root = root.id;
      el.dataset.key = n.key;
      el.dataset.path = n.path;
      el.dataset.isdir = n.is_dir ? 1 : 0;
      const showLabel = cell.w > 46 && cell.h > 20;
      const showSize = cell.w > 74 && cell.h > 34;
      el.innerHTML = showLabel
        ? `<div class="tm-label"><span class="tm-name">${esc(n.name)}</span>${showSize ? `<span class="tm-size">${fmtBytes(n.size)}</span>` : ''}</div>`
        : '';
      el.title = `${n.path}\n${fmtBytes(n.size)} · 占本层 ${fmtPct(n.size / Math.max(data.parent.size, 1))}`;
      // 第二层
      const sub = n.sub || [];
      if (sub.length && cell.w > 70 && cell.h > 46) {
        const offset = showLabel ? (showSize ? 27 : 15) : 0;
        const iw = Math.max(1, cell.w - offset - 2), ih = Math.max(1, cell.h - offset - 2);
        const innerCells = squarify(sub.map(c => ({ value: c.size, node: c })), 0, 0, iw, ih);
        innerCells.forEach(ic => {
          const sn = ic.item.node;
          const sel2 = document.createElement('div');
          sel2.className = 'tm-cell lvl2';
          sel2.style.cssText = `left:${pct(offset + ic.x, cell.w)};top:${pct(offset + ic.y, cell.h)};width:${pct(Math.max(0, ic.w - 1), cell.w)};height:${pct(Math.max(0, ic.h - 1), cell.h)};--c:${tierOf(sn.size).color}`;
          sel2.dataset.root = root.id;
          sel2.dataset.key = sn.key;
          sel2.dataset.path = sn.path;
          sel2.dataset.isdir = sn.is_dir ? 1 : 0;
          if (ic.w > 54 && ic.h > 15) sel2.innerHTML = `<div class="tm-label"><span class="tm-name">${esc(sn.name)}</span></div>`;
          sel2.title = `${sn.path}\n${fmtBytes(sn.size)}`;
          el.appendChild(sel2);
        });
      }
      frag.appendChild(el);
    });
    box.appendChild(frag);
    renderCrumb(root, data);
    $('#tm-legend').innerHTML = legend;
  } catch (err) {
    box.innerHTML = `<div class="loading-row" style="color:var(--red)">${esc(err.message)}</div>`;
  }
}

function renderCrumb(root, data) {
  const crumb = $('#tm-crumb');
  const anc = data.ancestors || [];
  const parts = [`<button data-crumb="${esc(root.key)}">${esc(root.label)}</button>`];
  anc.forEach(a => parts.push(`<span class="sep">›</span><button data-crumb="${esc(a.key)}">${esc(a.name)}</button>`));
  if (data.node && data.node.key !== root.key) {
    parts.push(`<span class="sep">›</span><span class="cur">${esc(data.node.name)}</span>`);
  }
  crumb.innerHTML = parts.join('') +
    `<span style="margin-left:auto" class="hint">${fmtBytes(data.parent ? data.parent.size : 0)} · ${data.total} 项</span>`;
}

/* ────────────────────────────────────────────────────────── 6. 安装台账 */
async function loadApps(force = false) {
  if (S.appsLoaded && !force) return;
  const q = $('#apps-search').value.trim();
  const tracked = $('#apps-tracked').checked ? 1 : 0;
  try {
    const data = await api(`/api/apps?q=${encodeURIComponent(q)}&tracked=${tracked}`);
    S.apps = data.apps || [];
    S.appsLoaded = true;
    renderApps(data);
  } catch (err) {
    $('#apps-list').innerHTML = `<div class="apps-empty">台账读取失败：${esc(err.message)}</div>`;
  }
}

function renderApps(data) {
  const box = $('#apps-list');
  $('#apps-count').textContent = `${S.apps.length} 条`;
  if (!data.registry_ok) {
    box.innerHTML = `<div class="apps-empty">注册表不可读（可能不是 Windows）。<br>仍可手动添加追踪目标。</div>`;
    return;
  }
  if (!S.apps.length) {
    box.innerHTML = `<div class="apps-empty">没有匹配的安装记录。</div>`;
    return;
  }
  box.innerHTML = S.apps.map(a => {
    const t = tierOf(a.size);
    return `<div class="app-row" data-key="${esc(a.key)}">
      <div class="app-name">
        ${a.is_new ? '<span class="badge-new">NEW</span>' : ''}
        <b title="${esc(a.name)}">${esc(a.name)}</b>
        <span class="pub">${esc(a.publisher || '')}</span>
      </div>
      <div class="app-size" style="color:${t.color}">${a.size ? fmtBytes(a.size) : '—'}</div>
      <div class="app-ver">${esc(a.version || '')}</div>
      <div class="app-act">${a.root_id
        ? '<span class="tag-tracked">已追踪</span>'
        : (a.path ? '<button class="btn-mini" data-act="track">追踪</button>' : '<span class="hint">无路径</span>')}</div>
      <div class="app-path" title="${esc(a.path)}">${esc(a.path || '注册表未提供安装位置')}</div>
    </div>`;
  }).join('');
}

/* ────────────────────────────────────────────────────────── 7. 检视面板 */
async function loadInspector(rootId, key) {
  const box = $('#inspect-body');
  try {
    const { node } = await api(`/api/node?root_id=${rootId}&key=${encodeURIComponent(key)}`);
    const t = tierOf(node.size);
    const root = S.rootMap.get(rootId);
    const parentSize = node.parent_key
      ? (S.treeCache.get(treeKey(rootId, node.parent_key))?.parent?.size || node.size)
      : node.size;
    const share = node.is_root ? 1 : clamp(node.size / Math.max(parentSize, 1), 0, 1);
    $('#inspect-kind').textContent = node.is_root ? 'ROOT' : (node.is_dir ? 'DIR' : 'FILE');
    box.innerHTML = `
      <div class="insp-path" style="--c:${t.color}">${esc(node.path)}</div>
      <div class="insp-name"><span class="ico">${nodeIcon(node)}</span>${esc(node.is_root ? (root?.label || node.name) : node.name)}</div>
      <div class="insp-size" style="--c:${t.color}">${fmtBytes(node.size)}<small>${node.is_dir ? '子树占用' : '文件大小'}</small></div>
      <div class="share-bar" style="--c:${t.color}"><i style="width:${(share * 100).toFixed(1)}%"></i></div>
      <div class="insp-stats">
        <div class="insp-stat"><label>上级占比</label><b>${fmtPct(share)}</b></div>
        <div class="insp-stat"><label>文件数</label><b>${fmtInt(node.files)}</b></div>
        <div class="insp-stat"><label>子目录</label><b>${fmtInt(node.dirs)}</b></div>
        <div class="insp-stat"><label>修改时间</label><b>${node.mtime ? fmtDate(node.mtime) : '—'}</b></div>
      </div>
      <div class="insp-sec">我的标注</div>
      <textarea id="note-input" placeholder="例如：这是 2023 年的老游戏，通关了可以删 / 临时素材，月底清理">${esc(node.note || '')}</textarea>
      <div class="note-foot">
        <button class="flag-btn ${node.flagged ? 'on' : ''}" id="flag-btn">${node.flagged ? '⚑ 已标记待清理' : '⚐ 标记待清理'}</button>
        <span class="note-state" id="note-state">${node.note ? '已保存' : '未添加'}</span>
      </div>
      <input id="tag-input" type="text" placeholder="标签，用逗号分隔（清理 / 游戏 / 素材）" value="${esc(node.tags || '')}">
      <div class="insp-actions">
        <button class="btn btn-ghost" data-act="reveal" data-path="${esc(node.path)}">⌖ 打开位置</button>
        <button class="btn btn-ghost" data-act="copy" data-path="${esc(node.path)}">⧉ 复制路径</button>
        ${node.is_root
          ? `<button class="btn btn-ghost" data-act="rescan" data-root="${rootId}">↻ 重新扫描</button>
             <button class="btn btn-ghost" data-act="remove-root" data-root="${rootId}">✕ 移出追踪</button>`
          : `<button class="btn btn-ghost" data-act="add-path" data-path="${esc(node.path)}">＋ 单独追踪</button>
             <button class="btn btn-ghost" data-act="reveal-parent" data-path="${esc(dirname(node.path))}">▤ 上级目录</button>`}
      </div>
      <div class="insp-sec">本层最大项</div>
      <div class="preview-list">
        ${(node.preview || []).slice(0, 10).map(p => `
          <div class="preview-item" data-root="${rootId}" data-key="${esc(p.key)}" data-path="${esc(p.path)}">
            <span style="color:${tierOf(p.size).color}">${nodeIcon(p)}</span>
            <span class="pi-name" title="${esc(p.path)}">${esc(p.name)}</span>
            <span class="pi-size">${fmtBytes(p.size)}</span>
          </div>`).join('') || '<div class="muted-note">无子项</div>'}
      </div>
      ${node.children_total > 10 ? `<div class="muted-note" style="margin-top:6px">共 ${node.children_total} 个子项，展开目录树查看更多。</div>` : ''}
    `;
    const noteEl = $('#note-input');
    if (noteEl) {
      const flagEl = $('#flag-btn');
      noteEl.addEventListener('input', debounce(() => saveNote(node.path, true, { noteEl, tagEl: $('#tag-input'), flagEl }), 700));
      noteEl.addEventListener('blur', () => saveNote(node.path, false, { noteEl, tagEl: $('#tag-input'), flagEl }));
    }
    const tagEl = $('#tag-input');
    if (tagEl) tagEl.addEventListener('change', () => saveNote(node.path, false, { noteEl: $('#note-input'), tagEl, flagEl: $('#flag-btn') }));
    const flagEl = $('#flag-btn');
    if (flagEl) flagEl.addEventListener('click', () => {
      flagEl.classList.toggle('on');
      const on = flagEl.classList.contains('on');
      flagEl.textContent = on ? '⚑ 已标记待清理' : '⚐ 标记待清理';
      saveNote(node.path, false, { noteEl: $('#note-input'), tagEl: $('#tag-input'), flagEl });
    });
  } catch (err) {
    box.innerHTML = `<div class="inspect-empty"><div class="crosshair">╳</div><p>${esc(err.message)}</p></div>`;
  }
}

async function saveNote(path, silent, refs) {
  if (!refs || !refs.noteEl) return;
  const body = {
    path,
    note: refs.noteEl.value,
    tags: refs.tagEl ? refs.tagEl.value : '',
    flagged: refs.flagEl ? refs.flagEl.classList.contains('on') : false,
  };
  const state = $('#note-state');
  try {
    await api('/api/notes', body, 'PUT');
    if (state) state.textContent = silent ? '已自动保存' : `已保存 ${fmtTime()}`;
    // 同步树中的标记
    if (S.selected) {
      const el = $(`#tree .node[data-root="${S.selected.rootId}"][data-key="${cssEsc(S.selected.key)}"]`);
      if (el) {
        const nameEl = el.querySelector(':scope > .row .name');
        if (nameEl) {
          const old = nameEl.querySelector('.n-flag');
          if (body.flagged && !old) nameEl.insertAdjacentHTML('beforeend', '<span class="n-flag" title="已标记待清理">⚑</span>');
          if (!body.flagged && old) old.remove();
        }
      }
    }
  } catch (err) {
    if (state) state.textContent = '保存失败';
  }
}

/* ────────────────────────────────────────────────────────── 8. 弹窗 */
function openModal(html) {
  const m = $('#modal');
  $('#modal-box').innerHTML = html;
  m.classList.add('show');
  m.setAttribute('aria-hidden', 'false');
  const first = $('#modal-box input[type=text]');
  if (first) setTimeout(() => first.focus(), 60);
}
function closeModal() {
  $('#modal').classList.remove('show');
  $('#modal').setAttribute('aria-hidden', 'true');
}

/* 路径选择器（作为弹窗内的覆盖层，不破坏上层对话框） */
const Picker = {
  cb: null,
  path: '',
  async open(cb, startPath) {
    this.cb = cb;
    this.path = startPath || '';
    if (!$('#modal').classList.contains('show')) openModal('');
    const box = $('#modal-box');
    $$('.picker-layer').forEach(x => { x.remove(); });
    const hiddenLayers = [...box.children].map(el => [el, el.style.display]);
    hiddenLayers.forEach(([el]) => { el.style.display = 'none'; });
    const layer = document.createElement('div');
    layer.className = 'picker-layer';
    layer.innerHTML = `<div class="modal-head"><span class="tick"></span><h3>选择目录</h3>
        <button class="btn-mini x" data-close>✕ 关闭</button></div>
      <div class="modal-body">
        <div class="picker">
          <div class="picker-drives" id="pk-drives"></div>
          <div class="picker-main">
            <div class="picker-crumbs" id="pk-crumbs"></div>
            <div class="picker-list" id="pk-list"></div>
          </div>
        </div>
        <div class="field" style="margin-top:12px;margin-bottom:0">
          <label>或直接粘贴路径（支持单个文件）</label>
          <input type="text" id="pk-input" placeholder="D:\\Games\\新游戏">
        </div>
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" data-close>取消</button>
        <button class="btn btn-primary" id="pk-ok">选择当前目录</button>
      </div>`;
    $('#modal-box').appendChild(layer);
    const closeLayer = () => {
      layer.remove();
      hiddenLayers.forEach(([el, d]) => { el.style.display = d; });
    };
    await this.loadDrives();
    await this.go(this.path || '');
    $$('.picker-layer [data-close]').forEach(b => b.addEventListener('click', closeLayer));
    $('#pk-ok').addEventListener('click', () => {
      const typed = $('#pk-input').value.trim();
      const target = typed || this.path;
      if (!target) { toast('请选择目录', '', 'error'); return; }
      closeLayer();
      this.cb && this.cb(target);
    });
    $('#pk-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); $('#pk-ok').click(); }
    });
  },
  async loadDrives() {
    try {
      const { drives } = await api('/api/fs/drives');
      $('#pk-drives').innerHTML = drives.map(d => `
        <button class="picker-drive" data-drive="${esc(d.path)}">
          <span>▤ ${esc(d.path)}</span>
          <span class="pd-free">${fmtBytes(d.free)} 可用</span>
        </button>`).join('');
      $$('#pk-drives .picker-drive').forEach(b => b.addEventListener('click', () => this.go(b.dataset.drive)));
    } catch { /* 忽略 */ }
  },
  async go(path) {
    const list = $('#pk-list');
    list.innerHTML = `<div class="picker-empty"><span class="spinner"></span> 读取中…</div>`;
    try {
      const data = await api(`/api/fs/list?path=${encodeURIComponent(path || '')}`);
      this.path = data.path;
      $('#pk-input').value = data.path;
      $$('#pk-drives .picker-drive').forEach(b => b.classList.toggle('on', data.path.startsWith(b.dataset.drive)));
      const segs = data.path.replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean);
      const crumbs = [];
      let acc = '';
      segs.forEach((seg, i) => {
        acc += (i === 0 ? seg : '\\' + seg);
        const target = i === 0 ? seg + '\\' : acc;
        crumbs.push(`<button data-crumb="${esc(target)}">${esc(seg)}</button>`);
      });
      $('#pk-crumbs').innerHTML = (data.parent ? '<button data-crumb="' + esc(data.parent) + '">◀ 上一级</button><span class="sep">|</span>' : '') + crumbs.join('<span class="sep">›</span>');
      $$('#pk-crumbs [data-crumb]').forEach(b => b.addEventListener('click', () => this.go(b.dataset.crumb)));
      list.innerHTML = data.dirs.length ? data.dirs.map(d => `
        <button class="picker-item" data-dir="${esc(d.path)}">
          <span class="pi-ico">▣</span><span class="pi-name">${esc(d.name)}</span><span class="pi-go">进入 ›</span>
        </button>`).join('') : '<div class="picker-empty">此目录没有子目录</div>';
      $$('#pk-list .picker-item').forEach(b => {
        b.addEventListener('click', () => this.go(b.dataset.dir));
        b.addEventListener('dblclick', () => { this.path = b.dataset.dir; $('#pk-ok').click(); });
      });
    } catch (err) {
      list.innerHTML = `<div class="picker-empty" style="color:var(--red)">${esc(err.message)}</div>`;
    }
  },
};

async function addTrackDialog(prefill) {
  const suggestions = [];
  for (const p of ['C:\\Program Files', 'C:\\Program Files (x86)',
    (S.settings.localappdata || '')]) if (p) suggestions.push(p);
  try {
    const { drives } = await api('/api/fs/drives');
    drives.forEach(d => suggestions.push(d.path));
  } catch { /* 忽略 */ }
  openModal(`<div class="modal-head"><span class="tick"></span><h3>添加追踪目标</h3>
      <button class="btn-mini x" data-close>✕</button></div>
    <div class="modal-body">
      <div class="field">
        <label>目录或文件路径</label>
        <input type="text" id="add-path" placeholder="C:\\Games\\我的游戏 或 D:\\Downloads\\大电影.mkv" value="${esc(prefill || '')}">
        <div class="suggest" id="add-suggest">
          ${suggestions.slice(0, 8).map(s => `<button data-p="${esc(s)}">${esc(s)}</button>`).join('')}
        </div>
      </div>
      <div class="field">
        <label>显示名称（可选）</label>
        <input type="text" id="add-label" placeholder="留空则使用文件夹名">
      </div>
      <div class="modal-hint">添加后 SpaceSight 会立即扫描该目录并建立大小索引。目录只记录大小，不会被修改或删除。<br>
      也可以直接粘贴一个文件路径，单独追踪某个大文件（例如 ISO 镜像）。</div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="add-browse">⌖ 浏览目录</button>
      <button class="btn btn-ghost" data-close>取消</button>
      <button class="btn btn-primary" id="add-ok">添加并扫描</button>
    </div>`);
  $$('#modal-box [data-close]').forEach(b => b.addEventListener('click', closeModal));
  $$('#add-suggest button').forEach(b => b.addEventListener('click', () => { $('#add-path').value = b.dataset.p; }));
  $('#add-browse').addEventListener('click', () => {
    const cur = $('#add-path').value.trim();
    Picker.open(p => { $('#add-path').value = p; }, cur);
  });
  const submit = async () => {
    const path = $('#add-path').value.trim();
    if (!path) { toast('请输入路径', '', 'error'); return; }
    const label = $('#add-label').value.trim();
    closeModal();
    await addRoot(path, label);
    $('#add-path') && $('#add-path').focus();
  };
  $('#add-ok').addEventListener('click', submit);
  $('#add-path').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
}

async function addRoot(path, label) {
  try {
    setStatus(`正在添加 ${path} …`);
    const data = await api('/api/roots', { path, label });
    if (data.already) toast('已在追踪中', data.root.path, 'info');
    else toast('已加入追踪', path, 'install');
    await refreshState();
    if (data.root) focusRoot(data.root.id);        // 新增即聚焦：矩形图立刻切到新目标
    if (data.job) watchJob(data.job.id, `扫描 ${data.root ? data.root.label : ''}`);
    if (S.view === 'tree') renderTree();
    setStatus('就绪');
  } catch (err) {
    toast('添加失败', err.message, 'error');
  }
}

async function auditDialog(prefill) {
  openModal(`<div class="modal-head"><span class="tick"></span><h3>大文件体检</h3>
      <button class="btn-mini x" data-close>✕</button></div>
    <div class="modal-body">
      <div class="field">
        <label>体检范围</label>
        <input type="text" id="au-path" placeholder="C:\\ 或 D:\\Games" value="${esc(prefill || 'C:\\\\')}">
        <div class="suggest" id="au-suggest"></div>
      </div>
      <div class="field">
        <label>只关心大于</label>
        <div class="presets" id="au-size">
          <button data-v="104857600" class="on">100M</button>
          <button data-v="524288000">500M</button>
          <button data-v="1073741824">1G</button>
          <button data-v="5368709120">5G</button>
        </div>
      </div>
      <div class="modal-hint">体检是只读遍历，不会写入索引。目录越大耗时越久，结果会列出最大的文件与文件夹。</div>
      <div id="au-progress"></div>
      <div id="au-result"></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="au-browse">⌖ 浏览</button>
      <button class="btn btn-ghost" data-close>关闭</button>
      <button class="btn btn-primary" id="au-start">开始体检</button>
    </div>`);
  let minSize = 100 * MB;
  $$('#modal-box [data-close]').forEach(b => b.addEventListener('click', closeModal));
  $$('#au-size button').forEach(b => b.addEventListener('click', () => {
    $$('#au-size button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    minSize = Number(b.dataset.v);
  }));
  $('#au-browse').addEventListener('click', () => {
    Picker.open(p => { $('#au-path').value = p; }, $('#au-path').value);
  });
  try {
    const { drives } = await api('/api/fs/drives');
    $('#au-suggest').innerHTML = drives.map(d => `<button data-p="${esc(d.path)}">${esc(d.path)} (${fmtBytes(d.free)} 可用)</button>`).join('');
    $$('#au-suggest button').forEach(b => b.addEventListener('click', () => { $('#au-path').value = b.dataset.p; }));
  } catch { /* 忽略 */ }
  $('#au-start').addEventListener('click', async () => {
    const path = $('#au-path').value.trim();
    if (!path) { toast('请输入路径', '', 'error'); return; }
    $('#au-start').disabled = true;
    $('#au-progress').innerHTML = `<div class="progress-line"><i></i></div><div class="hint" id="au-live">正在遍历…</div>`;
    $('#au-result').innerHTML = '';
    try {
      const { job } = await api('/api/audit', { path, min_size: minSize, top_n: 120 });
      const timer = setInterval(async () => {
        try {
          const { job: j } = await api(`/api/jobs/${job.id}`);
          const live = $('#au-live');
          if (live) live.textContent = `已遍历 ${fmtInt(j.files)} 个文件 · ${fmtBytes(j.bytes)}`;
          if (j.done) {
            clearInterval(timer);
            $('#au-start').disabled = false;
            $('#au-progress').innerHTML = '';
            if (j.error) { toast('体检失败', j.error, 'error'); return; }
            renderAuditResult(j.result, path);
          }
        } catch { clearInterval(timer); $('#au-start').disabled = false; }
      }, 500);
    } catch (err) {
      $('#au-start').disabled = false;
      toast('体检失败', err.message, 'error');
    }
  });
}

function renderAuditResult(res, path) {
  const files = res.files || [], dirs = res.dirs || [];
  const row = (it, isDir) => `<div class="audit-row">
    <span style="color:${tierOf(it.size).color}">${isDir ? '▣' : nodeIcon({ name: it.name, is_dir: 0 })}</span>
    <span class="ar-name" title="${esc(it.path)}">${esc(it.name)}</span>
    <span class="ar-size">${fmtBytes(it.size)}</span>
    <span><button class="btn-mini" data-add="${esc(it.path)}" data-label="${esc(it.name)}">追踪</button></span>
    <span class="ar-path">${esc(it.path)}</span>
  </div>`;
  $('#au-result').innerHTML = `
    <div class="insp-sec" style="margin-top:8px">体检结果 · ${esc(path)} · 遍历 ${fmtInt(res.files_scanned)} 文件 / ${fmtBytes(res.scanned_bytes)}</div>
    <div class="audit-tabs">
      <button class="btn-mini on" data-tab="files">最大文件 ${files.length}</button>
      <button class="btn-mini" data-tab="dirs">最大目录 ${dirs.length}</button>
      <button class="btn-mini" id="au-add-all" style="margin-left:auto">＋ 全部加入追踪</button>
    </div>
    <div class="audit-list" id="au-list" data-which="files">${files.map(f => row(f, false)).join('')}</div>`;
  let currentTab = 'files';
  const paint = which => {
    currentTab = which;
    const list = which === 'files' ? files : dirs;
    $('#au-list').dataset.which = which;
    $('#au-list').innerHTML = list.length ? list.map(f => row(f, which === 'dirs')).join('')
      : '<div class="picker-empty">没有超过阈值的项目</div>';
    bindAuditRows();
  };
  $$('.audit-tabs [data-tab]').forEach(b => b.addEventListener('click', () => {
    $$('.audit-tabs [data-tab]').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    paint(b.dataset.tab);
  }));
  const bindAuditRows = () => $$('#au-list [data-add]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    b.textContent = '…';
    await addRoot(b.dataset.add, b.dataset.label);
    b.textContent = '已加';
  }));
  bindAuditRows();
  $('#au-add-all').addEventListener('click', async () => {
    const list = (currentTab === 'dirs' ? dirs : files).slice(0, 12);
    for (const f of list) await addRoot(f.path, f.name);
    toast('已批量加入', `共 ${list.length} 项`, 'install');
  });
}

async function settingsDialog() {
  const s = S.settings || {};
  const mins = Math.round((S.uptime || 0) / 60);
  openModal(`<div class="modal-head"><span class="tick"></span><h3>设置与数据</h3>
      <button class="btn-mini x" data-close>✕</button></div>
    <div class="modal-body">
      <div class="field">
        <label>后台察觉</label>
        <label class="chk" style="font-size:12px"><input type="checkbox" id="st-watch" ${String(s.watch_enabled) === '1' ? 'checked' : ''}>
          启用注册表台账 / 安装进程 / 目录变更侦测</label>
        <div class="modal-hint">关闭后不再自动发现新安装，只保留手动添加与扫描。</div>
      </div>
      <div class="field">
        <label>自动纳入追踪</label>
        <label class="chk" style="font-size:12px"><input type="checkbox" id="st-auto" ${String(s.auto_track) === '1' ? 'checked' : ''}>
          察觉到新安装 / 新目录时自动添加并测量占用</label>
        <div class="modal-hint">关闭后依然会写进察觉日志与安装台账，由你决定是否追踪。</div>
      </div>
      <div class="insp-sec">本地数据</div>
      <div class="muted-note" style="line-height:1.95">
        数据库：<span class="mono" style="color:#7fb3c9">${esc(S.dbPath || '')}</span><br>
        索引节点 <span class="mono">${fmtInt(S.totals.nodes)}</span> ·
        追踪目标 <span class="mono">${S.roots.length}</span> ·
        安装台账 <span class="mono">${fmtInt((S.detector || {}).apps || 0)}</span> 条 ·
        标注 <span class="mono">${fmtInt(S.totals.notes)}</span> 条<br>
        所有数据只保存在本机，不做任何联网上传。备份整个 <span class="mono">data</span> 目录即可。
      </div>
      <div class="insp-sec">运行信息</div>
      <div class="muted-note" style="line-height:1.95">
        服务地址 <span class="mono">${esc(location.origin)}</span> ·
        版本 <span class="mono">${esc(S.version || '')}</span> ·
        已运行 <span class="mono">${mins} 分钟</span>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="st-export">⭳ 导出 JSON 快照</button>
      <button class="btn btn-ghost" data-close>关闭</button>
      <button class="btn btn-primary" id="st-save">保存设置</button>
    </div>`);
  $$('#modal-box [data-close]').forEach(b => b.addEventListener('click', closeModal));
  $('#st-save').addEventListener('click', async () => {
    try {
      await api('/api/settings', {
        watch_enabled: $('#st-watch').checked ? 1 : 0,
        auto_track: $('#st-auto').checked ? 1 : 0,
      });
      closeModal();
      await refreshState();
      toast('设置已保存', $('#st-watch').checked ? '后台察觉运行中' : '后台察觉已暂停', 'info');
    } catch (err) { toast('保存失败', err.message, 'error'); }
  });
  $('#st-export').addEventListener('click', async () => {
    try {
      const data = await api('/api/export');
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `spacesight-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast('已导出', '包含追踪目标、标注、事件与安装台账', 'info');
    } catch (err) { toast('导出失败', err.message, 'error'); }
  });
}

function confirmDialog(title, text, onOk, okText = '确认') {  openModal(`<div class="modal-head"><span class="tick tick-magenta"></span><h3>${esc(title)}</h3>
      <button class="btn-mini x" data-close>✕</button></div>
    <div class="modal-body"><div class="modal-hint" style="font-size:12px">${text}</div></div>
    <div class="modal-foot"><button class="btn btn-ghost" data-close>取消</button>
      <button class="btn btn-primary" id="cf-ok">${esc(okText)}</button></div>`);
  $$('#modal-box [data-close]').forEach(b => b.addEventListener('click', closeModal));
  $('#cf-ok').addEventListener('click', () => { closeModal(); onOk(); });
}

/* ────────────────────────────────────────────────────────── 9. 轮询与启动 */
async function refreshState() {
  const data = await api('/api/state');
  S.settings = data.settings || {};
  S.roots = data.roots || [];
  S.rootMap = new Map(S.roots.map(r => [r.id, r]));
  S.totals = data.totals || {};
  S.detector = data.detector || {};
  S.disk = data.disk || {};
  S.version = data.version || '';
  S.uptime = data.uptime || 0;
  S.dbPath = data.db_path || '';
  if (!S.activeRoot && S.roots.length) S.activeRoot = S.roots[0].id;
  if (S.activeRoot && !S.rootMap.has(S.activeRoot)) S.activeRoot = S.roots[0]?.id || null;
  if (!S.tmRootId && S.roots.length) S.tmRootId = S.roots[0].id;
  if (S.tmRootId && !S.rootMap.has(S.tmRootId)) { S.tmRootId = null; S.tmPath = null; }
  if (!S.tmRootId && S.roots.length) S.tmRootId = S.roots[0].id;
  applyDetectorUI();
  applyDiskUI();
  renderRoots();
  $('#empty-state').classList.toggle('hidden', S.roots.length > 0 || S.view !== 'tree');
  return data;
}

function applyDetectorUI() {
  const d = S.detector || {};
  const on = DEMO || (d.running !== false && String(S.settings.watch_enabled) === '1');
  const pill = $('#watch-pill');
  pill.classList.toggle('off', !on);
  const text = !on ? '监控暂停' : (d.installers && d.installers.length ? `安装中 · ${d.installers[0]}` : '监控中');
  $('#watch-text').textContent = text;
  pill.title = on
    ? `后台察觉运行中\n台账 ${d.apps || 0} 条 · 监视 ${d.watched || 0} 个位置${d.installers && d.installers.length ? '\n正在安装：' + d.installers.join(', ') : ''}`
    : '后台察觉已暂停，点击可开启';
  const led = $('#led');
  led.className = 'led' + (!on ? ' off' : '');
}

function applyDiskUI() {
  const d = S.disk || {};
  if (!d.total) return;
  if (DEMO) {                       // 截图模式：不暴露真实磁盘容量
    $('#metric-disk').textContent = '1.02 TB / 2.00 TB';
    $('#disk-bar-fill').style.width = '51%';
    return;
  }
  $('#metric-disk').textContent = `${fmtBytes(d.used)} / ${fmtBytes(d.total)}`;
  $('#disk-bar-fill').style.width = `${(d.used / d.total * 100).toFixed(1)}%`;
}

function watchJob(jobId, label) {
  if (!jobId) return;
  const bar = $('#jobbar');
  const fill = $('#jobbar-fill');
  bar.classList.remove('idle');
  const started = Date.now();
  const timer = setInterval(async () => {
    try {
      const { job } = await api(`/api/jobs/${jobId}`);
      $('#status-job').textContent = `${label || ''} ${fmtInt(job.files)} 文件 / ${fmtBytes(job.bytes)}`;
      setStatus(`扫描中：${label || job.path || ''}  ${fmtInt(job.dirs)} 目录 / ${fmtInt(job.files)} 文件`);
      $('#led').classList.add('busy');
      if (job.done) {
        clearInterval(timer);
        bar.classList.add('idle');
        $('#status-job').textContent = '';
        $('#led').classList.remove('busy');
        setStatus(job.status === 'done' ? `扫描完成：${label} · ${fmtBytes(job.result?.total_bytes)} · ${job.result?.scan_ms}ms` : `扫描结束（${job.status}）`);
        await refreshState();
        const rid = job.extra && job.extra.root_id;
        if (rid) await refreshRootTree(Number(rid));
        else renderTree();
        if (S.view === 'treemap' && (!rid || Number(rid) === S.tmRootId)) renderTreemap();
        if (job.result && job.result.error) toast('扫描出错', job.result.error, 'error');
      }
      if (Date.now() - started > 1800e3) clearInterval(timer);
    } catch {
      clearInterval(timer);
    }
  }, 700);
}

async function pollEvents() {
  try {
    const data = await api(`/api/events?since=${S.maxEventId}&limit=40`);
    const fresh = (data.events || []).filter(e => e.id > S.maxEventId);
    if (!fresh.length) return;
    S.maxEventId = Math.max(S.maxEventId, ...fresh.map(e => e.id));
    S.events = [...fresh, ...S.events].slice(0, 120);
    renderEvents(fresh);
    fresh.slice(0, 3).forEach(e => {
      if (['install', 'folder', 'bigfile', 'error'].includes(e.kind)) {
        toast(e.label || e.kind, e.message || e.path, e.kind);
      }
    });
    if (fresh.some(e => ['install', 'folder', 'track'].includes(e.kind))) {
      await refreshState();
      if (S.view === 'tree') renderTree();
    }
  } catch { /* 网络抖动忽略 */ }
}

async function pollState() {
  try {
    const before = new Map(S.roots.map(r => [r.id, r.status + ':' + r.total_bytes]));
    const beforeCount = S.roots.length;
    await refreshState();
    const changedIds = new Set();
    for (const r of S.roots) {
      if (before.get(r.id) !== r.status + ':' + r.total_bytes) changedIds.add(r.id);
    }
    if (S.view === 'tree') {
      for (const id of changedIds) await refreshRootTree(id);
    }
    // 只在"矩形图正在展示的根"确实变化（或目标增删）时才重建，避免每 7 秒闪一下
    if (S.view === 'treemap' && (changedIds.has(S.tmRootId) || beforeCount !== S.roots.length)) {
      renderTreemap();
    }
  } catch { /* 忽略 */ }
}

function applyThreshold(v, { persist = true } = {}) {
  S.threshold = Math.max(1024, Math.round(v));
  $('#thr-value').textContent = fmtBytes(S.threshold);
  const idx = thrToSlider(S.threshold);
  $('#thr-slider').value = idx;
  $('#thr-slider').style.setProperty('--p', `${clamp(idx / 10, 0, 100)}%`);
  $$('#thr-presets button').forEach(b => b.classList.toggle('on', Number(b.dataset.v) === S.threshold));
  if (persist) persistThreshold();
}
const THR_MIN = 10 * MB, THR_MAX = 50 * GB, THR_RATIO = THR_MAX / THR_MIN;
const thrToSlider = v => clamp(Math.round(1000 * Math.log(Math.max(v, THR_MIN) / THR_MIN) / Math.log(THR_RATIO)), 0, 1000);
const sliderToThr = v => Math.round(THR_MIN * Math.pow(THR_RATIO, clamp(Number(v), 0, 1000) / 1000));
const persistThreshold = debounce(() => {
  api('/api/settings', { threshold: S.threshold }).catch(() => {});
}, 900);

const reloadTree = debounce(() => {
  S.treeCache.clear();
  if (S.view === 'tree') renderTree();
}, 260);

function switchView(view, { hash = true } = {}) {
  S.view = view;
  $$('#view-tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#tree-wrap').classList.toggle('hidden', view !== 'tree');
  $('#treemap-wrap').classList.toggle('hidden', view !== 'treemap');
  $('#apps-wrap').classList.toggle('hidden', view !== 'apps');
  $('#empty-state').classList.toggle('hidden', S.roots.length > 0 || view !== 'tree');
  if (hash) history.replaceState(null, '', '#' + view);
  if (view === 'treemap') renderTreemap();
  if (view === 'apps') loadApps();
}

/* ────────────────────────────────────────────────────────── 事件绑定 */
function bindUI() {
  // 视图切换
  $$('#view-tabs button').forEach(btn => btn.addEventListener('click', () => {
    switchView(btn.dataset.view);
  }));

  // 树：事件委托
  $('#tree').addEventListener('click', async e => {
    const more = e.target.closest('[data-act="more"]');
    if (more) {
      const rootId = Number(more.dataset.root);
      const offset = Number(more.dataset.offset || 0);
      const data = await api(`/api/tree?root_id=${rootId}&parent=${encodeURIComponent(more.dataset.key)}&threshold=${Math.round(S.threshold)}&limit=400&offset=${offset}`);
      const nodeEl = $('#tree .node[data-root="' + rootId + '"][data-key="' + cssEsc(more.dataset.key) + '"]');
      if (nodeEl) {
        const inner = nodeEl.querySelector(':scope > .kids-inner');
        const cacheKey = treeKey(rootId, more.dataset.key);
        const cached = S.treeCache.get(cacheKey);
        if (cached) cached.children = cached.children.concat(data.children);
        const maxSibling = Math.max(...data.children.map(c => c.size), 1);
        const html = data.children.map(c => nodeRowHTML(c, rootId, Number(nodeEl.dataset.depth) + 1, maxSibling, data.parent.size)).join('');
        more.insertAdjacentHTML('beforebegin', html);
        more.dataset.offset = offset + data.children.length;
        if (offset + data.children.length >= data.total) more.remove();
      }
      return;
    }
    const nodeEl = e.target.closest('.node');
    if (!nodeEl) return;
    const isToggle = e.target.closest('[data-act="toggle"]');
    if (isToggle) { toggleNode(nodeEl); return; }
    selectNode(nodeEl);
    if (nodeEl.dataset.isdir === '1') toggleNode(nodeEl);
  });
  $('#tree').addEventListener('dblclick', e => {
    const nodeEl = e.target.closest('.node');
    if (!nodeEl) return;
    selectNode(nodeEl);
  });

  // 左栏
  $('#roots-list').addEventListener('click', async e => {
    const card = e.target.closest('.root-card');
    if (!card) return;
    const rootId = Number(card.dataset.root);
    const act = e.target.closest('[data-act]');
    if (act) {
      const a = act.dataset.act;
      if (a === 'rescan') { rescanRoot(rootId); return; }
      if (a === 'remove') { removeRoot(rootId); return; }
      if (a === 'reveal') {
        const r = S.rootMap.get(rootId);
        api('/api/reveal', { path: r.path }).catch(err => toast('打开失败', err.message, 'error'));
        return;
      }
    }
    focusRoot(rootId);          // 高亮该目标 + 若正处于矩形图则切根重绘
    renderRoots();
    if (S.view === 'tree') {
      const el = $(`#tree .node[data-root="${rootId}"][data-depth="0"]`);
      if (el) {
        if (!S.expanded.has(treeKey(rootId, el.dataset.key))) toggleNode(el);
        selectNode(el, { scroll: true });
      }
    }
  });
  $('#events-list').addEventListener('click', e => {
    const ev = e.target.closest('.event');
    if (!ev || !ev.dataset.path) return;
    api('/api/reveal', { path: ev.dataset.path }).catch(() => {});
  });
  $('#btn-clear-events').addEventListener('click', async () => {
    await api('/api/events/clear', {});
    S.events = []; S.maxEventId = 0; renderEvents(); toast('日志已清空', '', 'info');
  });
  $('#btn-rescan-all').addEventListener('click', async () => {
    if (!S.roots.length) { toast('还没有追踪目标', '', 'error'); return; }
    for (const r of S.roots) await rescanRoot(r.id, true);
    toast('已排队重扫', `${S.roots.length} 个目标`, 'info');
  });
  $('#btn-detect').addEventListener('click', async () => {
    try {
      setStatus('正在侦查安装与目录变化…');
      const data = await api('/api/detect', {});
      const r = data.result || {};
      await refreshState();
      setStatus(`侦查完成：台账 ${r.apps || 0} 条 · 监视 ${r.watched || 0} 处 · 新装 ${r.new_apps || 0}`);
      toast('侦查完成', `台账 ${r.apps || 0} 条，新增安装 ${r.new_apps || 0} 个`, 'install');
    } catch (err) { toast('侦查失败', err.message, 'error'); }
  });
  $('#watch-pill').addEventListener('click', async () => {
    const on = String(S.settings.watch_enabled) === '1';
    await api('/api/settings', { watch_enabled: on ? 0 : 1 });
    await refreshState();
    toast(on ? '后台察觉已暂停' : '后台察觉已开启', on ? '不再自动记录新安装' : '将自动记录新安装与目录变化', 'info');
  });

  // 阈值
  $('#thr-slider').addEventListener('input', e => {
    applyThreshold(sliderToThr(e.target.value), { persist: true });
    reloadTree();
    if (S.view === 'treemap') debounce(renderTreemap, 300)();
  });
  $$('#thr-presets button').forEach(b => b.addEventListener('click', () => {
    applyThreshold(Number(b.dataset.v));
    reloadTree();
    if (S.view === 'treemap') renderTreemap();
  }));
  $('#btn-expand').addEventListener('click', () => expandTwoLevels());
  $('#btn-collapse').addEventListener('click', () => { S.expanded.clear(); renderTree(); });

  // 搜索
  $('#search-input').addEventListener('input', e => {
    clearTimeout(S.searchTimer);
    const q = e.target.value.trim();
    if (!q) { $('#search-results').classList.remove('show'); return; }
    S.searchTimer = setTimeout(() => doSearch(q), 280);
  });
  $('#search-input').addEventListener('keydown', e => {
    if (e.key === 'Escape') { $('#search-results').classList.remove('show'); e.target.blur(); }
  });
  $('#search-results').addEventListener('click', async e => {
    const item = e.target.closest('.sr-item');
    if (!item) return;
    $('#search-results').classList.remove('show');
    await revealPath(Number(item.dataset.root), item.dataset.key);
  });

  // 顶部按钮
  $('#btn-add').addEventListener('click', () => addTrackDialog());
  $('#btn-settings').addEventListener('click', () => settingsDialog());
  $('#empty-add').addEventListener('click', () => addTrackDialog());
  $('#btn-audit').addEventListener('click', () => auditDialog());
  $('#btn-audit-2').addEventListener('click', () => auditDialog());
  $('#empty-audit').addEventListener('click', () => auditDialog());
  $('#empty-programfiles').addEventListener('click', async () => {
    const dirs = [];
    try { const { drives } = await api('/api/fs/drives'); drives.forEach(d => dirs.push(d.path)); } catch { }
    await addRoot('C:\\Program Files', 'Program Files');
  });
  $('#btn-shutdown').addEventListener('click', () => {
    confirmDialog('关闭后台服务？', '界面将失去连接，需要重新双击启动器才能再次打开。<br>所有标注与索引已实时保存，不会丢失。',
      async () => {
        try { await api('/api/shutdown', {}); } catch { }
        document.body.innerHTML = `<div style="height:100vh;display:grid;place-items:center;background:#04060b;color:#5c7590;font-family:monospace;letter-spacing:.2em">
          <div style="text-align:center"><div style="font-size:28px;color:#00f0ff;text-shadow:0 0 30px #00f0ff">◈</div>
          <p>SERVICE OFFLINE</p><p style="font-size:11px">SpaceSight 已停机，可关闭此页面。</p></div></div>`;
      }, '停机');
  });

  // 台账
  $('#apps-search').addEventListener('input', debounce(() => { S.appsLoaded = false; loadApps(); }, 260));
  $('#apps-tracked').addEventListener('change', () => { S.appsLoaded = false; loadApps(); });
  $('#apps-list').addEventListener('click', async e => {
    const btn = e.target.closest('[data-act="track"]');
    const row = e.target.closest('.app-row');
    if (btn && row) {
      btn.disabled = true; btn.textContent = '…';
      try {
        const data = await api('/api/apps/track', { key: row.dataset.key });
        toast('已加入追踪', data.root.label, 'install');
        await refreshState();
        if (S.view === 'tree') renderTree();
        S.appsLoaded = false; loadApps();
        if (data.job) watchJob(data.job.id, data.root.label);
      } catch (err) { toast('追踪失败', err.message, 'error'); btn.disabled = false; btn.textContent = '追踪'; }
    }
  });
  $('#apps-clear-new').addEventListener('click', async () => {
    await api('/api/apps/clear-new', {});
    S.appsLoaded = false; loadApps();
    await refreshState();
  });

  // 矩形图
  $('#treemap').addEventListener('click', e => {
    const cell = e.target.closest('.tm-cell');
    if (!cell) return;
    $$('#treemap .tm-cell').forEach(c => c.classList.remove('sel'));
    cell.classList.add('sel');
    const rootId = Number(cell.dataset.root);
    S.selected = { rootId, key: cell.dataset.key, path: cell.dataset.path };
    loadInspector(rootId, cell.dataset.key);
  });
  $('#treemap').addEventListener('dblclick', e => {
    const cell = e.target.closest('.tm-cell');
    if (!cell || cell.classList.contains('lvl2')) return;
    S.tmPath = cell.dataset.key;
    renderTreemap();
  });
  $('#tm-crumb').addEventListener('click', e => {
    const b = e.target.closest('[data-crumb]');
    if (!b) return;
    const root = S.rootMap.get(S.tmRootId);
    S.tmPath = b.dataset.crumb === root.key ? null : b.dataset.crumb;
    renderTreemap();
  });

  // 检视面板里的动作
  $('#inspect-body').addEventListener('click', async e => {
    const prev = e.target.closest('.preview-item');
    if (prev) {
      const rootId = Number(prev.dataset.root);
      await revealPath(rootId, prev.dataset.key);
      return;
    }
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    const path = btn.dataset.path;
    if (act === 'reveal' || act === 'reveal-parent') {
      api('/api/reveal', { path }).catch(err => toast('打开失败', err.message, 'error'));
    } else if (act === 'copy') {
      try { await navigator.clipboard.writeText(path); toast('已复制路径', path, 'info'); }
      catch { toast('复制失败', '浏览器未授权剪贴板', 'error'); }
    } else if (act === 'rescan') {
      rescanRoot(Number(btn.dataset.root));
    } else if (act === 'remove-root') {
      removeRoot(Number(btn.dataset.root));
    } else if (act === 'add-path') {
      addRoot(path, basename(path));
    }
  });

  // 弹窗背景关闭 & 快捷键
  $('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
    if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      e.preventDefault();
      $('#search-input').focus();
    }
    if (e.key.toLowerCase() === 'r' && e.ctrlKey && e.altKey) { e.preventDefault(); $('#btn-rescan-all').click(); }
  });
  window.addEventListener('resize', debounce(() => { if (S.view === 'treemap') renderTreemap(); }, 300));
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(debounce(() => {
      if (S.view === 'treemap' && $('#treemap').offsetParent) renderTreemap();
    }, 220));
    ro.observe($('#treemap-wrap'));
  }
}

async function expandTwoLevels() {
  const roots = S.roots.slice(0, 6);
  for (const root of roots) {
    const el = $(`#tree .node[data-root="${root.id}"][data-depth="0"]`);
    if (!el) continue;
    await loadChildren(root.id, root.key, el);
    const kids = [...el.querySelectorAll(':scope > .kids > .kids-inner > .node')].slice(0, 6);
    for (const k of kids) {
      if (k.dataset.isdir === '1') await toggleNode(k);
    }
  }
}

async function doSearch(q) {
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}&threshold=0&root_id=0`);
    const box = $('#search-results');
    if (!data.hits.length) {
      box.innerHTML = `<div class="sr-empty">没有匹配项</div>`;
    } else {
      box.innerHTML = data.hits.slice(0, 40).map(h => `
        <div class="sr-item" data-root="${h.root_id}" data-key="${esc(h.key)}" data-path="${esc(h.path)}">
          <span style="color:${tierOf(h.size).color}">${nodeIcon(h)}</span>
          <span class="sr-name" title="${esc(h.path)}">${esc(h.name)}</span>
          <span class="sr-root">${esc(h.root_label)}</span>
          <span class="sr-size">${fmtBytes(h.size)}</span>
        </div>`).join('');
    }
    box.classList.add('show');
  } catch (err) { toast('搜索失败', err.message, 'error'); }
}

async function revealPath(rootId, key) {
  const root = S.rootMap.get(rootId);
  if (!root) return;
  if (S.view !== 'tree') switchView('tree');
  // 逐级展开
  try {
    const { node } = await api(`/api/node?root_id=${rootId}&key=${encodeURIComponent(key)}`);
    const targetKey = node.key;
    let parentKey = node.parent_key;
    const chain = [];
    let guard = 0;
    while (parentKey && guard++ < 40) {
      const data = await api(`/api/tree?root_id=${rootId}&parent=${encodeURIComponent(parentKey)}&threshold=${Math.round(S.threshold)}&limit=1`);
      chain.unshift(parentKey);
      parentKey = data.parent ? data.parent.parent_key : '';
      if (parentKey === root.key) break;
    }
    // 顺序展开
    let first = $(`#tree .node[data-root="${rootId}"][data-depth="0"]`);
    if (!first) { renderTree(); first = $(`#tree .node[data-root="${rootId}"][data-depth="0"]`); }
    if (first && !S.expanded.has(treeKey(rootId, root.key))) await toggleNode(first);
    for (const pk of chain) {
      const el = $(`#tree .node[data-root="${rootId}"][data-key="${cssEsc(pk)}"]`);
      if (!el) continue;
      if (!S.expanded.has(treeKey(rootId, pk))) await toggleNode(el);
    }
    const el = $(`#tree .node[data-root="${rootId}"][data-key="${cssEsc(targetKey)}"]`);
    if (el) {
      selectNode(el, { scroll: true });
      const row = el.querySelector(':scope > .row');
      if (row) { row.classList.remove('hit'); void row.offsetWidth; row.classList.add('hit'); }
    } else {
      toast('该项低于当前阈值', `请调低阈值以显示：${node.path}`, 'info');
    }
  } catch (err) { toast('定位失败', err.message, 'error'); }
}

async function rescanRoot(rootId, silent) {
  try {
    const { job } = await api(`/api/roots/${rootId}/scan`, {});
    const r = S.rootMap.get(rootId);
    if (!silent) toast('开始扫描', r ? r.path : '', 'info');
    watchJob(job.id, r ? r.label : '');
  } catch (err) { toast('扫描失败', err.message, 'error'); }
}

function removeRoot(rootId) {
  const r = S.rootMap.get(rootId);
  if (!r) return;
  confirmDialog('移出追踪？', `将不再追踪 <b style="color:#00f0ff">${esc(r.path)}</b>，索引会被删除。<br>你的标注会保留在本地数据库中，重新添加同一路径时会自动恢复。`,
    async () => {
      try {
        await api(`/api/roots/${rootId}`, null, 'DELETE');
        invalidateRoot(rootId);
        S.selected = null;
        await refreshState();
        renderTree();
        $('#inspect-body').innerHTML = `<div class="inspect-empty"><div class="crosshair">╳</div><p>在左侧树或矩形图中选择一个节点<br>查看占用详情并添加标注</p></div>`;
        toast('已移出追踪', r.label, 'info');
      } catch (err) { toast('操作失败', err.message, 'error'); }
    }, '移出');
}

/* ────────────────────────────────────────────────────────── 启动 */
async function boot() {
  bindUI();
  const clock = () => { $('#clock').textContent = fmtTime(); };
  clock(); setInterval(clock, 1000);

  try {
    const data = await refreshState();
    S.events = (data.events || []).slice();
    S.maxEventId = data.max_event_id || 0;
    renderEvents();
    applyThreshold(Number(S.settings.threshold) || 100 * MB, { persist: false });
    renderTree();
    const hv = (location.hash || '').replace('#', '');
    if (['tree', 'treemap', 'apps'].includes(hv)) switchView(hv, { hash: false });
    if (data.jobs && data.jobs.some(j => j.status === 'running')) {
      data.jobs.filter(j => j.status === 'running').forEach(j => watchJob(j.id, j.extra?.label || j.extra?.path || ''));
    }
    // 首次加载自动展开两层并选中最大项，避免右侧面板一片空白
    if (S.roots.length) {
      const firstRoot = S.roots[0];
      const el = $(`#tree .node[data-root="${firstRoot.id}"][data-depth="0"]`);
      if (el) {
        await toggleNode(el);
        const cache = S.treeCache.get(treeKey(firstRoot.id, firstRoot.key));
        if (cache && cache.children && cache.children.length) {
          const top = cache.children[0];
          const childEl = $(`#tree .node[data-root="${firstRoot.id}"][data-key="${cssEsc(top.key)}"]`);
          if (childEl) {
            selectNode(childEl);
            if (top.is_dir) await toggleNode(childEl);
          }
        }
      }
    }
    setStatus(S.roots.length ? '就绪 · 索引已加载' : '等待添加第一个追踪目标');
    $('#led').classList.remove('busy');
  } catch (err) {
    setStatus('无法连接后端服务');
    toast('连接失败', err.message, 'error');
    $('#led').className = 'led off';
  }
  S.booted = true;
  setTimeout(() => { if (S.view === 'treemap') renderTreemap(); }, 420);

  setInterval(() => { if (!document.hidden) pollEvents(); }, 3200);
  setInterval(() => { if (!document.hidden) pollState(); }, 7000);
}

document.addEventListener('DOMContentLoaded', boot);
