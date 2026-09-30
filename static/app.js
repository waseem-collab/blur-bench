/* Oval Blur Studio — frame-accurate oval masking */
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || r.statusText);
  return body;
};

// Theatrical lighting gels: each oval reads as a filter laid over the picture.
const PALETTE = ['#FF6B5A', '#4FC3E8', '#A8D84A', '#F07CC3',
                 '#C8A2FF', '#FFD24A', '#5FE0A8', '#FF9A4A'];
const CHALK = '#E9E7DF', DIM = '#666E64', EDGE = '#313630';
const HANDLE = 7;        // screen px
const ROT_OFFSET = 26;   // screen px above the oval
const ZMIN = 10, ZMAX = 800;   // zoom percent

const S = {
  proj: null, shapes: [], frame: 0, sel: null,
  zoom: 'fit', scale: 1, playing: false, playTimer: null,
  undo: [], redo: [], saveTimer: null, rev: 0,
  drag: null, hover: null, loading: 0, scope: 'frame',
  newKind: 'oval', newScope: 'one',
};

/* ====================================================================== */
/* start screen                                                            */
/* ====================================================================== */

async function refreshRecent() {
  const box = $('recent');
  let items = [];
  try { items = await api('/api/projects'); } catch { /* ignore */ }
  if (!items.length) {
    box.innerHTML = '<p class="empty">Clips you open show up here.</p>';
    return;
  }
  box.innerHTML = '';
  for (const it of items) {
    const el = document.createElement('div');
    el.className = 'recent-item';
    const covered = it.shapes
      ? `<b>${it.shapes}</b> shape${it.shapes === 1 ? '' : 's'} so far`
      : 'nothing covered yet';
    el.innerHTML = `<img class="thumb" src="/api/project/${it.id}/thumb" alt="">
      <div class="ri-main">
        <span class="ri-name">${esc(it.name)}</span>
        <span class="ri-sub">${it.meta.width}&times;${it.meta.height}, ${covered}</span>
      </div>
      <button class="icon-btn danger" title="Take out of the list" aria-label="Remove">&times;</button>`;
    el.onclick = () => openProject(it.id);
    el.querySelector('button').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Take "${it.name}" out of the list?\nThe video file itself stays where it is.`)) return;
      await api(`/api/project/${it.id}`, { method: 'DELETE' });
      refreshRecent();
    };
    box.appendChild(el);
  }
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function startErr(msg) {
  const b = $('startErr');
  if (!msg) { b.hidden = true; return; }
  b.textContent = msg; b.hidden = false;
}

function uploadFile(file) {
  startErr('');
  $('uploadBar').hidden = false;
  $('uploadName').textContent = file.name;
  const fd = new FormData(); fd.append('file', file);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = Math.round(e.loaded / e.total * 100);
    $('uploadFill').style.width = p + '%';
    $('uploadPct').textContent = p + '%';
  };
  xhr.onload = () => {
    $('uploadBar').hidden = true;
    $('uploadFill').style.width = '0%';
    try {
      const res = JSON.parse(xhr.responseText);
      if (xhr.status >= 400) throw new Error(res.error || 'upload failed');
      openProject(res.id);
    } catch (err) { startErr(err.message); }
  };
  xhr.onerror = () => { $('uploadBar').hidden = true; startErr('Upload failed.'); };
  xhr.send(fd);
}

function initStart() {
  $('pickBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = (e) => e.target.files[0] && uploadFile(e.target.files[0]);

  const drop = $('drop');
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });

  const openPath = async () => {
    const path = $('pathInput').value.trim();
    if (!path) return;
    startErr('');
    try { openProject((await api('/api/open-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    })).id); } catch (e) { startErr(e.message); }
  };
  $('pathBtn').onclick = openPath;
  $('pathInput').onkeydown = e => { if (e.key === 'Enter') openPath(); };
  $('browseBtn').onclick = () => openBrowser();
  document.querySelectorAll('[data-close]').forEach(b => {
    b.onclick = () => { $(b.dataset.close).hidden = true; };
  });
  refreshRecent();
}

async function openBrowser(path) {
  $('browser').hidden = false;
  let data;
  try { data = await api('/api/browse' + (path ? '?path=' + encodeURIComponent(path) : '')); }
  catch (e) { $('browserList').innerHTML = `<p class="empty" style="padding:16px">${esc(e.message)}</p>`; return; }
  $('browserPath').textContent = data.path;
  const list = $('browserList'); list.innerHTML = '';
  const row = (icon, name, cls, fn) => {
    const d = document.createElement('div');
    d.className = 'brow-row ' + cls;
    d.innerHTML = `<span class="bi">${icon}</span><span class="bn">${esc(name)}</span>`;
    d.onclick = fn; list.appendChild(d); return d;
  };
  if (data.parent) row('&#8593;', 'up a level', 'dir', () => openBrowser(data.parent));
  data.dirs.forEach(d => row('&#9656;', d.name, 'dir', () => openBrowser(d.path)));
  data.files.forEach(f => {
    const r = row(f.image ? '&#9635;' : '&#9673;', f.name, 'video', async () => {
      try {
        const p = await api('/api/open-path', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: f.path }),
        });
        $('browser').hidden = true;
        openProject(p.id);
      } catch (e) { alert(e.message); }
    });
    r.insertAdjacentHTML('beforeend',
      `<span class="bs">${(f.size / 1048576).toFixed(1)} MB</span>`);
  });
  if (!data.dirs.length && !data.files.length)
    list.innerHTML = '<p class="empty" style="padding:16px">Nothing here to open.</p>';
}

/* ====================================================================== */
/* project + frames                                                        */
/* ====================================================================== */

async function openProject(id) {
  let p;
  try { p = await api('/api/project/' + id); }
  catch (e) { startErr(e.message); history.replaceState(null, '', ' '); return; }
  const asked = (location.hash.match(/[#&]f=(\d+)/) || [])[1];
  S.proj = p;
  S.shapes = p.shapes || [];
  S.sel = null; S.undo = []; S.redo = []; S.frame = 0; S.zoom = 'fit';
  $('start').hidden = true;
  $('editor').hidden = false;
  $('vidName').textContent = p.name;
  const m = p.meta;
  const still = m.media === 'image';
  $('editor').classList.toggle('is-image', still);
  $('vidMeta').textContent = still
    ? `${m.width}\u00d7${m.height} image`
    : `${m.width}\u00d7${m.height}, ${m.fps.toFixed(2)} fps, ${m.frames} frames, ` +
      `${fmtTime(m.duration).replace(/\.\d+$/, '')} long, ` +
      `${m.has_audio ? 'with sound' : 'no sound'}`;
  $('hint').textContent = still
    ? 'Drag across the picture to cover something'
    : 'Drag across the picture to cover something on this frame';
  $('emptyShapes').textContent = still
    ? 'Nothing covered yet. Drag across the picture.'
    : 'Nothing covered on this frame. Drag across the picture.';
  $('frameTotal').textContent = 'of ' + (m.frames - 1);
  $('frameNum').max = m.frames - 1;
  fitZoom();
  goto(asked ? +asked : 0, true);
  renderShapeList();
  markSaved();
}

function fmtTime(sec) {
  if (!isFinite(sec)) return '00:00.000';
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const base = `${String(m).padStart(2, '0')}:${s.toFixed(3).padStart(6, '0')}`;
  return h ? `${h}:${base}` : base;
}

function goto(n, force) {
  const max = S.proj.meta.frames - 1;
  n = Math.max(0, Math.min(max, Math.round(n)));
  if (n === S.frame && !force) { drawOverlay(); return; }
  S.frame = n;
  history.replaceState(null, '', `#p=${S.proj.id}&f=${n}`);
  $('frameNum').value = n;
  $('timecode').textContent = fmtTime(n / S.proj.meta.fps);
  loadFrame();
  drawOverlay();
  drawTimeline();
  renderShapeList();
  syncProps();
}

function frameURL(n) {
  const blur = $('previewBlur').checked ? 1 : 0;
  return `/api/project/${S.proj.id}/frame/${n}?blur=${blur}` + (blur ? `&r=${S.rev}` : '');
}

let pendingFrame = null;
function loadFrame() {
  const img = $('frameImg');
  const n = S.frame;
  pendingFrame = n;
  S.loading++; $('loading').hidden = false;
  const probe = new Image();
  probe.onload = () => {
    S.loading--;
    if (S.loading <= 0) { S.loading = 0; $('loading').hidden = true; }
    if (pendingFrame !== n) return;          // a newer request won
    img.src = probe.src;
    applyScale();
    // warm the next frame so stepping forward feels instant
    if (n + 1 < S.proj.meta.frames) new Image().src = frameURL(n + 1);
  };
  probe.onerror = () => {
    S.loading--;
    if (S.loading <= 0) { S.loading = 0; $('loading').hidden = true; }
  };
  probe.src = frameURL(n);
}

function refreshPreview() {
  if ($('previewBlur').checked) { S.rev++; loadFrame(); }
}

/* ---- zoom / scale ---- */
function fitZoom() {
  const st = $('stage'), m = S.proj.meta;
  const availW = st.clientWidth - 64, availH = st.clientHeight - 64;
  S.scale = Math.min(availW / m.width, availH / m.height, ZMAX / 100);
  if (!isFinite(S.scale) || S.scale <= 0) S.scale = 1;
  S.zoom = 'fit';
  $('zoom').value = clamp(Math.round(S.scale * 100), ZMIN, ZMAX);
  $('zoomVal').textContent = 'fit';
  applyScale();
}

function setZoom(pct) {
  pct = clamp(pct, ZMIN, ZMAX);
  S.zoom = pct;
  S.scale = pct / 100;
  $('zoom').value = Math.round(pct);
  $('zoomVal').textContent = Math.round(pct) + '%';
  applyScale();
}

/** Zoom so that whatever sits under the pointer stays under the pointer. */
function zoomAt(pct, clientX, clientY) {
  const stage = $('stage'), inner = $('stageInner');
  const before = inner.getBoundingClientRect();
  const vx = (clientX - before.left) / S.scale;
  const vy = (clientY - before.top) / S.scale;
  setZoom(pct);
  const after = inner.getBoundingClientRect();
  // scrolling right by d moves the picture left by d, so push the anchor back
  stage.scrollLeft += (after.left + vx * S.scale) - clientX;
  stage.scrollTop += (after.top + vy * S.scale) - clientY;
}

function applyScale() {
  const m = S.proj.meta;
  const w = Math.round(m.width * S.scale), h = Math.round(m.height * S.scale);
  const img = $('frameImg'), cv = $('overlay'), inner = $('stageInner');
  inner.style.width = w + 'px'; inner.style.height = h + 'px';
  img.style.width = w + 'px'; img.style.height = h + 'px';
  img.style.imageRendering = S.scale >= 2 ? 'pixelated' : 'auto';
  const dpr = window.devicePixelRatio || 1;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  drawOverlay();
}

/* ====================================================================== */
/* shape model                                                             */
/* ====================================================================== */

function newId() { return 's' + Math.random().toString(36).slice(2, 9); }

function makeShape(cx, cy, rx, ry) {
  const n = S.shapes.length;
  const kind = S.newKind;
  const whole = S.newScope === 'all';
  return {
    id: newId(), kind,
    name: (kind === 'rect' ? 'Box ' : 'Oval ') + (n + 1),
    color: PALETTE[n % PALETTE.length],
    enabled: true, mode: 'blur', strength: 45, feather: 12, track: false,
    start: whole ? 0 : S.frame,
    end: whole ? S.proj.meta.frames - 1 : S.frame,
    keys: [{ f: S.frame, cx, cy, rx, ry, ang: 0 }],
  };
}

function geomAt(sh, f) {
  if (f < sh.start || f > sh.end) return null;
  const k = [...sh.keys].sort((a, b) => a.f - b.f);
  if (!k.length) return null;
  if (k.length === 1 || f <= k[0].f) return { ...k[0] };
  if (f >= k[k.length - 1].f) return { ...k[k.length - 1] };
  for (let i = 0; i < k.length - 1; i++) {
    const a = k[i], b = k[i + 1];
    if (f >= a.f && f <= b.f) {
      const span = b.f - a.f, t = span ? (f - a.f) / span : 0;
      let d = ((b.ang - a.ang + 180) % 360 + 360) % 360 - 180;
      return {
        cx: a.cx + (b.cx - a.cx) * t, cy: a.cy + (b.cy - a.cy) * t,
        rx: a.rx + (b.rx - a.rx) * t, ry: a.ry + (b.ry - a.ry) * t,
        ang: a.ang + d * t,
      };
    }
  }
  return null;
}

function keyAt(sh, f) { return sh.keys.find(k => k.f === f); }

/** Write geometry onto a shape at the current frame, honouring track mode. */
function writeGeom(sh, f, g, kind) {
  if (sh.track) {
    const ex = keyAt(sh, f);
    if (ex) Object.assign(ex, g);
    else { sh.keys.push({ f, ...g }); sh.keys.sort((a, b) => a.f - b.f); }
  } else if (sh.keys.length <= 1) {
    if (!sh.keys.length) sh.keys.push({ f, ...g });
    else Object.assign(sh.keys[0], g);
  } else {
    // multiple keys but tracking is off: shift the whole path
    const cur = geomAt(sh, f) || sh.keys[0];
    if (kind === 'move') {
      const dx = g.cx - cur.cx, dy = g.cy - cur.cy;
      sh.keys.forEach(k => { k.cx += dx; k.cy += dy; });
    } else {
      sh.keys.forEach(k => { k.rx = g.rx; k.ry = g.ry; k.ang = g.ang; k.cx = k.cx + (g.cx - cur.cx); k.cy = k.cy + (g.cy - cur.cy); });
    }
  }
}

function isImage() { return S.proj && S.proj.meta.media === 'image'; }

function selected() { return S.shapes.find(s => s.id === S.sel) || null; }

function onFrame(f) { return S.shapes.filter(s => geomAt(s, f)); }

/** Nearest frame before `from` that carries an oval, or -1. */
function prevMarked(from) {
  let best = -1;
  for (const s of S.shapes) {
    const f = Math.min(s.end, from - 1);
    if (f >= s.start && f > best) best = f;
  }
  return best;
}

/** Nearest frame after `from` that carries an oval, or -1. */
function nextMarked(from) {
  let best = Infinity;
  for (const s of S.shapes) {
    const f = Math.max(s.start, from + 1);
    if (f <= s.end && f < best) best = f;
  }
  return isFinite(best) ? best : -1;
}

/** Bring the previous marked frame's ovals onto this one, so you can nudge them. */
function copyFromPrevious() {
  const src = prevMarked(S.frame);
  const from = src < 0 ? [] : onFrame(src);
  if (!from.length) return;
  snapshot();
  let last = null;
  for (const s of from) {
    const g = geomAt(s, src);
    const copy = JSON.parse(JSON.stringify(s));
    copy.id = newId();
    copy.start = copy.end = S.frame;
    copy.track = false;
    copy.keys = [{ f: S.frame, cx: g.cx, cy: g.cy, rx: g.rx, ry: g.ry, ang: g.ang }];
    S.shapes.push(copy);
    last = copy.id;
  }
  S.sel = last;
  afterChange();
}

function syncCopyBtn() {
  const src = prevMarked(S.frame);
  const btn = $('copyPrev');
  btn.disabled = src < 0;
  btn.textContent = src < 0 ? 'Copy from previous' : `Copy from frame ${src}`;
}

/* ---- undo ---- */
function snapshot() {
  S.undo.push(JSON.stringify(S.shapes));
  if (S.undo.length > 80) S.undo.shift();
  S.redo.length = 0;
}
function undo() { swapHistory(S.undo, S.redo); }
function redo() { swapHistory(S.redo, S.undo); }
function swapHistory(from, to) {
  if (!from.length) return;
  to.push(JSON.stringify(S.shapes));
  S.shapes = JSON.parse(from.pop());
  if (!S.shapes.some(s => s.id === S.sel)) S.sel = null;
  afterChange();
}

function afterChange() {
  renderShapeList(); syncProps(); drawOverlay(); drawTimeline();
  markDirty(); scheduleSave(); refreshPreview();
}

function markDirty() { $('saveState').textContent = 'saving…'; $('saveState').classList.add('dirty'); }
function markSaved() { $('saveState').textContent = 'saved'; $('saveState').classList.remove('dirty'); }

function scheduleSave() {
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(async () => {
    try {
      await api(`/api/project/${S.proj.id}/shapes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ shapes: S.shapes }),
      });
      markSaved();
    } catch { $('saveState').textContent = 'save failed'; }
  }, 350);
}

/* ====================================================================== */
/* overlay drawing                                                         */
/* ====================================================================== */

function shapePath(ctx, kind, g) {
  const a = g.ang * Math.PI / 180;
  const rx = Math.max(0.5, g.rx), ry = Math.max(0.5, g.ry);
  if (kind === 'rect') {
    // built under a rotated transform; the path keeps that geometry after restore
    ctx.save();
    ctx.translate(g.cx, g.cy);
    ctx.rotate(a);
    ctx.beginPath();
    ctx.rect(-rx, -ry, rx * 2, ry * 2);
    ctx.restore();
  } else {
    ctx.beginPath();
    ctx.ellipse(g.cx, g.cy, rx, ry, a, 0, Math.PI * 2);
  }
}

function handlePoints(g) {
  const a = g.ang * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
  const to = (lx, ly) => ({ x: g.cx + lx * ca - ly * sa, y: g.cy + lx * sa + ly * ca });
  const pts = [];
  for (const [sx, sy] of [[-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0]])
    pts.push({ sx, sy, ...to(sx * g.rx, sy * g.ry) });
  return { pts, rot: to(0, -g.ry - ROT_OFFSET / S.scale), top: to(0, -g.ry) };
}

function drawOverlay() {
  const cv = $('overlay');
  if (!cv.width || !S.proj) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr * S.scale, 0, 0, dpr * S.scale, 0, 0);
  ctx.clearRect(0, 0, S.proj.meta.width, S.proj.meta.height);
  const px = (v) => v / S.scale;

  for (const sh of S.shapes) {
    const g = geomAt(sh, S.frame);
    if (!g) continue;
    const isSel = sh.id === S.sel;

    ctx.save();
    shapePath(ctx, sh.kind, g);
    if (sh.enabled) {
      ctx.fillStyle = sh.color + (isSel ? '2e' : '1c');
      ctx.fill();
    } else {
      ctx.setLineDash([px(7), px(6)]);
    }
    // a dark liner under the gel keeps the edge readable on a bright frame
    ctx.lineWidth = px(isSel ? 3.4 : 2.6);
    ctx.strokeStyle = 'rgba(12,14,11,.55)';
    ctx.stroke();
    ctx.lineWidth = px(isSel ? 1.8 : 1.3);
    ctx.strokeStyle = sh.color;
    ctx.stroke();
    ctx.restore();

    // the name, on a flat tab of tape above the oval
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const lx = g.cx * S.scale, ly = (g.cy - g.ry) * S.scale;
    ctx.font = '11px Ubuntu, system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    const marks = sh.track && sh.keys.length > 1 ? `  ${sh.keys.length}◆` : '';
    const label = (sh.name || 'Shape') + marks;
    const tw = Math.ceil(ctx.measureText(label).width) + 12;
    const tx = clamp(Math.round(lx - tw / 2), 2, Math.max(2, cv.clientWidth - tw - 2));
    const ty = Math.max(2, Math.round(ly - 19));
    ctx.globalAlpha = sh.enabled ? 1 : .5;
    ctx.fillStyle = sh.color;
    ctx.fillRect(tx, ty, tw, 15);
    ctx.fillStyle = '#16180F';
    ctx.fillText(label, tx + 6, ty + 11);
    ctx.restore();

    if (!isSel) continue;

    // registration cross at the centre, then the handles
    const { pts, rot, top } = handlePoints(g);
    ctx.save();
    const arm = px(7);
    ctx.strokeStyle = sh.color;
    ctx.lineWidth = px(1);
    ctx.beginPath();
    ctx.moveTo(g.cx - arm, g.cy); ctx.lineTo(g.cx + arm, g.cy);
    ctx.moveTo(g.cx, g.cy - arm); ctx.lineTo(g.cx, g.cy + arm);
    ctx.stroke();

    ctx.lineWidth = px(1.2);
    ctx.beginPath(); ctx.moveTo(top.x, top.y); ctx.lineTo(rot.x, rot.y); ctx.stroke();

    const hs = px(HANDLE);
    for (const hp of pts) {
      ctx.fillStyle = 'rgba(12,14,11,.6)';
      ctx.fillRect(hp.x - hs / 2 - px(1), hp.y - hs / 2 - px(1), hs + px(2), hs + px(2));
      ctx.fillStyle = CHALK;
      ctx.fillRect(hp.x - hs / 2, hp.y - hs / 2, hs, hs);
    }
    ctx.beginPath(); ctx.arc(rot.x, rot.y, hs * 0.58, 0, Math.PI * 2);
    ctx.fillStyle = CHALK; ctx.fill();
    ctx.restore();
  }

  // the oval being dragged out right now
  if (S.drag && S.drag.type === 'new' && S.drag.g) {
    ctx.save();
    shapePath(ctx, S.newKind, S.drag.g);
    ctx.strokeStyle = CHALK; ctx.lineWidth = px(1.3);
    ctx.setLineDash([px(6), px(5)]); ctx.stroke();
    ctx.restore();
  }
}

/* ====================================================================== */
/* pointer interaction                                                     */
/* ====================================================================== */

function toVideo(e) {
  const r = $('overlay').getBoundingClientRect();
  return { x: (e.clientX - r.left) / S.scale, y: (e.clientY - r.top) / S.scale };
}

function localOf(g, p) {
  const a = g.ang * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
  const dx = p.x - g.cx, dy = p.y - g.cy;
  return { x: dx * ca + dy * sa, y: -dx * sa + dy * ca };
}

function hitTest(p) {
  const sel = selected();
  if (sel) {
    const g = geomAt(sel, S.frame);
    if (g) {
      const tol = (HANDLE + 4) / S.scale;
      const { pts, rot } = handlePoints(g);
      if (Math.hypot(p.x - rot.x, p.y - rot.y) < tol)
        return { type: 'rotate', shape: sel };
      for (const h of pts)
        if (Math.abs(p.x - h.x) < tol && Math.abs(p.y - h.y) < tol)
          return { type: 'resize', shape: sel, sx: h.sx, sy: h.sy };
    }
  }
  for (let i = S.shapes.length - 1; i >= 0; i--) {
    const sh = S.shapes[i];
    const g = geomAt(sh, S.frame);
    if (!g) continue;
    const l = localOf(g, p);
    const inside = sh.kind === 'rect'
      ? Math.abs(l.x) <= g.rx && Math.abs(l.y) <= g.ry
      : (l.x / Math.max(1, g.rx)) ** 2 + (l.y / Math.max(1, g.ry)) ** 2 <= 1;
    if (inside) return { type: 'move', shape: sh };
  }
  return null;
}

function initCanvas() {
  const cv = $('overlay');

  cv.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    cv.setPointerCapture(e.pointerId);
    const p = toVideo(e);
    const hit = hitTest(p);
    stopPlay();

    if (!hit) {
      select(null);
      S.drag = { type: 'new', origin: p, g: null, alt: e.altKey };
    } else {
      if (hit.shape.id !== S.sel) select(hit.shape.id);
      snapshot();
      const g = geomAt(hit.shape, S.frame);
      S.drag = { ...hit, origin: p, start: { ...g }, moved: false };
      if (hit.type === 'resize') {
        const a = g.ang * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
        const alx = -hit.sx * g.rx, aly = -hit.sy * g.ry;
        S.drag.anchor = { x: g.cx + alx * ca - aly * sa, y: g.cy + alx * sa + aly * ca };
      }
    }
    $('hint').style.opacity = 0;
    drawOverlay();
  });

  cv.addEventListener('pointermove', (e) => {
    const p = toVideo(e);
    if (!S.drag) {
      const hit = hitTest(p);
      cv.style.cursor = !hit ? 'crosshair'
        : hit.type === 'move' ? 'move'
          : hit.type === 'rotate' ? 'grab' : 'nwse-resize';
      return;
    }
    const d = S.drag;
    const W = S.proj.meta.width, H = S.proj.meta.height;

    if (d.type === 'new') {
      let x0 = d.origin.x, y0 = d.origin.y, x1 = p.x, y1 = p.y;
      let rx, ry, cx, cy;
      if (e.altKey) { cx = x0; cy = y0; rx = Math.abs(x1 - x0); ry = Math.abs(y1 - y0); }
      else { cx = (x0 + x1) / 2; cy = (y0 + y1) / 2; rx = Math.abs(x1 - x0) / 2; ry = Math.abs(y1 - y0) / 2; }
      if (e.shiftKey) { const r = Math.max(rx, ry); rx = r; ry = r; }
      d.g = { cx, cy, rx: Math.max(1, rx), ry: Math.max(1, ry), ang: 0 };
      drawOverlay();
      return;
    }

    const sh = d.shape;
    d.moved = true;
    if (d.type === 'move') {
      const g = {
        ...d.start,
        cx: clamp(d.start.cx + (p.x - d.origin.x), -d.start.rx, W + d.start.rx),
        cy: clamp(d.start.cy + (p.y - d.origin.y), -d.start.ry, H + d.start.ry),
      };
      writeGeom(sh, S.frame, g, 'move');
    } else if (d.type === 'rotate') {
      let ang = Math.atan2(p.y - d.start.cy, p.x - d.start.cx) * 180 / Math.PI + 90;
      if (e.shiftKey) ang = Math.round(ang / 15) * 15;
      writeGeom(sh, S.frame, { ...geomAt(sh, S.frame), ang: ((ang % 360) + 360) % 360 }, 'shape');
    } else if (d.type === 'resize') {
      const a = d.start.ang * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
      const ddx = p.x - d.anchor.x, ddy = p.y - d.anchor.y;
      const lx = ddx * ca + ddy * sa, ly = -ddx * sa + ddy * ca;
      let rx = d.sx === 0 ? d.start.rx : Math.max(2, Math.abs(lx) / 2);
      let ry = d.sy === 0 ? d.start.ry : Math.max(2, Math.abs(ly) / 2);
      if (e.shiftKey) {
        const ratio = d.start.rx / Math.max(1, d.start.ry);
        if (d.sx !== 0 && d.sy !== 0) ry = rx / ratio;
      }
      const ox = d.sx === 0 ? 0 : Math.sign(lx || d.sx) * rx;
      const oy = d.sy === 0 ? 0 : Math.sign(ly || d.sy) * ry;
      const cx = d.anchor.x + ox * ca - oy * sa;
      const cy = d.anchor.y + ox * sa + oy * ca;
      writeGeom(sh, S.frame, { cx, cy, rx, ry, ang: d.start.ang }, 'shape');
    }
    drawOverlay();
  });

  cv.addEventListener('pointerup', (e) => {
    const d = S.drag;
    S.drag = null;
    if (!d) return;
    if (d.type === 'new') {
      if (d.g && d.g.rx > 3 && d.g.ry > 3) {
        snapshot();
        const sh = makeShape(d.g.cx, d.g.cy, d.g.rx, d.g.ry);
        S.shapes.push(sh);
        S.sel = sh.id;
        afterChange();
      } else { drawOverlay(); }
    } else {
      if (d.moved) afterChange(); else { S.undo.pop(); drawOverlay(); }
    }
  });

  cv.addEventListener('dblclick', (e) => {
    const hit = hitTest(toVideo(e));
    if (hit) { select(hit.shape.id); $('pName').focus(); $('pName').select(); }
  });
}

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

/* ====================================================================== */
/* shape list + properties                                                 */
/* ====================================================================== */

function select(id) { S.sel = id; renderShapeList(); syncProps(); drawOverlay(); drawTimeline(); }

function renderShapeList() {
  const ul = $('shapeList'); ul.innerHTML = '';
  const here = onFrame(S.frame);
  const list = S.scope === 'all' ? S.shapes : here;

  $('shapeCount').textContent = (isImage() || S.scope === 'all')
    ? S.shapes.length
    : `${here.length} of ${S.shapes.length}`;
  $('emptyShapes').hidden = list.length > 0;
  if (!isImage()) {
    $('emptyShapes').textContent = S.scope === 'all'
      ? 'Nothing covered anywhere yet. Drag across the picture.'
      : (S.shapes.length
        ? 'Nothing on this frame. Drag across the picture, or copy the previous frame.'
        : 'Nothing covered on this frame. Drag across the picture.');
  }
  $('hint').style.opacity = S.shapes.length ? 0 : 1;
  syncCopyBtn();

  list.forEach(sh => {
    const live = !!geomAt(sh, S.frame);
    const li = document.createElement('li');
    li.className = 'shape-item' + (sh.id === S.sel ? ' sel' : '')
      + (sh.enabled ? '' : ' off') + (live ? '' : ' elsewhere');
    li.style.color = sh.color;
    li.innerHTML = `<span class="gel ${sh.kind === 'rect' ? 'box' : ''}" style="background:${sh.color}"></span>
      <span class="sn">${esc(sh.name)}</span>
      <span class="sr">${isImage() ? '' : (sh.start === sh.end ? sh.start : sh.start + '\u2013' + sh.end)}</span>
      <span class="eye" title="${sh.enabled ? 'Leave this one out' : 'Put this one back'}">${sh.enabled ? '&#9673;' : '&#9675;'}</span>`;
    li.onclick = () => {
      if (!live) goto(sh.start);        // an oval from another frame takes you there
      select(sh.id);
    };
    li.querySelector('.eye').onclick = (e) => {
      e.stopPropagation(); snapshot(); sh.enabled = !sh.enabled; afterChange();
    };
    ul.appendChild(li);
  });
}

function syncProps() {
  const sh = selected();
  $('props').hidden = !sh;
  if (!sh) return;
  $('propsTitle').textContent = sh.name || 'Shape';
  $('slate').style.setProperty('--gel', sh.color);
  $('pKind').querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', b.dataset.kind === (sh.kind || 'oval')));
  $('pName').value = sh.name;
  $('pMode').value = sh.mode;
  $('pStrength').value = sh.strength; $('pStrengthVal').textContent = sh.strength;
  $('pFeather').value = sh.feather; $('pFeatherVal').textContent = sh.feather;
  const single = sh.start === sh.end;
  $('scopeToggle').querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', (b.dataset.mode === 'one') === single));
  $('rangeFields').hidden = single || isImage();
  $('trackBox').hidden = single;        // interpolation means nothing on one frame
  $('pStart').value = sh.start; $('pEnd').value = sh.end;
  $('pStart').max = $('pEnd').max = S.proj.meta.frames - 1;
  $('pTrack').checked = !!sh.track;
  const onKey = !!keyAt(sh, S.frame);
  $('keyInfo').textContent = sh.keys.length === 1
    ? '1 position'
    : `${sh.keys.length} positions${onKey ? ', on one' : ''}`;
  $('delKey').disabled = !onKey || sh.keys.length <= 1;
}

function editShape(fn) {
  const sh = selected(); if (!sh) return;
  snapshot(); fn(sh); afterChange();
}

function initPanel() {
  $('addShape').onclick = () => {
    snapshot();
    const m = S.proj.meta;
    const sh = makeShape(m.width / 2, m.height / 2, m.width / 8, m.height / 8);
    S.shapes.push(sh); S.sel = sh.id; afterChange();
  };
  $('delShape').onclick = deleteSelected;
  $('copyPrev').onclick = copyFromPrevious;
  $('newKind').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      S.newKind = b.dataset.kind;
      $('newKind').querySelectorAll('button').forEach(o => o.classList.toggle('on', o === b));
    };
  });
  $('newScope').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      S.newScope = b.dataset.scope;
      $('newScope').querySelectorAll('button').forEach(o => o.classList.toggle('on', o === b));
    };
  });
  $('pKind').querySelectorAll('button').forEach(b => {
    b.onclick = () => editShape(s => { s.kind = b.dataset.kind; });
  });
  $('scopeFilter').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      S.scope = b.dataset.scope;
      $('scopeFilter').querySelectorAll('button').forEach(o => o.classList.toggle('on', o === b));
      renderShapeList();
    };
  });
  $('scopeToggle').querySelectorAll('button').forEach(b => {
    b.onclick = () => editShape(s => {
      if (b.dataset.mode === 'one') {
        const g = geomAt(s, S.frame) || s.keys[0];
        s.start = s.end = S.frame;
        s.keys = [{ f: S.frame, cx: g.cx, cy: g.cy, rx: g.rx, ry: g.ry, ang: g.ang }];
        s.track = false;
      } else if (s.end <= s.start) {
        s.end = S.proj.meta.frames - 1;
      }
    });
  });
  $('pName').oninput = (e) => {
    const sh = selected();
    if (!sh) return;
    sh.name = e.target.value;
    $('propsTitle').textContent = sh.name || 'Shape';
    renderShapeList(); markDirty(); scheduleSave(); drawOverlay();
  };
  $('pMode').onchange = (e) => editShape(s => s.mode = e.target.value);
  $('pStrength').oninput = (e) => {
    $('pStrengthVal').textContent = e.target.value;
    const sh = selected(); if (sh) { sh.strength = +e.target.value; markDirty(); scheduleSave(); }
  };
  $('pStrength').onchange = () => refreshPreview();
  $('pFeather').oninput = (e) => {
    $('pFeatherVal').textContent = e.target.value;
    const sh = selected(); if (sh) { sh.feather = +e.target.value; markDirty(); scheduleSave(); }
  };
  $('pFeather').onchange = () => refreshPreview();
  $('pStart').onchange = (e) => editShape(s => { s.start = clamp(+e.target.value, 0, s.end); });
  $('pEnd').onchange = (e) => editShape(s => { s.end = clamp(+e.target.value, s.start, S.proj.meta.frames - 1); });
  $('pStartHere').onclick = () => editShape(s => { s.start = Math.min(S.frame, s.end); });
  $('pEndHere').onclick = () => editShape(s => { s.end = Math.max(S.frame, s.start); });
  $('pWhole').onclick = () => editShape(s => { s.start = 0; s.end = S.proj.meta.frames - 1; });
  $('pTrack').onchange = (e) => editShape(s => s.track = e.target.checked);
  $('addKey').onclick = () => editShape(s => {
    const g = geomAt(s, S.frame) || s.keys[0];
    const ex = keyAt(s, S.frame);
    if (ex) Object.assign(ex, g);
    else { s.keys.push({ f: S.frame, cx: g.cx, cy: g.cy, rx: g.rx, ry: g.ry, ang: g.ang }); s.keys.sort((a, b) => a.f - b.f); }
    s.track = true;
  });
  $('delKey').onclick = () => editShape(s => {
    if (s.keys.length > 1) s.keys = s.keys.filter(k => k.f !== S.frame);
  });
  $('dupShape').onclick = duplicateSelected;
  $('applyAll').onclick = () => editShape(s => {
    S.shapes.forEach(o => { o.mode = s.mode; o.strength = s.strength; o.feather = s.feather; });
  });
}

function deleteSelected() {
  const sh = selected(); if (!sh) return;
  snapshot();
  S.shapes = S.shapes.filter(s => s.id !== sh.id);
  S.sel = null;
  afterChange();
}

function duplicateSelected() {
  const sh = selected(); if (!sh) return;
  snapshot();
  const copy = JSON.parse(JSON.stringify(sh));
  copy.id = newId();
  copy.name = sh.name + ' copy';
  copy.color = PALETTE[S.shapes.length % PALETTE.length];
  copy.keys.forEach(k => { k.cx += 24; k.cy += 24; });
  S.shapes.push(copy); S.sel = copy.id;
  afterChange();
}

/* ====================================================================== */
/* timeline                                                                */
/* ====================================================================== */

function drawTimeline() {
  const cv = $('timeline');
  if (!S.proj) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (w < 2) return;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const total = S.proj.meta.frames, fps = S.proj.meta.fps;
  const PAD = 1;
  const X = (f) => PAD + (f / Math.max(1, total - 1)) * (w - PAD * 2);

  const SPROCKET = 8;          // perforation strip along the bottom
  const RULER = 15;            // second ticks and their labels
  const laneTop = 6;
  const laneBottom = h - SPROCKET - RULER;

  ctx.fillStyle = '#121412';
  ctx.fillRect(0, 0, w, h);

  // perforations, at a fixed pitch, so the strip reads as film not as data
  ctx.fillStyle = '#272C26';
  for (let x = 7; x < w - 8; x += 17) roundRect(ctx, x, h - 6, 9, 4, 1.5, true);

  // second ticks
  const secs = total / fps;
  let stepSec = 1;
  for (const c of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800]) {
    stepSec = c;
    if (secs / c < 13) break;
  }
  ctx.font = '10px "Ubuntu Mono","DejaVu Sans Mono",monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  const ruleY = h - SPROCKET - RULER;
  for (let t = 0; t <= secs + 0.001; t += stepSec) {
    const x = X(Math.min(t * fps, total - 1));
    ctx.strokeStyle = EDGE;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + .5, ruleY);
    ctx.lineTo(Math.round(x) + .5, ruleY + 4);
    ctx.stroke();
    ctx.fillStyle = DIM;
    const lbl = fmtTime(t).replace(/\.000$/, '');
    const half = ctx.measureText(lbl).width / 2 + 2;
    ctx.fillText(lbl, clamp(x, half, w - half), ruleY + 13);
  }

  // Every oval marks the frames it covers, all in one band. Annotating a clip
  // frame by frame then reads as a density map rather than hundreds of lanes.
  const bandH = Math.min(13, laneBottom - laneTop);
  const bandY = laneTop + Math.max(0, (laneBottom - laneTop - bandH) / 2);
  ctx.fillStyle = '#1A1E19';
  ctx.fillRect(PAD, bandY, w - PAD * 2, bandH);

  S.shapes.forEach(sh => {
    const x0 = X(sh.start), x1 = X(sh.end);
    const bw = Math.max(2, x1 - x0);
    ctx.globalAlpha = sh.enabled ? (sh.id === S.sel ? 1 : .7) : .25;
    ctx.fillStyle = sh.color;
    roundRect(ctx, x0, bandY, bw, bandH, bw < 4 ? .5 : 1.5, true);
  });
  ctx.globalAlpha = 1;

  const sel = selected();
  if (sel) {                           // chalk caps so the selected one is findable
    const x0 = X(sel.start), x1 = X(sel.end);
    ctx.fillStyle = CHALK;
    ctx.fillRect(Math.round(x0), bandY - 2, 1, bandH + 4);
    ctx.fillRect(Math.round(x1), bandY - 2, 1, bandH + 4);
    if (sel.keys.length > 1) {         // recorded positions
      ctx.fillStyle = '#16180F';
      sel.keys.forEach(k => {
        const kx = X(k.f), r = Math.min(3, bandH / 2);
        ctx.beginPath();
        ctx.moveTo(kx, bandY + bandH / 2 - r);
        ctx.lineTo(kx + r, bandY + bandH / 2);
        ctx.lineTo(kx, bandY + bandH / 2 + r);
        ctx.lineTo(kx - r, bandY + bandH / 2);
        ctx.closePath(); ctx.fill();
      });
    }
  }

  // the blade, with its frame number riding on top
  const hx = Math.round(X(S.frame)) + .5;
  ctx.strokeStyle = CHALK;
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, h - SPROCKET); ctx.stroke();

  const flag = String(S.frame);
  ctx.font = '10px "Ubuntu Mono","DejaVu Sans Mono",monospace';
  const fw = Math.ceil(ctx.measureText(flag).width) + 9;
  const fx = Math.min(Math.max(hx - 1, 0), w - fw);
  ctx.fillStyle = CHALK;
  ctx.beginPath();
  ctx.moveTo(fx, 0); ctx.lineTo(fx + fw, 0);
  ctx.lineTo(fx + fw, 10); ctx.lineTo(fx + 4, 10); ctx.lineTo(fx, 13);
  ctx.closePath(); ctx.fill();
  ctx.fillStyle = '#121412';
  ctx.textAlign = 'left';
  ctx.fillText(flag, fx + 4, 8.5);
}

function roundRect(ctx, x, y, w, h, r, fill) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  if (fill) ctx.fill();
}

function nudgeZoom(factor) {
  const r = $('stage').getBoundingClientRect();
  const current = S.zoom === 'fit' ? S.scale * 100 : S.zoom;
  zoomAt(current * factor, r.left + r.width / 2, r.top + r.height / 2);
}

function initStageNav() {
  const stage = $('stage');

  stage.addEventListener('wheel', (e) => {
    if (!S.proj) return;
    e.preventDefault();                       // the wheel zooms here, never scrolls
    // line and page deltas need scaling up to feel the same as pixel ones
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    const dy = e.deltaY * unit;
    const current = S.zoom === 'fit' ? S.scale * 100 : S.zoom;
    zoomAt(current * Math.exp(-dy * 0.0015), e.clientX, e.clientY);
  }, { passive: false });

  // once the wheel is spoken for, panning needs its own gesture
  let pan = null;
  stage.addEventListener('mousedown', (e) => {
    if (e.button === 1) e.preventDefault();   // no middle-click autoscroll
  });
  stage.addEventListener('pointerdown', (e) => {
    if (e.button !== 1) return;
    e.preventDefault();
    pan = { x: e.clientX, y: e.clientY, sl: stage.scrollLeft, st: stage.scrollTop };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add('panning');
  });
  stage.addEventListener('pointermove', (e) => {
    if (!pan) return;
    stage.scrollLeft = pan.sl - (e.clientX - pan.x);
    stage.scrollTop = pan.st - (e.clientY - pan.y);
  });
  const endPan = () => { pan = null; stage.classList.remove('panning'); };
  stage.addEventListener('pointerup', endPan);
  stage.addEventListener('pointercancel', endPan);
}

function initTimeline() {
  const cv = $('timeline');
  let scrubbing = false;
  const seek = (e) => {
    const r = cv.getBoundingClientRect();
    const t = (e.clientX - r.left - 1) / (r.width - 2);
    goto(t * (S.proj.meta.frames - 1));
  };
  cv.addEventListener('pointerdown', e => {
    scrubbing = true; cv.setPointerCapture(e.pointerId); stopPlay(); seek(e);
  });
  cv.addEventListener('pointermove', e => { if (scrubbing) seek(e); });
  cv.addEventListener('pointerup', () => { scrubbing = false; });
}

/* ====================================================================== */
/* transport                                                               */
/* ====================================================================== */

function stopPlay() {
  const was = S.playing;
  S.playing = false;
  clearTimeout(S.playTimer);
  $('btnPlay').innerHTML = '&#9654;';
  if (was && S.proj) { renderShapeList(); syncProps(); }
}

function togglePlay() {
  if (S.playing) { stopPlay(); return; }
  if (S.frame >= S.proj.meta.frames - 1) goto(0);
  S.playing = true;
  $('btnPlay').innerHTML = '&#10074;&#10074;';
  const interval = 1000 / S.proj.meta.fps;
  const tick = () => {
    if (!S.playing) return;
    if (S.frame >= S.proj.meta.frames - 1) { stopPlay(); return; }
    const t0 = performance.now();
    const n = S.frame + 1;
    const img = new Image();
    img.onload = () => {
      if (!S.playing) return;
      S.frame = n;
      $('frameImg').src = img.src;
      $('frameNum').value = n;
      $('timecode').textContent = fmtTime(n / S.proj.meta.fps);
      drawOverlay(); drawTimeline();
      const wait = Math.max(0, interval - (performance.now() - t0));
      S.playTimer = setTimeout(tick, wait);
    };
    img.onerror = () => stopPlay();
    img.src = frameURL(n);
  };
  tick();
}

function initTransport() {
  $('btnPrev').onclick = () => { stopPlay(); goto(S.frame - 1); };
  $('btnNext').onclick = () => { stopPlay(); goto(S.frame + 1); };
  $('btnBack10').onclick = () => { stopPlay(); goto(S.frame - 10); };
  $('btnFwd10').onclick = () => { stopPlay(); goto(S.frame + 10); };
  $('btnStart').onclick = () => { stopPlay(); goto(0); };
  $('btnEnd').onclick = () => { stopPlay(); goto(S.proj.meta.frames - 1); };
  $('btnPrevMark').onclick = () => { const f = prevMarked(S.frame); if (f >= 0) { stopPlay(); goto(f); } };
  $('btnNextMark').onclick = () => { const f = nextMarked(S.frame); if (f >= 0) { stopPlay(); goto(f); } };
  $('btnPlay').onclick = togglePlay;
  $('frameNum').onchange = (e) => { stopPlay(); goto(+e.target.value); };
  $('zoom').oninput = (e) => setZoom(+e.target.value);
  $('btnFit').onclick = fitZoom;
  $('previewBlur').onchange = () => { S.rev++; loadFrame(); };
  $('backBtn').onclick = () => {
    stopPlay();
    $('editor').hidden = true; $('start').hidden = false;
    history.replaceState(null, '', ' ');
    refreshRecent();
  };
  window.addEventListener('resize', () => {
    if (!S.proj || $('editor').hidden) return;
    if (S.zoom === 'fit') fitZoom();
    drawTimeline();
  });
}

function initKeys() {
  document.addEventListener('keydown', (e) => {
    if ($('editor').hidden) return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'z') {
      e.preventDefault(); e.shiftKey ? redo() : undo(); return;
    }
    if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
    if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelected(); return; }
    if (mod) return;
    const step = e.shiftKey ? 10 : 1;
    switch (e.key) {
      case 'ArrowLeft': case ',': e.preventDefault(); stopPlay(); goto(S.frame - step); break;
      case 'ArrowRight': case '.': e.preventDefault(); stopPlay(); goto(S.frame + step); break;
      case 'Home': e.preventDefault(); stopPlay(); goto(0); break;
      case 'End': e.preventDefault(); stopPlay(); goto(S.proj.meta.frames - 1); break;
      case ' ': e.preventDefault(); togglePlay(); break;
      case 'Delete': case 'Backspace': e.preventDefault(); deleteSelected(); break;
      case 'Escape': select(null); break;
      case 'b': case 'B': $('previewBlur').checked = !$('previewBlur').checked;
        S.rev++; loadFrame(); break;
      case '+': case '=': e.preventDefault(); nudgeZoom(1.25); break;
      case '-': case '_': e.preventDefault(); nudgeZoom(1 / 1.25); break;
      case '0': e.preventDefault(); fitZoom(); break;
      case 'c': case 'C': e.preventDefault(); copyFromPrevious(); break;
      case '[': { e.preventDefault(); stopPlay();
        const f = prevMarked(S.frame); if (f >= 0) goto(f); break; }
      case ']': { e.preventDefault(); stopPlay();
        const f = nextMarked(S.frame); if (f >= 0) goto(f); break; }
    }
  });
}

/* ====================================================================== */
/* export                                                                  */
/* ====================================================================== */

let pollTimer = null;

function openExport() {
  const m = S.proj.meta;
  const still = isImage();
  $('xVideoOpts').hidden = still;
  $('xImageOpts').hidden = !still;
  $('exportModal').hidden = false;
  $('exportForm').hidden = false;
  $('exportProgress').hidden = true;
  $('exportDone').hidden = true;
  $('exportError').hidden = true;
  $('xStartBtn').hidden = false; $('xStartBtn').disabled = false;
  $('xCancel').hidden = true; $('xDownload').hidden = true;
  $('xName').value = S.proj.name.replace(/\.[^.]+$/, '') + '-blurred';
  $('xStart').value = 0; $('xStart').max = m.frames - 1;
  $('xEnd').value = m.frames - 1; $('xEnd').max = m.frames - 1;
  $('xAudio').disabled = !m.has_audio;
  $('xAudio').checked = !!m.has_audio;
  $('xBesideRow').hidden = !S.proj.linked;   // only meaningful for a file opened in place
  $('xQualityRow').hidden = imageFormat() !== 'jpg';
  updateSummary();
}

function imageFormat() {
  const on = $('xFormat').querySelector('button.on');
  return on ? on.dataset.format : 'png';
}

function updateSummary() {
  if (isImage()) {
    const active = S.shapes.filter(s => s.enabled);
    const skipped = S.shapes.length - active.length;
    const fmt = imageFormat();
    $('xSummary').innerHTML =
      (active.length
        ? `<b>${active.length}</b> shape${active.length === 1 ? '' : 's'} will be covered`
        : 'No shapes yet, so nothing will be covered') +
      (skipped > 0 ? `, ${skipped} left out` : '') +
      `. Saved as ${fmt === 'png' ? 'a PNG, pixel for pixel' : 'a JPEG'}.`;
    return;
  }
  const a = +$('xStart').value, b = +$('xEnd').value;
  const n = Math.max(0, b - a + 1);
  const active = S.shapes.filter(s => s.enabled && s.end >= a && s.start <= b);
  const skipped = S.shapes.length - active.length;
  $('xSummary').innerHTML =
    `<b>${n}</b> frames, ${fmtTime(n / S.proj.meta.fps).replace(/\.\d+$/, '')} of footage. ` +
    (active.length
      ? `<b>${active.length}</b> shape${active.length === 1 ? '' : 's'} will be covered`
      : 'No shapes fall in this range, so nothing will be covered') +
    (skipped > 0 ? `, ${skipped} left out.` : '.');
}

function initExport() {
  $('exportBtn').onclick = openExport;
  ['xStart', 'xEnd'].forEach(id => $(id).oninput = updateSummary);
  $('xWhole').onclick = () => {
    $('xStart').value = 0; $('xEnd').value = S.proj.meta.frames - 1; updateSummary();
  };
  $('xCrf').oninput = (e) => $('xCrfVal').textContent = e.target.value;
  $('xQuality').oninput = (e) => $('xQualityVal').textContent = e.target.value;
  $('xFormat').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      $('xFormat').querySelectorAll('button').forEach(o => o.classList.toggle('on', o === b));
      $('xQualityRow').hidden = b.dataset.format !== 'jpg';
      updateSummary();
    };
  });
  $('xStartBtn').onclick = startExport;
  $('xCancel').onclick = async () => {
    if (S.job) await api(`/api/export/${S.job}/cancel`, { method: 'POST' });
  };
}

async function startExport() {
  $('exportForm').hidden = true;
  $('exportProgress').hidden = false;
  $('exportError').hidden = true;
  $('xStartBtn').hidden = true;
  $('xCancel').hidden = false;
  $('xFill').style.width = '0%'; $('xPct').textContent = '0%';
  $('xNote').textContent = 'starting…'; $('xStats').textContent = '';
  try {
    const res = await api(`/api/project/${S.proj.id}/export`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        shapes: S.shapes,
        filename: $('xName').value.trim(),
        range_start: +$('xStart').value,
        range_end: +$('xEnd').value,
        crf: +$('xCrf').value,
        preset: $('xPreset').value,
        audio: $('xAudio').checked,
        beside: $('xBeside').checked,
        format: imageFormat(),
        quality: +$('xQuality').value,
      }),
    });
    S.job = res.job;
    poll();
  } catch (e) { exportFailed(e.message); }
}

function exportFailed(msg) {
  clearTimeout(pollTimer);
  $('exportProgress').hidden = true;
  $('exportError').hidden = false;
  $('exportError').textContent = msg;
  $('xCancel').hidden = true;
  $('xStartBtn').hidden = false;
  $('exportForm').hidden = false;
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    let j;
    try { j = await api('/api/export/' + S.job); }
    catch (e) { exportFailed(e.message); return; }
    const pct = Math.round((j.progress || 0) * 100);
    $('xFill').style.width = pct + '%';
    $('xPct').textContent = pct + '%';
    $('xNote').textContent = j.note || j.state;
    $('xStats').textContent = (j.total && !isImage())
      ? `frame ${j.frame || 0} of ${j.total}` +
        (j.fps ? `, ${j.fps} a second` : '') +
        (j.eta != null ? `, about ${fmtDur(j.eta)} left` : '')
      : '';
    if (j.state === 'done') {
      $('exportProgress').hidden = true;
      $('exportDone').hidden = false;
      $('xCancel').hidden = true;
      $('xDownload').hidden = false;
      $('xDownload').href = `/api/export/${S.job}/download`;
      $('xDoneInfo').textContent = isImage()
        ? `${(j.size / 1024).toFixed(0)} KB.`
        : `${(j.size / 1048576).toFixed(1)} MB, ${j.audio ? 'sound kept' : 'no sound'}.`;
      $('xPathInfo').textContent = j.output;
      return;
    }
    if (j.state === 'error') { exportFailed(j.note || 'export failed'); return; }
    if (j.state === 'cancelled') {
      $('exportProgress').hidden = true; $('exportForm').hidden = false;
      $('xCancel').hidden = true; $('xStartBtn').hidden = false;
      return;
    }
    poll();
  }, 400);
}

function fmtDur(s) {
  s = Math.round(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/* ====================================================================== */

initStart(); initCanvas(); initPanel(); initStageNav(); initTimeline();
initTransport(); initKeys(); initExport();

// reopening the page lands you back in the clip you were working on
const deep = location.hash.match(/^#p=([a-f0-9]+)/);
if (deep) openProject(deep[1]);
