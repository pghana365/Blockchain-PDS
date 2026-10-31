(function () {
  'use strict';
  const { api, esc, alertBox } = window.PDS;

  document.addEventListener('DOMContentLoaded', () => {
    const video = document.getElementById('scan-video');
    if (!video) return;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const startBtn = document.getElementById('scan-start');
    const stopBtn = document.getElementById('scan-stop');
    const out = document.getElementById('scan-result');
    const manual = document.getElementById('manual-form');
    const file = document.getElementById('scan-file');
    let stream = null, raf = null, busy = false;

    async function resolve(text) {
      if (busy) return;
      busy = true;
      try {
        const d = await api('POST', '/api/scan/resolve', { code: text });
        alertBox(out, 'success', 'Found <strong>' + esc(d.bag_id) + '</strong> - opening…');
        stop();
        window.location = d.url;
      } catch (e) {
        alertBox(out, 'danger', esc(e.message));
        setTimeout(() => { busy = false; }, 1500);
      }
    }

    function tick() {
      if (video.readyState === video.HAVE_ENOUGH_DATA) {
        canvas.width = video.videoWidth; canvas.height = video.videoHeight;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = window.jsQR && window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
        if (code && code.data) resolve(code.data);
      }
      raf = requestAnimationFrame(tick);
    }

    async function start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alertBox(out, 'warning', 'Camera access needs https:// or http://localhost. Use manual entry or upload an image instead.');
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        video.srcObject = stream; await video.play();
        startBtn.classList.add('d-none'); stopBtn.classList.remove('d-none');
        out.classList.add('d-none');
        raf = requestAnimationFrame(tick);
      } catch (e) {
        alertBox(out, 'danger', 'Could not open the camera: ' + esc(e.message) + '. You can still type the Bag ID below.');
      }
    }
    function stop() {
      if (raf) cancelAnimationFrame(raf);
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null; video.srcObject = null;
      startBtn.classList.remove('d-none'); stopBtn.classList.add('d-none');
    }

    startBtn.addEventListener('click', start);
    stopBtn.addEventListener('click', stop);
    window.addEventListener('pagehide', stop);

    manual.addEventListener('submit', (ev) => { ev.preventDefault(); busy = false; resolve(manual.code.value.trim()); });

    file.addEventListener('change', () => {
      const f = file.files[0]; if (!f) return;
      const img = new Image();
      img.onload = () => {
        canvas.width = img.width; canvas.height = img.height; ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = window.jsQR(data.data, data.width, data.height);
        URL.revokeObjectURL(img.src);
        if (code) { busy = false; resolve(code.data); } else { alertBox(out, 'danger', 'No QR code found in that image.'); }
      };
      img.src = URL.createObjectURL(f);
    });
  });
})();
