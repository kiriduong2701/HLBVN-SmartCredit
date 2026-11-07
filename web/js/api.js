// Shared client utilities: storage, API client (bearer session), formatting, icons, dialogs.
// All business state lives on the server; the UI only renders what it returns.
const Store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
};

class ApiError extends Error {
  constructor(status, code, message, details) { super(message); this.status = status; this.code = code; this.details = details || {}; }
}

async function api(method, path, body, token, headers) {
  const h = Object.assign({ "Content-Type": "application/json" }, headers || {});
  if (token) h.Authorization = "Bearer " + token;
  let res;
  try {
    res = await fetch(path, { method, headers: h, body: body === undefined ? (method === "POST" ? "{}" : undefined) : JSON.stringify(body) });
  } catch (e) { throw new ApiError(0, "NETWORK", "Can't reach the server."); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(res.status, err.code || "HTTP_" + res.status, err.message || "Request failed", err.details);
  }
  return data;
}

const fmt = {
  vnd(n) {
    if (n === null || n === undefined) return "—";
    const v = Number(n);
    return (typeof L !== "undefined" && L.lang === "en") ? "VND " + v.toLocaleString("en-US") : v.toLocaleString("vi-VN") + " ₫";
  },
  num(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString((typeof L !== "undefined" && L.lang === "en") ? "en-US" : "vi-VN"); },
  short(n) {
    const en = typeof L !== "undefined" && L.lang === "en";
    if (n >= 1e6) return (n / 1e6).toLocaleString(en ? "en-US" : "vi-VN", { maximumFractionDigits: 2 }) + (en ? "M" : " tr");
    if (n >= 1e3) return Math.round(n / 1e3) + (en ? "K" : "k");
    return String(n);
  },
  date(s) {
    if (!s) return "—"; const d = new Date(s.length === 10 ? s + "T00:00:00" : s);
    return isNaN(d) ? s : d.toLocaleDateString((typeof L !== "undefined" && L.lang === "en") ? "en-GB" : "vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" });
  },
  time(s) { if (!s) return "—"; const d = new Date(s); return isNaN(d) ? s : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); },
  dt(s) { return fmt.date(s) + " " + fmt.time(s); },
  pct(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString((typeof L !== "undefined" && L.lang === "en") ? "en-US" : "vi-VN", { maximumFractionDigits: 1 }) + "%"; },
  esc(s) { return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
  mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); },
};

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "k-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

// Consistent 24px line icons (decorative: aria-hidden). Generic shapes, not HLB brand assets.
const ICON_PATHS = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  qr: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3",
  bell: "M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8M10 20a2 2 0 0 0 4 0",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  card: "M2 6h20v12H2zM2 10h20M6 15h4",
  wallet: "M3 7h16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H3zM3 7V5h13M16 13h.01",
  loan: "M4 4h12l4 4v12H4zM8 12h8M8 16h5M15 4v4h4",
  layers: "M12 2l10 5-10 5L2 7zM2 12l10 5 10-5M2 17l10 5 10-5",
  gauge: "M12 15l4-6M4 18a9 9 0 1 1 16 0",
  clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  check: "M5 12l5 5L20 7", x: "M6 6l12 12M18 6L6 18", alert: "M12 8v5M12 17h.01M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  info: "M12 11v6M12 7h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z", chevron: "M9 6l6 6-6 6", back: "M15 6l-6 6 6 6",
  bank: "M3 10l9-6 9 6M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18", link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z", doc: "M6 3h9l5 5v13H6zM14 3v6h5", face: "M9 10h.01M15 10h.01M9 15s1 1.5 3 1.5 3-1.5 3-1.5M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3",
  key: "M15 7a4 4 0 1 1-3.9 5H3v3h3v3h3v-3h2.1A4 4 0 0 1 15 7z", lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  store: "M3 9l2-5h14l2 5M4 9v11h16V9M3 9h18M9 20v-6h6v6", refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6",
  menu: "M4 7h16M4 12h16M4 17h16",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  eyeoff: "M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2",
  hand: "M3 14h3v6H3zM6 15h4l4 2 6-3a1.5 1.5 0 0 0-1.5-2.4L14 13h-3M14 3v8M11.5 5.2c0-1 1.1-1.7 2.5-1.7s2.5.7 2.5 1.7-1.1 1.4-2.5 1.8-2.5.8-2.5 1.8 1.1 1.7 2.5 1.7 2.5-.7 2.5-1.7",
  stripes: "M5 9c4-3 9-4 14-3M5 13c4-3 9-4 14-3M5 17c4-3 9-4 14-3",
  phone: "M5 3h4l2 5-3 2a12 12 0 0 0 6 6l2-3 5 2v4a2 2 0 0 1-2 2A18 18 0 0 1 3 5a2 2 0 0 1 2-2z",
  arrow: "M4 12h14M13 6l6 6-6 6M4 8h6M4 16h6", up: "M12 19V5M6 11l6-6 6 6",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18", logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
};
function icon(name, cls) {
  return `<svg class="ic ${cls || ""}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="${ICON_PATHS[name] || ICON_PATHS.info}"/></svg>`;
}

// Accessible modal: focus moves in, Escape closes, focus returns to the opener.
function modal(html, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    const host = document.getElementById(opts.host || "phone") || document.body;
    const opener = document.activeElement;
    const wrap = document.createElement("div");
    wrap.className = "modal-wrap";
    wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1">${html}</div>`;
    const close = (v) => { wrap.remove(); document.removeEventListener("keydown", onKey); if (opener && opener.focus) opener.focus(); resolve(v); };
    const onKey = (e) => {
      if (e.key === "Escape") close(null);
      if (e.key === "Tab") {
        const f = [...wrap.querySelectorAll("button, input, select, textarea, a[href]")].filter(x => !x.disabled);
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (b) close(b.dataset.v);
      else if (e.target === wrap) close(null);
    });
    host.appendChild(wrap);
    if (opts.onOpen) opts.onOpen(wrap, close);
    const first = wrap.querySelector("[autofocus]") || wrap.querySelector("input, button");
    (first || wrap.querySelector(".modal")).focus();
  });
}
function ask(message, okLabel, cancelLabel) {
  return modal(`<p id="modal-title" class="modal-text">${fmt.esc(message)}</p>
    <div class="modal-actions"><button class="btn ghost" data-v="0">${fmt.esc(cancelLabel || "←")}</button><button class="btn primary" data-v="1">${fmt.esc(okLabel)}</button></div>`)
    .then(v => v === "1");
}
