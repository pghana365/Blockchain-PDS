(function () {
  'use strict';
  const { api, esc, alertBox, flash } = window.PDS;

  document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('bag-root');
    if (!root) return;
    const bagId = root.dataset.bagId;

    // ---- MLS dispatch / FPS receipt (the same code path, different endpoint)
    const form = document.getElementById('weight-form');
    if (form) {
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const kind = form.dataset.kind; // dispatch | receive
        const input = document.getElementById('weight-kg');
        const out = document.getElementById('weight-result');
        const weight = input.value.trim();
        if (!/^\d{1,3}(\.\d{1,3})?$/.test(weight) || Number(weight) <= 0) {
          alertBox(out, 'danger', 'Enter the gross weight in kg (e.g. 50.5), up to 3 decimals.');
          return;
        }
        const verb = kind === 'dispatch' ? 'DISPATCH' : 'RECEIVE';
        if (!window.confirm(verb + ' ' + bagId + ' with gross weight ' + weight + ' kg?\n\nThis is recorded permanently and cannot be edited.')) return;
        const btn = form.querySelector('button[type=submit]');
        btn.disabled = true; btn.textContent = 'Submitting to blockchain…';
        try {
          const data = await api('POST', '/api/bags/' + encodeURIComponent(bagId) + '/' + kind, { weight_kg: weight });
          if (data.outcome === 'PENDING_CONFIRMATION') {   // saved, but the ledger has not confirmed it: never show success
            flash('warning', '<strong>Awaiting blockchain confirmation.</strong><br>' + esc(data.message));
          } else {
            flash('success', '<strong>' + esc(data.message) + '</strong><br>Blockchain transaction ID: <span class="mono">' + esc(data.tx_id) + '</span>');
          }
          location.reload();
        } catch (e) {
          alertBox(out, 'danger', esc(e.message));
          btn.disabled = false; btn.textContent = kind === 'dispatch' ? 'Confirm dispatch' : 'Confirm receipt';
        }
      });
    }

    // ---- auditor/admin: verify against ledger
    const verifyBtn = document.getElementById('verify-btn');
    if (verifyBtn) {
      verifyBtn.addEventListener('click', async () => {
        const out = document.getElementById('verify-result');
        verifyBtn.disabled = true; verifyBtn.textContent = 'Checking ledger…';
        try {
          const d = await api('GET', '/api/audit/bags/' + encodeURIComponent(bagId) + '/verify');
          const rows = d.checks.map((c) => '<tr><td>' + (c.ok ? '<span class="status status-MATCHED">PASS</span>' : '<span class="status status-FLAGGED">FAIL</span>') + '</td><td>' + esc(c.check) + '</td><td class="text-muted small">' + esc(c.detail) + '</td></tr>').join('');
          out.innerHTML = '<div class="alert alert-' + (d.ok ? 'success' : 'danger') + ' mb-2">' + (d.ok ? 'All integrity checks passed: database records agree with the blockchain.' : 'One or more checks FAILED - investigate.') + '</div><table class="table table-sm"><tbody>' + rows + '</tbody></table>';
        } catch (e) { out.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + '</div>'; }
        verifyBtn.disabled = false; verifyBtn.textContent = 'Verify against blockchain';
      });
    }
    const histBtn = document.getElementById('history-btn');
    if (histBtn) {
      histBtn.addEventListener('click', async () => {
        const out = document.getElementById('history-result');
        try {
          const d = await api('GET', '/api/audit/bags/' + encodeURIComponent(bagId) + '/chain-history');
          out.innerHTML = '<ol class="timeline mt-3">' + d.map((h) => '<li><span class="status status-' + esc(h.status) + '">' + esc(h.status) + '</span> <span class="text-muted small">' + esc(h.timestamp) + '</span><br><span class="mono small">' + esc(h.tx_id) + '</span></li>').join('') + '</ol>';
        } catch (e) { out.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + '</div>'; }
      });
    }

    // ---- admin: corrections & chain-sync retry
    const corr = document.getElementById('correction-form');
    if (corr) {
      corr.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const out = document.getElementById('correction-result');
        try {
          const d = await api('POST', '/api/admin/bags/' + encodeURIComponent(bagId) + '/corrections', { note: corr.note.value });
          flash('success', esc(d.message)); location.reload();
        } catch (e) { alertBox(out, 'danger', esc(e.message)); }
      });
    }
    [['resolve-btn', false], ['abandon-btn', true]].forEach(([id, abandon]) => {
      const b = document.getElementById(id);
      if (!b) return;
      b.addEventListener('click', async () => {
        if (abandon && !window.confirm('Undo the unconfirmed operation? Only do this if the ledger does not have it (checked automatically).')) return;
        try {
          const d = await api('POST', '/api/admin/bags/' + encodeURIComponent(bagId) + '/resolve-pending', { abandon });
          flash(d.still_pending.length ? 'warning' : 'success',
            d.still_pending.length ? 'Still not confirmed by the ledger (' + d.still_pending.length + ' operation(s)).' : 'Resolved against the ledger.');
          location.reload();
        } catch (e) { window.alert(e.message); }
      });
    });
    const retry = document.getElementById('retry-sync');
    if (retry) {
      retry.addEventListener('click', async () => {
        try { await api('POST', '/api/admin/bags/' + encodeURIComponent(bagId) + '/retry-chain-sync'); flash('success', 'Reconciliation written to the blockchain.'); location.reload(); }
        catch (e) { window.alert(e.message); }
      });
    }
  });
})();
