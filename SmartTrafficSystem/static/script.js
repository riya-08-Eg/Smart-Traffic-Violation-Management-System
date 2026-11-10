(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  /* ---- Theme ---- */
  const root = document.documentElement;
  function syncThemeIcon() {
    const dark = root.dataset.theme === 'dark';
    $$('.theme-sun').forEach(e => e.hidden = dark);
    $$('.theme-moon').forEach(e => e.hidden = !dark);
  }
  document.addEventListener('click', e => {
    if (!e.target.closest('#theme-toggle')) return;
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('ts-theme', root.dataset.theme); } catch (_) {}
    syncThemeIcon();
    document.dispatchEvent(new Event('themechange'));
  });
  syncThemeIcon();

  /* ---- Mobile navigation ---- */
  const closeNav = () => document.body.classList.remove('nav-open');
  document.addEventListener('click', e => {
    if (e.target.closest('#menu-btn')) document.body.classList.toggle('nav-open');
    else if (e.target.closest('#scrim') || e.target.closest('.nav-link')) closeNav();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeNav(); });

  /* ---- Toasts ---- */
  function wireToast(t) {
    const kill = () => { t.style.transition = 'opacity .25s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 260); };
    const btn = $('[data-dismiss]', t);
    if (btn) btn.addEventListener('click', kill);
    if (!t.classList.contains('error')) setTimeout(kill, 6000);
  }
  $$('#toasts .flash').forEach(wireToast);
  window.showToast = function (message, kind) {
    const stack = $('#toasts'); if (!stack) return;
    const t = document.createElement('div');
    t.className = 'flash ' + (kind || 'info');
    t.innerHTML = '<svg class="icon"><use href="#i-' + (kind === 'error' ? 'alert' : 'info') + '"/></svg><span>' + esc(message) + '</span><button type="button" aria-label="Dismiss" data-dismiss><svg class="icon icon-sm"><use href="#i-x"/></svg></button>';
    stack.appendChild(t); wireToast(t);
  };

  /* ---- Confirmation dialog for destructive actions ---- */
  const dlg = $('#confirm-dialog');
  let pendingForm = null;
  document.addEventListener('submit', e => {
    const f = e.target;
    if (!f.dataset || !f.dataset.confirm || f.dataset.confirmed || !dlg || !dlg.showModal) return;
    e.preventDefault();
    pendingForm = f;
    $('#confirm-title').textContent = f.dataset.confirmTitle || 'Are you sure?';
    $('#confirm-text').textContent = f.dataset.confirm;
    $('#confirm-ok').textContent = f.dataset.confirmLabel || 'Confirm';
    dlg.showModal();
  });
  if (dlg) {
    $('#confirm-cancel').addEventListener('click', () => { dlg.close(); pendingForm = null; });
    $('#confirm-ok').addEventListener('click', () => { dlg.close(); if (pendingForm) { pendingForm.dataset.confirmed = '1'; pendingForm.submit(); } });
    dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  }

  /* ---- Collapsible panels ---- */
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-toggle]'); if (!b) return;
    const p = $(b.dataset.toggle); if (!p) return;
    p.hidden = !p.hidden;
    b.setAttribute('aria-expanded', String(!p.hidden));
    if (!p.hidden) { const first = $('input:not([type=hidden]),select,textarea', p); if (first) first.focus(); p.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
  });

  /* ---- Filter forms: submit on change, debounce search ---- */
  $$('form[data-autosubmit]').forEach(form => {
    $$('select,input[type=date]', form).forEach(el => el.addEventListener('change', () => form.requestSubmit()));
    const q = $('input[type=search]', form);
    if (q) { let t; q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => form.requestSubmit(), 450); });
      if (document.activeElement === document.body && q.value) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } }
  });

  /* ---- Violation form helpers ---- */
  const typeSel = $('#violation-type'), fine = $('#fine-amount');
  if (typeSel && fine) typeSel.addEventListener('change', () => {
    const o = typeSel.options[typeSel.selectedIndex];
    if (o && o.dataset.fine) fine.value = Number(o.dataset.fine).toFixed(2);
  });
  const photo = $('#evidence-photo'), preview = $('#evidence-preview');
  if (photo && preview) photo.addEventListener('change', () => {
    const f = photo.files[0];
    if (!f) { preview.hidden = true; return; }
    if (f.size > 5 * 1024 * 1024) { photo.value = ''; preview.hidden = true; window.showToast('That photo is larger than 5 MB.', 'error'); return; }
    preview.src = URL.createObjectURL(f); preview.hidden = false;
  });

  /* ---- Password reveal ---- */
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-reveal]'); if (!b) return;
    const input = $(b.dataset.reveal); if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    b.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    b.innerHTML = '<svg class="icon"><use href="#i-' + (show ? 'eye-off' : 'eye') + '"/></svg>';
  });

  /* ---- Client-side status tabs (driver history) ---- */
  $$('[data-filter-group]').forEach(group => {
    const table = $(group.dataset.filterGroup); if (!table) return;
    const rows = $$('tbody tr[data-status]', table);
    const empty = $('.js-empty', table.closest('.card') || document);
    const search = $(group.dataset.search || '#none');
    let active = 'all';
    const apply = () => {
      const q = search ? search.value.trim().toLowerCase() : '';
      let shown = 0;
      rows.forEach(r => {
        const ok = (active === 'all' || r.dataset.status.split(' ').includes(active)) && (!q || r.textContent.toLowerCase().includes(q));
        r.hidden = !ok; if (ok) shown++;
      });
      if (empty) empty.hidden = shown !== 0;
    };
    group.addEventListener('click', e => {
      const c = e.target.closest('.chip'); if (!c) return;
      $$('.chip', group).forEach(x => x.classList.toggle('active', x === c));
      active = c.dataset.value; apply();
    });
    if (search) search.addEventListener('input', apply);
  });

  /* ---- Charts (dependency-free SVG) ---- */
  const PALETTE = ['#2f5bea', '#e07b00', '#12a36b', '#7a4ee0', '#d92d4a', '#0ea5c6', '#8a94a6'];
  const css = n => getComputedStyle(root).getPropertyValue(n).trim();

  function donut(el, items, centerLabel) {
    const total = items.reduce((s, i) => s + i.value, 0);
    if (!total) { el.innerHTML = '<div class="empty-state"><p>No data yet</p></div>'; return; }
    const R = 54, C = 2 * Math.PI * R; let off = 0;
    const arcs = items.filter(i => i.value > 0).map(i => {
      const len = i.value / total * C;
      const s = '<circle cx="70" cy="70" r="' + R + '" fill="none" stroke="' + i.color + '" stroke-width="18" stroke-dasharray="' + len + ' ' + (C - len) + '" stroke-dashoffset="' + (-off) + '" transform="rotate(-90 70 70)"><title>' + esc(i.label) + ': ' + i.value + '</title></circle>';
      off += len; return s;
    }).join('');
    el.innerHTML = '<div class="donut-wrap"><svg class="donut" viewBox="0 0 140 140" width="150" height="150" role="img" aria-label="' + esc(centerLabel) + ' chart"><circle cx="70" cy="70" r="' + R + '" fill="none" stroke="' + css('--slate-soft') + '" stroke-width="18"/>' + arcs +
      '<text x="70" y="68" text-anchor="middle" font-size="24" font-weight="800" fill="' + css('--ink') + '">' + total + '</text><text x="70" y="86" text-anchor="middle" font-size="10.5" fill="' + css('--muted') + '">' + esc(centerLabel) + '</text></svg>' +
      '<div class="legend">' + items.map(i => '<div><i style="background:' + i.color + '"></i>' + esc(i.label) + '<b>' + i.value + '</b></div>').join('') + '</div></div>';
  }

  function bars(el, items) {
    const W = 520, H = 220, pad = {l: 30, r: 8, t: 14, b: 28};
    const max = Math.max(1, ...items.map(i => i.value));
    const top = Math.max(4, Math.ceil(max / 4) * 4), iw = W - pad.l - pad.r, ih = H - pad.t - pad.b, bw = Math.min(46, iw / items.length * .55);
    let g = '';
    for (let k = 0; k <= 4; k++) {
      const y = pad.t + ih - ih * k / 4;
      g += '<line x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + y + '" y2="' + y + '" stroke="' + css('--line') + '"/><text x="' + (pad.l - 8) + '" y="' + (y + 4) + '" text-anchor="end" font-size="10.5" fill="' + css('--muted') + '">' + Math.round(top * k / 4) + '</text>';
    }
    items.forEach((i, n) => {
      const x = pad.l + iw / items.length * (n + .5) - bw / 2, h = ih * i.value / top, y = pad.t + ih - h;
      g += '<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + Math.max(h, 0) + '" rx="6" fill="' + css('--brand') + '"><title>' + esc(i.label) + ': ' + i.value + '</title></rect>';
      if (i.value) g += '<text x="' + (x + bw / 2) + '" y="' + (y - 6) + '" text-anchor="middle" font-size="11.5" font-weight="700" fill="' + css('--ink-2') + '">' + i.value + '</text>';
      g += '<text x="' + (x + bw / 2) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="11.5" fill="' + css('--muted') + '">' + esc(i.label) + '</text>';
    });
    el.innerHTML = '<svg class="bars" viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="Violations per month">' + g + '</svg>';
  }

  function drawCharts() {
    const dataEl = $('#chart-data'); if (!dataEl) return;
    let d; try { d = JSON.parse(dataEl.textContent); } catch (_) { return; }
    const colors = {Paid: '#12a36b', Unpaid: '#e0a100', Overdue: '#d92d4a', Waived: '#8a94a6'};
    const m = $('#chart-monthly'), t = $('#chart-types'), s = $('#chart-status');
    if (m) bars(m, d.monthly);
    if (t) donut(t, d.types.slice(0, 6).map((i, n) => ({...i, color: PALETTE[n % PALETTE.length]})), 'violations');
    if (s) donut(s, d.status.map(i => ({...i, color: colors[i.label]})), 'fines');
  }
  drawCharts();
  document.addEventListener('themechange', drawCharts);
})();
