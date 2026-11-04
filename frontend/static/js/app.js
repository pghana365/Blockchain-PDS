/* Shared helpers. No inline scripts anywhere (strict CSP). */
(function () {
  'use strict';

  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), 'Accept': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (res.status === 401) { window.location = '/login?next=' + encodeURIComponent(location.pathname); }
    if (!res.ok) { const e = new Error(data.error || ('Request failed (' + res.status + ')')); e.status = res.status; e.data = data; throw e; }
    return data;
  }
  window.PDS = { api, csrf };

  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  window.PDS.esc = esc;

  function alertBox(el, kind, html) {
    if (!el) return;
    el.className = 'alert alert-' + kind + ' mt-3';
    el.innerHTML = html;
    el.classList.remove('d-none');
  }
  window.PDS.alertBox = alertBox;

  function flash(kind, html) { sessionStorage.setItem('pds-flash', JSON.stringify({ kind, html })); }
  window.PDS.flash = flash;

  // show a flash message stored before a reload
  document.addEventListener('DOMContentLoaded', () => {
    const raw = sessionStorage.getItem('pds-flash');
    if (raw) {
      sessionStorage.removeItem('pds-flash');
      const box = document.getElementById('flash');
      try { const f = JSON.parse(raw); alertBox(box, f.kind, f.html); } catch (_) { /* ignore */ }
    }

    // mobile menu
    const t = document.querySelector('.menu-toggle');
    if (t) t.addEventListener('click', () => document.querySelector('.sidebar').classList.toggle('open'));

    // generic JSON form:  <form data-api="/url" data-method="POST" data-reload>
    document.querySelectorAll('form[data-api]').forEach((form) => {
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const out = form.querySelector('.js-result');
        const btn = form.querySelector('button[type=submit]');
        const body = {};
        new FormData(form).forEach((v, k) => { body[k] = v; });
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
        if (btn) btn.disabled = true;
        try {
          const data = await api(form.dataset.method || 'POST', form.dataset.api, body);
          if (form.hasAttribute('data-reload')) {
            flash('success', esc(data.message || 'Saved.') + (data.created ? ' Created: <strong>' + data.created.map((c) => esc(c.bag_id) + (c.pending ? ' (awaiting blockchain confirmation)' : '')).join(', ') + '</strong>' : ''));
            location.reload();
            return;
          }
          alertBox(out, 'success', esc(data.message || 'Saved.'));
        } catch (e) {
          alertBox(out, 'danger', esc(e.message));
        } finally { if (btn) btn.disabled = false; }
      });
    });

    // generic action buttons: <button data-patch="/url" data-json='{"x":1}' data-confirm="..." data-prompt="new_password:Enter new password">
    document.querySelectorAll('button[data-patch]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const body = btn.dataset.json ? JSON.parse(btn.dataset.json) : {};
        if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
        if (btn.dataset.prompt) {
          const [field, label] = btn.dataset.prompt.split(':');
          const v = window.prompt(label);
          if (!v) return;
          body[field] = v;
        }
        try {
          await api('PATCH', btn.dataset.patch, body);
          location.reload();
        } catch (e) { window.alert(e.message); }
      });
    });

    // select-all helper for label printing
    const all = document.getElementById('select-all');
    if (all) all.addEventListener('change', () => document.querySelectorAll('.row-select').forEach((c) => { c.checked = all.checked; }));
    const printBtn = document.getElementById('print-labels');
    if (printBtn) printBtn.addEventListener('click', () => {
      const ids = Array.from(document.querySelectorAll('.row-select:checked')).map((c) => c.value);
      if (!ids.length) { window.alert('Select at least one bag.'); return; }
      window.open('/admin/labels?ids=' + encodeURIComponent(ids.join(',')), '_blank');
    });
    const printNow = document.getElementById('print-now');
    if (printNow) printNow.addEventListener('click', () => window.print());
  });
})();
