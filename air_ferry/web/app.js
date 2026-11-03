(() => {
  const CHUNK = 64 * 1024;
  const isTouch =
    window.matchMedia("(pointer: coarse)").matches ||
    navigator.maxTouchPoints > 0;

  const els = {
    status: document.getElementById("status-line"),
    presence: document.getElementById("presence-line"),
    dropzone: document.getElementById("dropzone"),
    dropTitle: document.getElementById("drop-title"),
    dropSub: document.getElementById("drop-sub"),
    fileInput: document.getElementById("file-input"),
    selectBtn: document.getElementById("select-btn"),
    fileList: document.getElementById("file-list"),
    fileCount: document.getElementById("file-count"),
    transfersSection: document.getElementById("transfers-section"),
    transferList: document.getElementById("transfer-list"),
    footUrl: document.getElementById("foot-url"),
    footVersion: document.getElementById("foot-version"),
    connectBlurb: document.getElementById("connect-blurb"),
    shareUrl: document.getElementById("share-url"),
    firewallNote: document.getElementById("firewall-note"),
    tunnelBtn: document.getElementById("tunnel-btn"),
    qrImage: document.getElementById("qr-image"),
  };

  const state = {
    ws: null,
    clientId: null,
    info: null,
    clients: [],
    files: new Map(),
    transfers: new Map(),
    queue: [],
    uploading: false,
    tunnelBusy: false,
    waiters: new Set(),
    label: guessLabel(),
  };

  const TUNNEL_OPEN = "Page doesn't load? Open a tunnel";
  const TUNNEL_CLOSE = "Close tunnel";

  if (isTouch) {
    els.dropTitle.textContent = "Send a file";
    els.dropSub.textContent = "Tap Select File. It shows up instantly on the other screens.";
  }

  function guessLabel() {
    const ua = navigator.userAgent || "";
    const iPadDesktop = /Macintosh/i.test(ua) && navigator.maxTouchPoints > 1;
    if (/iPhone|iPad|iPod|Android/i.test(ua) || iPadDesktop) return "Phone";
    if (/Macintosh/i.test(ua)) return "Mac";
    if (/Windows/i.test(ua)) return "PC";
    if (isTouch) return "Phone";
    return "Device";
  }

  function fmtBytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n) || 0;
    for (const u of units) {
      if (Math.abs(v) < 1024 || u === units[units.length - 1]) {
        return u === "B" ? `${v | 0} ${u}` : `${v.toFixed(1)} ${u}`;
      }
      v /= 1024;
    }
    return `${n} B`;
  }

  function setStatus(text) {
    els.status.textContent = text;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll("'", "&#39;");
  }

  function newId() {
    return `f-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function waitForMessage(predicate) {
    return new Promise((resolve, reject) => {
      const waiter = { predicate, resolve, reject };
      state.waiters.add(waiter);
    });
  }

  function rejectAllWaiters(err) {
    for (const waiter of [...state.waiters]) {
      state.waiters.delete(waiter);
      waiter.reject(err);
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/ui`);
    ws.binaryType = "arraybuffer";
    state.ws = ws;

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ type: "hello", label: state.label }));
      setStatus("Drop a file to share it with your other devices.");
      pumpQueue();
    });
    ws.addEventListener("close", () => {
      rejectAllWaiters(new Error("socket closed"));
      setStatus("Disconnected. Retrying...");
      setTimeout(connect, 1000);
    });
    ws.addEventListener("message", (ev) => {
      if (typeof ev.data !== "string") return;
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      for (const waiter of [...state.waiters]) {
        if (!waiter.predicate(msg)) continue;
        state.waiters.delete(waiter);
        waiter.resolve(msg);
      }
      handleMessage(msg);
    });
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "hello":
        state.clientId = msg.client_id;
        state.info = msg.info;
        state.clients = msg.clients || [];
        state.files = new Map((msg.files || []).map((f) => [f.id, f]));
        renderInfo();
        renderPresence();
        renderFiles();
        break;
      case "info":
        state.info = msg.info || state.info;
        renderInfo();
        break;
      case "presence":
        state.clients = msg.clients || [];
        renderPresence();
        break;
      case "progress":
        upsertTransfer(msg);
        break;
      case "file":
        if (msg.file) {
          state.files.set(msg.file.id, msg.file);
          renderFiles();
          if (msg.file.from_id !== state.clientId) {
            setStatus(`${msg.file.from_label} shared ${msg.file.filename}`);
          }
        }
        break;
      case "complete":
        upsertTransfer({ ...msg, received: msg.size, total: msg.size, done: true });
        if (msg.id && msg.filename) {
          state.files.set(msg.id, {
            id: msg.id,
            filename: msg.filename,
            size: msg.size,
            sha256: msg.sha256,
            from_id: msg.from_id || state.clientId,
            from_label: msg.from_label || state.label,
            download_url: msg.download_url || `/api/files/${msg.id}`,
            size_label: msg.size_label || fmtBytes(msg.size),
          });
          renderFiles();
        }
        setStatus(`Shared ${msg.filename || "file"}`);
        break;
      case "error":
        upsertTransfer({ ...msg, error: true });
        setStatus(msg.reason || "Transfer failed");
        break;
      default:
        break;
    }
  }

  function renderInfo() {
    if (!state.info) return;
    const url = state.info.url || "";
    els.footUrl.textContent = url;
    els.footVersion.textContent = `v${state.info.version}`;
    if (els.shareUrl) {
      els.shareUrl.href = url;
      els.shareUrl.textContent = url;
    }
    if (els.qrImage && url) {
      els.qrImage.src = `/api/qr.svg?u=${encodeURIComponent(url)}`;
    }
    const tunneled = Boolean(state.info.tunnel);
    if (els.connectBlurb) {
      els.connectBlurb.textContent = tunneled
        ? "Tunnel open. Scan this QR on your phone."
        : "Same Wi-Fi. Scan the QR. If the page doesn't load, open a tunnel.";
    }
    if (els.tunnelBtn && !state.tunnelBusy) {
      els.tunnelBtn.hidden = false;
      els.tunnelBtn.disabled = false;
      els.tunnelBtn.textContent = tunneled ? TUNNEL_CLOSE : TUNNEL_OPEN;
    }
    const fw = state.info.firewall || {};
    if (fw.ok === false) {
      els.firewallNote.hidden = false;
      els.firewallNote.textContent = fw.detail || "Firewall blocked phones. Approve UAC.";
    } else {
      els.firewallNote.hidden = true;
    }
  }

  async function toggleTunnel() {
    if (!els.tunnelBtn || state.tunnelBusy) return;
    const closing = Boolean(state.info && state.info.tunnel);
    state.tunnelBusy = true;
    els.tunnelBtn.disabled = true;
    els.tunnelBtn.textContent = closing ? "Closing tunnel..." : "Opening tunnel...";
    try {
      const res = await fetch("/api/tunnel", { method: closing ? "DELETE" : "POST" });
      const data = await res.json();
      if (!data.ok) {
        els.firewallNote.hidden = false;
        els.firewallNote.textContent = data.detail || "Tunnel action failed.";
        return;
      }
      state.info = data.info || state.info;
    } catch {
      els.firewallNote.hidden = false;
      els.firewallNote.textContent = "Tunnel action failed.";
    } finally {
      state.tunnelBusy = false;
      renderInfo();
    }
  }

  if (els.tunnelBtn) {
    els.tunnelBtn.addEventListener("click", () => {
      toggleTunnel();
    });
  }

  function renderPresence() {
    const others = state.clients.filter((c) => c.id !== state.clientId);
    const n = state.clients.length;
    if (n <= 1) {
      els.presence.textContent = "Only you here. Scan the QR on another device.";
      return;
    }
    els.presence.textContent = `${n} devices here, with ${others.map((c) => c.label).join(", ")}`;
  }

  function renderFiles() {
    const items = [...state.files.values()];
    els.fileCount.textContent = `${items.length} file${items.length === 1 ? "" : "s"}`;
    if (!items.length) {
      els.fileList.innerHTML =
        '<div class="peer-empty">Nothing shared yet. Drop a file on this PC or tap Select File on your phone.</div>';
      return;
    }
    els.fileList.innerHTML = items
      .map((f) => {
        const mine = f.from_id === state.clientId;
        return `
          <article class="file-row glass" data-id="${escapeAttr(f.id)}">
            <div class="file-meta">
              <strong>${escapeHtml(f.filename)}</strong>
              <span>${escapeHtml(f.size_label || fmtBytes(f.size))} from ${escapeHtml(f.from_label || "device")}${mine ? " (you)" : ""}</span>
            </div>
            <a class="download-btn" href="${escapeAttr(f.download_url || `/api/files/${f.id}`)}" download="${escapeAttr(f.filename)}">Download</a>
          </article>
        `;
      })
      .join("");
  }

  function upsertTransfer(msg) {
    const id = msg.id || `anon-${Date.now()}`;
    const prev = state.transfers.get(id) || {};
    state.transfers.set(id, {
      id,
      filename: msg.filename || prev.filename || "file",
      received: msg.received ?? prev.received ?? 0,
      total: msg.total ?? prev.total ?? msg.size ?? 0,
      speed_mbps: msg.speed_mbps ?? prev.speed_mbps ?? 0,
      done: Boolean(msg.done) || msg.type === "complete",
      error: Boolean(msg.error) || msg.type === "error",
      queued: Boolean(msg.queued) && !(msg.done || msg.type === "complete" || msg.error || msg.type === "error"),
      reason: msg.reason || prev.reason || "",
    });
    renderTransfers();
  }

  function renderTransfers() {
    const items = [...state.transfers.values()].filter((t) => !t.done && !t.error).slice(-6);
    if (!items.length) {
      els.transfersSection.hidden = true;
      return;
    }
    els.transfersSection.hidden = false;
    els.transferList.innerHTML = items
      .map((t) => {
        const pct = t.total ? Math.min(100, (t.received / t.total) * 100) : 0;
        const speed = t.queued
          ? "queued"
          : `${(t.speed_mbps || 0).toFixed(2)} MB/s, ${fmtBytes(t.received)} / ${fmtBytes(t.total)}`;
        return `
          <div class="transfer">
            <div class="transfer-top">
              <strong>${escapeHtml(t.filename)}</strong>
              <span class="speed">${speed}</span>
            </div>
            <div class="bar"><i style="width:${t.queued ? 0 : pct.toFixed(2)}%"></i></div>
          </div>
        `;
      })
      .join("");
  }

  function enqueueFiles(fileList) {
    const files = [...fileList].filter(Boolean);
    if (!files.length) return;
    for (const file of files) {
      const id = newId();
      state.queue.push({ id, file });
      upsertTransfer({
        id,
        filename: file.name,
        received: 0,
        total: file.size,
        speed_mbps: 0,
        queued: true,
      });
    }
    setStatus(
      files.length === 1
        ? `Queued ${files[0].name}`
        : `Queued ${files.length} files`
    );
    pumpQueue();
  }

  async function pumpQueue() {
    if (state.uploading) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const next = state.queue.shift();
    if (!next) {
      els.dropzone.classList.remove("is-busy");
      return;
    }
    state.uploading = true;
    els.dropzone.classList.add("is-busy");
    try {
      await sendFile(next.id, next.file);
    } finally {
      state.uploading = false;
      pumpQueue();
    }
  }

  async function sendFile(id, file) {
    setStatus(`Sharing ${file.name}...`);
    upsertTransfer({
      id,
      filename: file.name,
      received: 0,
      total: file.size,
      speed_mbps: 0,
      queued: false,
    });

    const acceptWait = waitForMessage((m) => m.type === "accept" && m.id === id);
    const doneWait = waitForMessage(
      (m) => (m.type === "complete" || m.type === "error") && m.id === id
    );

    state.ws.send(JSON.stringify({ type: "share", id, filename: file.name, size: file.size }));

    try {
      await acceptWait;
      let offset = 0;
      while (offset < file.size) {
        const buf = await file.slice(offset, offset + CHUNK).arrayBuffer();
        state.ws.send(buf);
        offset += buf.byteLength;
      }
      state.ws.send(JSON.stringify({ type: "done", id }));
      const result = await doneWait;
      if (result.type === "error") throw new Error(result.reason || "share failed");
    } catch (err) {
      setStatus(err.message || String(err));
      upsertTransfer({ id, filename: file.name, error: true, reason: err.message });
    }
  }

  function openPicker() {
    els.fileInput.click();
  }

  els.dropzone.addEventListener("click", openPicker);
  els.selectBtn.addEventListener("click", openPicker);
  els.fileInput.addEventListener("change", () => {
    if (els.fileInput.files && els.fileInput.files.length) {
      enqueueFiles(els.fileInput.files);
    }
    els.fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((type) => {
    window.addEventListener(type, (ev) => {
      ev.preventDefault();
      els.dropzone.classList.add("is-dragover");
    });
  });
  window.addEventListener("drop", (ev) => {
    ev.preventDefault();
    els.dropzone.classList.remove("is-dragover");
    if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files.length) {
      enqueueFiles(ev.dataTransfer.files);
    }
  });

  // Allow picking multiple files at once.
  els.fileInput.setAttribute("multiple", "multiple");

  connect();
})();
