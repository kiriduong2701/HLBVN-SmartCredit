// HLB Connect Smart Credit - customer app (single page, hash routed, bilingual).
// Every screen is derived from server state; nothing here decides credit, prices or payment outcomes.
(function () {
  const $screen = document.getElementById("screen");
  const S = { token: Store.get("sc.token", null), me: null, cfg: null, customers: [] };
  const ui = { method: {}, agree: {}, payKey: {}, conv: {} };
  let pollTimer = null, tickTimer = null, renderToken = 0, lastKey = "";
  const h = (s) => fmt.esc(s);

  // ------------------------------------------------------------ helpers
  const call = (m, p, b, hdr) => api(m, p, b, S.token, hdr);
  function toast(msg, isErr) {
    document.querySelectorAll(".toast").forEach(x => x.remove());
    const d = document.createElement("div");
    d.className = "toast" + (isErr ? " err" : ""); d.setAttribute("role", isErr ? "alert" : "status"); d.textContent = msg;
    document.getElementById("phone").appendChild(d);
    setTimeout(() => d.remove(), 3500);
  }
  function fail(e) {
    if (e && e.code === "SIGN_IN_REQUIRED") { signOut(); return; }
    toast(errText(e || {}), true);
  }
  function stopTimers() { clearInterval(pollTimer); clearInterval(tickTimer); pollTimer = tickTimer = null; }
  function topbar(title, sub, back, right) {
    return `<header class="topbar">${back ? `<a class="hbtn" href="${h(back)}" aria-label="${h(t("common.back"))}">${icon("back")}</a>` : "<span></span>"}
      <h1>${h(title)}${sub ? `<small>${h(sub)}</small>` : ""}</h1>${right || "<span></span>"}</header>`;
  }
  function busy(btn, on) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span>${h(t("common.wait"))}</span>`; }
    else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
  }
  function status(kind, ic, title, body) {
    return `<section class="status ${kind}" aria-label="${h(title)}"><span class="si">${icon(ic, "lg")}</span>
      <div><h2>${h(title)}</h2>${body ? `<p>${body}</p>` : ""}</div></section>`;
  }
  function notice(kind, html) { return `<div class="notice ${kind || ""}" role="note">${icon(kind === "red" || kind === "amber" ? "alert" : "info", "sm")}<div>${html}</div></div>`; }
  function money(params) {
    const out = Object.assign({}, params);
    ["amount", "limit", "fee", "avail", "short", "amt"].forEach(k => { if (typeof out[k] === "number") out[k] = fmt.vnd(out[k]); });
    ["first_due", "due", "d"].forEach(k => { if (out[k]) out[k] = fmt.date(out[k]); });
    return out;
  }
  function paint(html, token) {
    if (token !== renderToken) return false;
    const key = location.hash;
    $screen.innerHTML = html;
    if (key !== lastKey) { $screen.scrollTop = 0; $screen.focus({ preventScroll: true }); }
    lastKey = key;
    return true;
  }
  function chipFor(outcome) {
    const c = { PAYMENT_COMPLETED: "green", PAYMENT_FAILED: "red", CANCELLED: "grey", EXPIRED: "grey", PROCESSING: "blue" }[outcome] || "amber";
    return `<span class="chip ${c}">${h(t("out." + outcome))}</span>`;
  }
  function appChip(st) {
    const c = { APPROVED: "green", ACTION_NEEDED: "amber", IN_REVIEW: "amber", CHECKING: "blue", NOT_APPROVED: "red", STOPPED: "grey", CANCELLED: "grey" }[st] || "grey";
    return `<span class="chip ${c}">${h(t("app.status." + st))}</span>`;
  }
  function plText(code, d) {
    const r = S.cfg.amount_range;
    return t("pl." + code, { min: fmt.vnd(r.min), max: fmt.vnd(r.max), n: S.cfg.conversion_window_days,
      avail: fmt.vnd((d && d.available_vnd) || 0), short: fmt.vnd((d && d.shortfall_vnd) || 0),
      pl: fmt.vnd((d && d.split && d.split.pay_later_vnd) || 0), pn: fmt.vnd((d && d.split && d.split.pay_now_vnd) || 0) });
  }

  const DOCS = {
    vi: { tc: "<p><b>Bản mẫu.</b> Điều khoản sản phẩm Smart Credit giả lập — không phải văn bản pháp lý của HLBVN.</p><h4>1. Sản phẩm</h4><p>Smart Credit là hạn mức trả góp xoay vòng dùng khi quét QR tại cửa hàng đối tác. Mỗi giao dịch có hợp đồng trả góp riêng.</p><h4>2. Đánh giá</h4><p>Hạn mức dựa trên khả năng trả nợ (thu nhập đã xác minh, nghĩa vụ hiện có). Không có lịch sử CIC không phải là lý do từ chối.</p><h4>3. Trả nợ</h4><p>Kỳ quá hạn không có phí trả chậm; gốc quá hạn chịu lãi tối đa 150% lãi suất trong hạn và lãi chậm trả tối đa 10%/năm (Thông tư 39, Điều 13.4), và hạn mức tạm khóa đến khi thanh toán. Không báo cáo CIC trong bản mẫu.</p>",
      privacy: "<p><b>Bản mẫu.</b> Chính sách quyền riêng tư giả lập.</p><h4>Thu thập</h4><p>Dữ liệu tài khoản, giao dịch, thông tin tín dụng CIC (khi bạn cho phép), và dữ liệu thay thế chỉ khi bạn cho phép từng nguồn (lấy qua API mô phỏng, dùng một lần).</p><h4>Mục đích</h4><p>Đánh giá và quản lý hạn mức, xử lý thanh toán, phòng chống gian lận, nhật ký kiểm toán.</p><h4>Quyền của bạn</h4><p>Không cho phép dữ liệu thay thế (không bị từ chối vì điều đó), rút quyền khi yêu cầu đang mở. Cơ sở pháp lý theo Luật Bảo vệ dữ liệu cá nhân 2025 cần HLBVN xác nhận.</p>",
      agreement: "<p><b>Bản mẫu.</b> Hợp đồng trả góp giả lập cho giao dịch này. Số tiền, kỳ hạn, lãi suất và lịch trả nợ hiển thị trên màn hình xác nhận là nội dung của hợp đồng. Không phải hợp đồng tín dụng thật của HLBVN.</p>" },
    en: { tc: "<p><b>Prototype.</b> Simulated Smart Credit product terms — not an HLBVN legal document.</p><h4>1. Product</h4><p>Smart Credit is a revolving instalment limit used when you scan a QR at partner stores. Each purchase has its own instalment agreement.</p><h4>2. Assessment</h4><p>Your limit is based on repayment capacity (verified income, existing commitments). Missing CIC history is never a reason to decline.</p><h4>3. Repayment</h4><p>Overdue instalments carry no late fee; overdue principal bears interest of at most 150% of the contract rate and unpaid interest at most 10%/year (Circular 39, Art. 13.4), and your limit is frozen until paid. Nothing is reported to CIC in this prototype.</p>",
      privacy: "<p><b>Prototype.</b> Simulated Privacy Policy.</p><h4>What we collect</h4><p>Account and transaction data, CIC credit information (with your permission), and alternative data only if you allow each source (retrieved once via simulated APIs).</p><h4>Why</h4><p>To assess and manage your limit, process payments, prevent fraud and keep an audit trail.</p><h4>Your choices</h4><p>Say no to alternative data (you are never declined for it), withdraw permission while a request is open. Legal basis under the 2025 Personal Data Protection Law to be confirmed by HLBVN.</p>",
      agreement: "<p><b>Prototype.</b> Simulated instalment agreement for this purchase. The amount, plan, rate and schedule on the confirmation screen are its terms. Not a real HLBVN credit agreement.</p>" },
  };
  function openDoc(which) {
    return modal(`<h2 id="modal-title">${h(t("doc." + which))}</h2><div class="doc-text">${(DOCS[L.lang] || DOCS.en)[which]}</div>
      <div class="btn-stack" style="margin:14px 0 0"><button class="btn primary" data-v="x">${h(t("common.close"))}</button></div>`);
  }

  // ------------------------------------------------------------ boot + router
  async function boot() {
    try { [S.cfg, S.customers] = await Promise.all([api("GET", "/api/public/config"), api("GET", "/api/public/customers")]); }
    catch (e) { $screen.innerHTML = `<div class="empty">${h(errText(e))}</div>`; return; }
    if (S.token) { try { await refreshMe(); } catch (e) { S.token = null; Store.del("sc.token"); } }
    window.addEventListener("hashchange", route);
    // Deep link from the standalone navigation page: /?as=CUS-001&qr=QR-...#/scan signs that fictional persona in.
    const qs = new URLSearchParams(location.search);
    const as = qs.get("as");
    if (qs.get("qr")) Store.set("sc.pendingQr", qs.get("qr"));
    if (as || qs.get("qr")) history.replaceState(null, "", location.pathname + location.hash);
    if (as && S.customers.some(c => c.customer_id === as) && (!S.me || S.me.customer_id !== as)) {
      try { await signIn(as, location.hash || "#/home"); return; } catch (e) { /* fall through to normal routing */ }
    }
    route();
  }
  // The navigation page (separate window/tab) asks open customer screens to refresh after clock/outage/reset changes.
  try { new BroadcastChannel("smart-credit-demo").onmessage = (e) => { if (e.data && e.data.type === "refresh" && S.token) refreshMe().then(route, () => signOut()); }; } catch (e) { /* older browsers */ }
  async function refreshMe() { S.me = await call("GET", "/api/me"); return S.me; }
  async function signIn(cid, target) {
    const s = await api("POST", "/api/session/customer", { customer_id: cid });
    S.token = s.token; Store.set("sc.token", s.token); Store.set("sc.lastLogin", Store.get("sc.thisLogin", null) || new Date().toISOString());
    Store.set("sc.thisLogin", new Date().toISOString()); await refreshMe();
    if (location.hash === target) route(); else location.hash = target || "#/home";
  }
  function signOut() {
    if (S.token) api("POST", "/api/session/sign-out", {}, S.token).catch(() => {});
    S.token = null; S.me = null; Store.del("sc.token"); location.hash = "#/signin";
  }
  // ------------------------------------------------------------ HLB navigation: drawer + Quick links (no tab bar)
  function lastLogin() { const v = Store.get("sc.lastLogin", null); return v ? fmt.dt(v) : "—"; }
  function openDrawer() {
    const host = document.getElementById("phone");
    const opener = document.activeElement;
    const me = S.me || {};
    const unread = me.unread_notifications || 0;
    const item = (href, ic, label, extra) => `<a class="di" href="${href}">${icon(ic)}<span>${h(label)}</span>${extra || ""}</a>`;
    const wrap = document.createElement("div");
    wrap.className = "drawer-wrap";
    wrap.innerHTML = `<aside class="drawer" role="dialog" aria-modal="true" aria-label="${h(t("drawer.menu"))}" tabindex="-1">
      <div class="dhead"><div class="dav" aria-hidden="true">${icon("user", "lg")}</div><div class="dname">${h(me.display_name || "")}</div>
        <div class="dlast">${h(t("drawer.last_login", { t: lastLogin() }))}</div></div>
      <nav>${item("#/smart-credit", "gauge", t("qa.smartcredit"))}${item("#/loans", "loan", t("qa.loans"))}${item("#/activity", "list", t("qa.history"))}
        ${item("#/products", "card", t("qa.products"))}${item("#/alerts", "bell", t("nav.alerts"), unread ? `<span class="badge">${unread}</span>` : "")}
        ${item("#/profile", "user", t("drawer.profile"))}
        <button class="di" id="d-lang">${icon("globe")}<span>${h(t("profile.lang"))}: ${L.lang === "vi" ? "Tiếng Việt" : "English"}</span></button>
        <div class="dsep"></div>
        <a class="di" href="tel:${h(S.cfg.hotline.replace(/\s/g, ""))}">${icon("phone")}<span>${h(t("drawer.contact"))}</span></a>
        <button class="di" data-doc="privacy">${icon("shield")}<span>${h(t("doc.privacy"))}</span></button></nav>
      <div class="dfoot"><button id="d-out">${icon("logout")}${h(t("profile.signout"))}</button><span class="ver">${h(t("drawer.version"))}</span></div></aside>`;
    const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); if (opener && opener.focus) opener.focus(); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("click", (e) => {
      if (e.target === wrap) return close();
      const d = e.target.closest("[data-doc]"); if (d) { close(); openDoc(d.dataset.doc); return; }
      if (e.target.closest("#d-out")) { close(); signOut(); return; }
      if (e.target.closest("#d-lang")) { L.set(L.lang === "vi" ? "en" : "vi"); close(); refresh(); return; }
      if (e.target.closest("a.di")) close();
    });
    host.appendChild(wrap);
    wrap.querySelector(".drawer").focus();
  }
  function openQuickLinks() {
    const host = document.getElementById("phone");
    const opener = document.activeElement;
    const q = (href, ic, cls, label) => `<a class="qli" href="${href}"><span class="fab ${cls}" style="background:var(--${cls})">${icon(ic)}</span>${h(label)}</a>`;
    const wrap = document.createElement("div");
    wrap.className = "ql-overlay"; wrap.setAttribute("role", "dialog"); wrap.setAttribute("aria-modal", "true"); wrap.setAttribute("aria-label", t("ql.title"));
    wrap.innerHTML = `
      ${q("#/scan", "qr", "accent-pink", t("ql.scan"))}${q("#/loans", "wallet", "accent-pink", t("ql.payloan"))}
      ${q("#/activity", "list", "accent-pink", t("qa.history"))}${q("#/smart-credit", "gauge", "hlb-blue", t("qa.smartcredit"))}
      <button class="qli close" aria-label="${h(t("common.close"))}"><span class="fab">${icon("x", "lg")}</span>${h(t("ql.title"))}</button>`;
    const dock = document.getElementById("ql"); if (dock) dock.style.visibility = "hidden";
    const close = () => { wrap.remove(); if (dock) dock.style.visibility = ""; document.removeEventListener("keydown", onKey); if (opener && opener.focus) opener.focus(); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("click", (e) => { if (e.target.closest(".close") || e.target.closest("a.qli") || e.target === wrap) close(); });
    host.appendChild(wrap);
    wrap.querySelector("a.qli").focus();
  }
  async function route() {
    stopTimers();
    document.querySelectorAll(".modal-wrap, .toast.err").forEach(x => x.remove());
    document.getElementById("proto-strip").textContent = t("app.prototype");
    const token = ++renderToken;
    const parts = (location.hash || "#/home").slice(2).split("?")[0].split("/");
    const name = parts[0] || "home";
    if (!S.token && name !== "signin") { location.hash = "#/signin"; return; }
    const views = { signin: vSignin, home: vHome, products: vProducts, "smart-credit": vSmartCredit, scan: vScan, tx: vTx, loans: vLoans,
      alerts: vAlerts, activity: vActivity, profile: vProfile };
    document.querySelectorAll(".drawer-wrap, .ql-overlay").forEach(x => x.remove());
    try { await (views[name] || vHome)(parts.slice(1), token); }
    catch (e) {
      if (e.code === "SIGN_IN_REQUIRED") return signOut();
      if (token === renderToken) $screen.innerHTML = `${topbar(t("err.generic"), "", "#/home")}<div class="empty">${h(errText(e))}</div>`;
    }
  }
  const refresh = () => route();
  $screen.addEventListener("click", (e) => {
    const d = e.target.closest("[data-doc]"); if (d) { e.preventDefault(); openDoc(d.dataset.doc); }
  });
  function pollUntilChange(fetcher, sigOf, ms) {
    let from = null;
    pollTimer = setInterval(async () => {
      try { const x = await fetcher(); const s = sigOf(x); if (from === null) from = s; else if (s !== from) { refresh(); } } catch (e) { /* keep polling */ }
    }, ms || 3000);
  }

  // ------------------------------------------------------------ sign in
  // Login-screen glyphs drawn to match the HLB Connect account login reference (docs/design, Account_login.png).
  const SI = {
    menu: `<svg class="ic si-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 5h18M3 12h18M3 19h18"/></svg>`,
    qr: `<svg class="ic si-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2 7V3.5A1.5 1.5 0 0 1 3.5 2H7M17 2h3.5A1.5 1.5 0 0 1 22 3.5V7M22 17v3.5a1.5 1.5 0 0 1-1.5 1.5H17M7 22H3.5A1.5 1.5 0 0 1 2 20.5V17M6 6h5v5H6zM13 6h5v5h-5zM6 13h5v5H6zM13 13h2v2h-2zM18 13v5h-5"/></svg>`,
    lock: `<svg class="si-lock" viewBox="0 0 20 22" aria-hidden="true" focusable="false"><path d="M5.5 9.5V6.5a4.5 4.5 0 0 1 9 0v3"/><rect x="1.5" y="9.5" width="17" height="11" rx="2"/><circle cx="10" cy="14.2" r="1.5"/><path d="M10 15.7v2"/></svg>`,
    face: `<svg class="si-face" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path d="M2 15v-5a8 8 0 0 1 8-8h5M33 2h5a8 8 0 0 1 8 8v5M46 33v5a8 8 0 0 1-8 8h-5M15 46h-5a8 8 0 0 1-8-8v-5M15 16v5M33 16v5M24.5 16v10.5a2 2 0 0 1-2 2h-1M13 32.5c6.5 5 15.5 5 22 0"/></svg>`,
  };
  async function vSignin(_, token) {
    const sel = Store.get("sc.signinSel", "CUS-001");
    const cur = S.customers.find(c => c.customer_id === sel) || S.customers[0];
    // Sign-in stays simulated: the demo customer is chosen by tapping the name; the password field is visual only and its value is never read or sent.
    paint(`<div class="signin">
      <div class="si-nav"><button type="button" class="hbtn" id="si-menu" aria-label="${h(t("signin.menu"))}">${SI.menu}</button><span class="spacer"></span>
        <button type="button" class="hbtn" data-prelogin aria-label="${h(t("ql.scan"))}">${SI.qr}</button>
        <button type="button" class="hbtn" data-prelogin aria-label="${h(t("nav.alerts"))}">${icon("bell")}</button></div>
      <h1 class="si-logo"><img src="/img/hlb-connect-logo.png" alt="HLB Connect" width="200" height="73"></h1>
      <p class="login-hello">${h(t("signin.hello"))} <span class="who-wrap"><b id="hello-name">${h(cur ? cur.display_name : "")}</b>${icon("chevron", "sm")}
        <select id="who" aria-label="${h(t("signin.pick"))}">${S.customers.map(c => `<option value="${h(c.customer_id)}" ${c.customer_id === cur.customer_id ? "selected" : ""}>${h(c.display_name)}</option>`).join("")}</select></span></p>
      <form class="login-card" id="login-form" autocomplete="off" novalidate>
        <div class="pw-row">${SI.lock}<input id="pw" type="password" placeholder="${h(t("signin.password"))}" aria-label="${h(t("signin.password"))}" autocomplete="off" autocapitalize="off" spellcheck="false">
          <button type="button" class="pw-eye" id="pw-eye" aria-pressed="false" aria-label="${h(t("signin.show_pw"))}">${icon("eyeoff")}</button></div>
        <div class="login-row"><button type="submit" class="btn primary" id="go">${h(t("signin.button"))}</button>
          <button type="button" class="face-btn" id="face" aria-label="${h(t("signin.face"))}">${SI.face}</button></div></form>
      <div class="si-reset"><button type="button" id="reset-pw">${h(t("signin.reset"))}</button></div>
      <img class="si-kkct" src="/img/kkct.png" alt="#KKCT · khoảnh khắc của tôi" width="150" height="129">
      <p class="si-note">${h(t("signin.note"))}</p></div>`, token);
    const who = document.getElementById("who");
    who.onchange = () => { Store.set("sc.signinSel", who.value); document.getElementById("hello-name").textContent = who.options[who.selectedIndex].text; };
    const go = async (btn) => { busy(btn, true); try { await signIn(who.value, "#/home"); } catch (err) { busy(btn, false); fail(err); } };
    document.getElementById("login-form").onsubmit = (e) => { e.preventDefault(); go(document.getElementById("go")); };
    document.getElementById("face").onclick = (e) => go(e.currentTarget);
    const pw = document.getElementById("pw"), eye = document.getElementById("pw-eye");
    eye.onclick = () => {
      const show = pw.type === "password";
      pw.type = show ? "text" : "password"; eye.setAttribute("aria-pressed", String(show));
      eye.setAttribute("aria-label", t(show ? "signin.hide_pw" : "signin.show_pw")); eye.innerHTML = icon(show ? "eye" : "eyeoff");
    };
    document.getElementById("reset-pw").onclick = () => modal(`<h2 id="modal-title">${h(t("signin.reset"))}</h2><p class="modal-text">${h(t("signin.reset_text"))}</p>
      <div class="btn-stack" style="margin:14px 0 0"><button class="btn primary" data-v="x">${h(t("common.close"))}</button></div>`);
    document.getElementById("si-menu").onclick = async () => {
      const v = await modal(`<h2 id="modal-title">${h(t("profile.lang"))}</h2>
        <div class="modal-actions"><button class="btn ${L.lang === "vi" ? "primary" : "ghost"}" data-v="vi" aria-pressed="${L.lang === "vi"}">Tiếng Việt</button>
          <button class="btn ${L.lang === "en" ? "primary" : "ghost"}" data-v="en" aria-pressed="${L.lang === "en"}">English</button></div>`);
      if (v && v !== L.lang) { L.set(v); refresh(); }
    };
    $screen.querySelectorAll("[data-prelogin]").forEach(b => b.onclick = () => toast(t("signin.first")));
  }

  // ------------------------------------------------------------ home
  function greeting() { const hr = new Date().getHours(); return t(hr < 11 ? "greet.morning" : hr < 18 ? "greet.afternoon" : "greet.evening"); }
  function scTile(cr) {
    const st = cr.line_status;
    if (st === "NONE" || st === "CLOSED") {
      const open = cr.open_application;
      return `<a class="tile promo" href="${open ? "#/smart-credit/app/" + h(open.application_id) : "#/smart-credit"}"><span class="ti blue">${icon("gauge")}</span>
        <span class="tm"><span class="tt">${h(t("home.sc"))}</span><span class="ts">${h(open ? t("app.status.IN_REVIEW") + " · " + t("common.view") : t("home.sc_tag"))}</span></span></a>`;
    }
    const used = cr.outstanding_principal_vnd + cr.held_vnd;
    const pct = cr.limit_vnd ? Math.min(100, Math.round(100 * used / cr.limit_vnd)) : 0;
    return `<a class="tile promo" href="#/smart-credit"><span class="ti blue">${icon("gauge")}</span>
      <span class="tm"><span class="tt">${h(t("home.sc"))}</span><span class="ts">${st === "ACTIVE" ? h(t("home.sc_available")) + ": <b>" + fmt.vnd(cr.available_vnd) + "</b>" : h(t("sc.status." + st))}</span>
        <span class="meter" role="img" aria-label="${h(t("home.sc_used"))} ${pct}%"><i style="width:${pct}%"></i></span></span></a>`;
  }
  function scCard(cr) {  // Smart Credit hub summary (white card)
    const used = cr.outstanding_principal_vnd + cr.held_vnd;
    const pct = cr.limit_vnd ? Math.min(100, Math.round(100 * used / cr.limit_vnd)) : 0;
    return `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><span class="label">${h(t("home.sc_available"))}</span>
        <span class="chip ${cr.line_status === "ACTIVE" ? "green" : "red"}">${h(t("sc.status." + cr.line_status))}</span></div>
      <div class="amount blue" style="margin-top:4px">${fmt.vnd(cr.available_vnd)}</div>
      <div class="bar" role="img" aria-label="${pct}%"><i style="width:${pct}%"></i></div>
      <dl class="kv" style="margin-top:12px"><dt>${h(t("home.sc_limit"))}</dt><dd>${fmt.vnd(cr.limit_vnd)}</dd><dt>${h(t("home.sc_used"))}</dt><dd>${fmt.vnd(cr.outstanding_principal_vnd)}</dd>
        ${cr.held_vnd ? `<dt>${h(t("home.sc_held"))}</dt><dd>${fmt.vnd(cr.held_vnd)}</dd>` : ""}</dl></div>`;
  }
  async function vHome(_, token) {
    const [me, hist] = await Promise.all([refreshMe(), call("GET", "/api/transactions")]);
    const active = hist.find(v => ["CHOOSE_METHOD", "REVIEW_PAY_NOW", "REVIEW_PAY_LATER", "PAYMENT_FAILED", "PROCESSING"].includes(v.outcome));
    const cr = me.credit;
    const show = Store.get("sc.showBalance", false);
    const loanDue = cr.outstanding_total_vnd > 0;
    paint(`<div class="home-head"><button class="hbtn" id="menu" aria-label="${h(t("drawer.menu"))}">${icon("menu")}</button><span class="spacer"></span>
        <a class="hbtn" href="#/scan" aria-label="${h(t("qa.scan"))}">${icon("qr")}</a>
        <a class="hbtn" href="#/alerts" aria-label="${h(t("nav.alerts"))}">${icon("bell")}${me.unread_notifications ? `<span class="badge-dot">${me.unread_notifications}</span>` : ""}</a></div>
      <p class="greet-c">${h(t("home.hi"))} <b>${h(me.display_name)}</b>!<br>${h(t("home.total_balance"))}</p>
      <button class="balance-pill" id="bal" aria-pressed="${show}" aria-label="${h(t(show ? "home.hide_balance" : "home.show_balance"))}">
        <span>${show ? fmt.vnd(me.balance_vnd) : "* VND"}</span>${icon(show ? "eye" : "eyeoff")}</button>
      <div class="tiles">
        ${active ? `<a class="tile" href="#/tx/${h(active.transaction.transaction_id)}"><span class="ti orange">${icon("clock")}</span><span class="tm"><span class="tt">${h(t("home.in_progress"))}</span>
          <span class="ts">${h(active.merchant.name)}</span></span><span class="tr"><b>${fmt.vnd(active.transaction.amount_vnd)}</b></span></a>` : ""}
        <a class="tile" href="#/activity"><span class="ti teal">${icon("wallet")}</span><span class="tm"><span class="tt">${h(t("home.account"))}</span><span class="ts">${h(me.account_masked)}</span></span>
          <span class="tr"><b>${show ? fmt.short(me.balance_vnd) : "*"}</b>VND</span></a>
        ${scTile(cr)}
        ${loanDue ? `<a class="tile" href="#/loans"><span class="ti ${cr.delinquency.arrears ? "pink" : "lblue"}">${icon("loan")}</span><span class="tm"><span class="tt">${h(t("qa.loans"))}</span>
          <span class="ts">${h(cr.delinquency.arrears ? t("inst.OVERDUE") : t("loans.outstanding"))}</span></span><span class="tr"><b>${fmt.short(cr.outstanding_total_vnd)}</b>VND</span></a>` : ""}
        <button class="tile" id="pl-tile"><span class="ti lblue">${icon("hand")}</span><span class="tm"><span class="tt">${h(t("products.pl"))}</span><span class="ts">${h(t("home.pl_sub"))}</span></span></button>
      </div>
      ${cr.line_status === "FROZEN" ? notice("red", `<b>${h(t("sc.status.FROZEN"))}</b> — ${h(t("sc.frozen_note"))} <a href="#/loans">${h(t("qa.payloan"))}</a>`) : ""}
      <div class="ql-dock"><button class="ql-btn" id="ql"><span class="fab">${icon("stripes", "lg")}</span>${h(t("ql.title"))}</button></div>`, token);
    document.getElementById("menu").onclick = openDrawer;
    document.getElementById("ql").onclick = openQuickLinks;
    document.getElementById("bal").onclick = () => { Store.set("sc.showBalance", !show); refresh(); };
    document.getElementById("pl-tile").onclick = () => modal(`<h2 id="modal-title">${h(t("products.pl"))}</h2><p class="modal-text">${h(t("home.pl_info"))}</p>
      <div class="btn-stack" style="margin:0"><button class="btn primary" data-v="x">${h(t("common.close"))}</button></div>`);
  }
  function histList(items) {
    if (!items.length) return `<div class="list"><div class="empty">${h(t("activity.none"))}</div></div>`;
    return `<div class="list">${items.map(v => `<a class="item" href="#/tx/${h(v.transaction.transaction_id)}"><span class="avatar ${v.transaction.method === "PAY_LATER" ? "" : "teal"}" aria-hidden="true">${icon(v.transaction.method === "PAY_LATER" ? "gauge" : "qr", "sm")}</span>
      <span class="main"><span class="t">${h(v.merchant.name)}</span><span class="s">${fmt.date(v.transaction.created_at)} · ${h(v.transaction.method ? t("m." + v.transaction.method) : "—")}</span></span>
      <span class="r">${fmt.vnd(v.transaction.amount_vnd)}<br>${chipFor(v.outcome)}</span></a>`).join("")}</div>`;
  }

  // ------------------------------------------------------------ products (Loans & Cards) -> Smart Credit
  async function vProducts(_, token) {
    const me = await refreshMe();
    paint(`${topbar(t("products.title"), "", "#/home")}<p class="sec-head">${h(t("products.q"))}</p>
      <div class="list"><a class="item" href="#/smart-credit"><span class="avatar">${icon("gauge", "sm")}</span><span class="main"><span class="t">${h(t("products.sc"))} <span class="chip blue">${h(t("products.new"))}</span></span>
        <span class="s">${h(t("products.sc_sub"))}</span></span><span class="chip ${me.credit.line_status === "ACTIVE" ? "green" : "grey"}">${h(t("sc.status." + me.credit.line_status))}</span>${icon("chevron", "sm chev")}</a></div>
      <p class="sec-head">${h(t("products.existing"))}</p>
      <div class="list"><div class="item"><span class="avatar lblue">${icon("hand", "sm")}</span><span class="main"><span class="t">${h(t("products.pl"))}</span><span class="s">${h(t("products.pl_sub"))}</span></span></div>
        <div class="item"><span class="avatar teal">${icon("card", "sm")}</span><span class="main"><span class="t">${h(t("products.debit"))}</span><span class="s">${h(t("products.debit_sub"))}</span></span></div></div>`, token);
  }

  // ------------------------------------------------------------ Smart Credit hub, activation, applications
  async function vSmartCredit(params, token) {
    if (params[0] === "activate") return vActivate(token);
    if (params[0] === "app") return vApplication(params[1], token);
    if (params[0] === "increase") return vIncrease(token);
    const [me, apps] = await Promise.all([refreshMe(), call("GET", "/api/applications")]);
    const cr = me.credit;
    const open = cr.open_application;
    const st = cr.line_status;
    const reqs = apps.length ? `<p class="sec-head">${h(t("sc.requests"))}</p><div class="list">${apps.map(a => `<a class="item" href="#/smart-credit/app/${h(a.application_id)}">
        <span class="main"><span class="t">${h(t("app.kind." + a.kind))}</span><span class="s">${fmt.dt(a.created_at)}</span></span>${appChip(a.status)}${icon("chevron", "sm chev")}</a>`).join("")}</div>` : "";
    if (st === "NONE" || st === "CLOSED" || st === "SUSPENDED") {
      paint(`${topbar("", "", "#/products")}<div class="landing">
          <div class="logo-badge"><img src="/img/hlb-app-icon.png" alt="Hong Leong Bank"></div>
          <div class="promo-card"><h2>${h(t("products.sc"))}</h2><p>${h(t("sc.promo"))}</p><span class="art" aria-hidden="true">${icon("qr")}</span></div>
          <ul class="features"><li><span class="ti teal">${icon("gauge")}</span>${h(t("sc.f1"))}</li><li><span class="ti pink">${icon("arrow")}</span>${h(t("sc.f2"))}</li>
            <li><span class="ti orange">${icon("card")}</span>${h(t("sc.f3", { n: S.cfg.conversion_window_days }))}</li></ul></div>
        ${st === "SUSPENDED" ? notice("red", h(t("sc.suspended_note", { hotline: S.cfg.hotline }))) : ""}
        <div class="btn-stack" style="margin-top:22px">${open ? `<a class="btn primary" href="#/smart-credit/app/${h(open.application_id)}">${h(t("app.kind." + open.kind))} · ${h(t("common.view"))}</a>`
          : st === "SUSPENDED" ? "" : `<a class="btn primary" href="#/smart-credit/activate">${h(t("sc.activate"))}</a>`}
          <button class="ulink" data-doc="tc">${h(t("act.terms_link"))}</button></div>${reqs}`, token);
      return;
    }
    paint(`${topbar(t("sc.title"), "", "#/home")}${scCard(cr)}
      <div class="actpanel"><a href="#/scan"><span class="ti pink">${icon("qr")}</span>${h(t("ql.scan"))}</a>
        <a href="#/loans"><span class="ti teal">${icon("wallet")}</span>${h(t("ql.payloan"))}</a>
        ${open ? `<a href="#/smart-credit/app/${h(open.application_id)}"><span class="ti lblue">${icon("clock")}</span>${h(t("app.kind." + open.kind))}</a>`
          : st === "ACTIVE" ? `<a href="#/smart-credit/increase"><span class="ti lblue">${icon("up")}</span>${h(t("sc.increase_short"))}</a>` : `<a href="#/activity"><span class="ti lblue">${icon("list")}</span>${h(t("qa.history"))}</a>`}</div>
      ${st === "FROZEN" ? notice("red", h(t("sc.frozen_note")) + ` <a href="#/loans">${h(t("qa.payloan"))}</a>`) : ""}
      ${cr.review_in_progress ? notice("", h(t("sc.review_progress"))) : ""}
      <div class="card"><h3>${h(t("sc.exposure"))}</h3><dl class="kv"><dt>${h(t("sc.exposure_sc"))}</dt><dd>${fmt.vnd(cr.outstanding_principal_vnd)}</dd>
        ${cr.other_hlb_exposure.map(p => `<dt>${h(t("products.pl"))}</dt><dd>${fmt.vnd(p.outstanding_vnd)}</dd>`).join("")}</dl>
        <p class="note" style="margin:10px 0 0">${h(t("sc.basis"))}</p></div>
      <details class="more"><summary>${h(t("sc.how"))}</summary><div><ol style="margin:0;padding-left:18px;font-size:14px;display:grid;gap:6px">
        <li>${h(t("sc.how1"))}</li><li>${h(t("sc.how2"))}</li><li>${h(t("sc.how3", { n: S.cfg.conversion_window_days }))}</li></ol></div></details>${reqs}
      <div class="btn-stack"><button class="btn link" id="sc-withdraw">${h(t("sc.withdraw"))}</button></div>`, token);
    const wd = document.getElementById("sc-withdraw");
    if (wd) wd.onclick = async () => {
      if (!await ask(t("sc.withdraw_ask"), t("common.confirm"), t("common.back"))) return;
      try { await call("POST", "/api/smart-credit/withdraw-consent"); toast(t("sc.withdrawn")); refresh(); } catch (e) { fail(e); }
    };
  }

  function consentForm() {
    return `<fieldset><legend>${h(t("act.consents"))}</legend>${S.cfg.activation_consents.map(c => `<label class="check"><input type="checkbox" data-consent="${h(c.id)}">
      <span>${h(t("act.consent." + c.id))}${c.id === "SC_TERMS" ? ` <a href="#" data-doc="tc">${h(t("act.terms_link"))}</a> · <a href="#" data-doc="privacy">${h(t("act.privacy_link"))}</a>` : ""}</span></label>`).join("")}</fieldset>
      <p class="note">${h(t("act.consent_note"))}</p>`;
  }
  function readConsents(root) { const o = {}; root.querySelectorAll("[data-consent]").forEach(x => { o[x.dataset.consent] = x.checked; }); return o; }
  function allConsented(root) { return [...root.querySelectorAll("[data-consent]")].every(x => x.checked); }

  async function vActivate(token) {
    paint(`${topbar(t("act.title"), "", "#/smart-credit")}<div class="card"><p style="margin-top:0">${h(t("act.intro"))}</p>${consentForm()}
        <label class="field">${h(t("act.desired"))}<input id="desired" type="number" inputmode="numeric" min="${S.cfg.min_limit_vnd}" step="500000" placeholder="—"></label>
        <p class="note">${h(t("act.desired_note"))}</p></div>
      <div class="btn-stack"><button class="btn primary" id="go" disabled>${h(t("act.submit"))}</button></div>`, token);
    const go = document.getElementById("go");
    $screen.querySelectorAll("[data-consent]").forEach(x => x.onchange = () => { go.disabled = !allConsented($screen); });
    go.onclick = async () => {
      busy(go, true);
      const want = parseInt(document.getElementById("desired").value, 10);
      try { const a = await call("POST", "/api/smart-credit/activate", Object.assign({ consents: readConsents($screen) }, Number.isInteger(want) && want > 0 ? { requested_limit_vnd: want } : {}));
        await refreshMe(); location.hash = "#/smart-credit/app/" + a.application_id; }
      catch (e) { busy(go, false); fail(e); }
    };
  }

  function reasonBlock(a) {
    const r = a.customer_reason || "VERIFYING_DATA";
    const kind = { APPROVED: "green", NOT_APPROVED: "red", STOPPED: "grey", CANCELLED: "grey", CHECKING: "blue" }[a.status] || "amber";
    const ic = { green: "check", red: "x", grey: "info", blue: "clock" }[kind] || "clock";
    if (a.status === "APPROVED") return status("green", "check", t("reason.APPROVED.t"), `${h(t("app.approved_limit"))}: <b>${fmt.vnd(a.approved_limit_vnd)}</b>`);
    if (a.status === "OFFER") return status("blue", "info", t("reason.COUNTER_OFFER.t"), h(t("reason.COUNTER_OFFER.b")));
    if (a.status === "CANCELLED") return status("grey", "info", t("app.status.CANCELLED"), "");
    if (a.status === "IN_REVIEW") return status("amber", "clock", t("reason.VERIFYING_DATA.t"), h(t("reason.VERIFYING_DATA.b")));
    return status(kind, ic, t("reason." + r + ".t"), h(t("reason." + r + ".b")));
  }
  const altLabel = (src) => { const k = "src." + src.id + ".t"; const v = t(k); return v === k ? src.label : v; };
  const altPurpose = (src) => { const k = "src." + src.id + ".p"; const v = t(k); return v === k ? src.purpose : v; };
  async function vApplication(id, token) {
    const a = await call("GET", "/api/applications/" + id);
    const req = a.info_request || { options: [], sources: [], purposes: [] };
    const altMode = a.status === "ACTION_NEEDED" && req.route === "ALTERNATIVE_DATA";
    const opts = req.options || [];
    let body = "";
    if (a.out_of_scope) body += notice("", h(t("app.oos", { max: fmt.vnd(a.out_of_scope.max_vnd) })));
    if (a.checking_reason === "SOURCE_RETRY") body += notice("", h(t("app.retrying")));
    if (a.status === "ACTION_NEEDED") {
      if (altMode) {
        // v6: only the requested sources and why each helps - never which bank data was assessed or missing.
        body += `<div class="card"><h3>${h(t("alt.why_t"))}</h3><ul class="why">${(req.purposes || []).map(m => `<li>${h(t("alt.purpose." + m))}</li>`).join("")}</ul></div>`;
        body += `<div class="card" id="alt-card"><h3>${h(t("alt.choose_t"))}</h3><p class="note" style="margin-top:0">${h(t("alt.choose_sub"))}</p>
          ${req.sources.map(src => `<label class="check alt-src" data-src="${h(src.id)}"><input type="checkbox" name="alt-${h(src.id)}" value="${h(src.id)}">
            <span><b>${h(altLabel(src))}</b><br><span class="alt-p">${h(altPurpose(src))}</span><span class="alt-status" aria-live="polite"></span></span></label>`).join("")}
          <p class="note">${h(t("alt.sim_note"))}</p></div>`;
      }
    } else if (a.status === "OFFER" && a.offer) {
      body += `<div class="card"><h3>${h(t("offer.t"))}</h3><dl class="kv"><dt>${h(t("offer.requested"))}</dt><dd>${fmt.vnd(a.offer.requested_vnd)}</dd>
        <dt>${h(t("offer.offered"))}</dt><dd><b>${fmt.vnd(a.offer.limit_vnd)}</b></dd></dl><p class="note" style="margin-bottom:0">${h(t("offer.note"))}</p></div>`;
    } else if (a.status === "IN_REVIEW") {
      body += notice("", h(t("app.review_eta", { h: a.expected_decision_hours || S.cfg.expected_decision_hours })) + "<br>" + h(t("app.not_at_counter")));
    } else if (a.status === "APPROVED") {
      if (a.counter_offer) body += notice("", h(t("app.counter")));
      body += `<div class="btn-stack"><a class="btn primary" href="#/scan">${h(t("app.go_shop"))}</a></div>`;
    } else if (a.status === "NOT_APPROVED") {
      const r = a.customer_reason;
      body += `<div class="card"><h3>${L.lang === "vi" ? "Bạn có thể làm gì" : "What you can do"}</h3><p style="margin:0">${h(t("todo." + (["CAPACITY", "CAPACITY_UNVERIFIED", "INCREASE_NOT_SUPPORTED", "ARREARS", "DEFAULT", "OFFICER_DECLINED"].includes(r) ? r : "generic"), { hotline: S.cfg.hotline }))}</p>
        ${a.appeal_available ? `<label class="field">${h(t("app.appeal_reason"))}<textarea id="appeal-r" rows="2"></textarea></label><button class="btn ghost" id="appeal">${h(t("app.appeal"))}</button>` : ""}
        <p class="note" style="margin-top:10px">Hotline ${h(S.cfg.hotline)}</p></div>`;
    }
    const openStates = ["ACTION_NEEDED", "IN_REVIEW", "CHECKING"];
    paint(`${topbar(t("app.kind." + a.kind), fmt.dt(a.created_at), "#/smart-credit")}${reasonBlock(a)}${body}
      <div class="btn-stack">
        ${altMode ? `<button class="btn primary" id="alt-go">${h(t("alt.continue"))}</button>` : ""}
        ${a.status === "OFFER" ? `<button class="btn primary" id="offer-ok">${h(t("offer.accept"))}</button><button class="btn ghost" id="offer-more">${h(t("offer.more"))}</button>
          <button class="btn link" id="offer-no">${h(t("offer.decline"))}</button>` : ""}
        ${openStates.includes(a.status) ? `<button class="btn link" id="cancel">${h(t("app.cancel"))}</button><button class="btn link" id="withdraw">${h(t("app.withdraw"))}</button>` : ""}
      </div>`, token);
    const base = "/api/applications/" + a.application_id;
    const offerBtn = (id, path, body) => { const b = document.getElementById(id); if (b) b.onclick = async () => { busy(b, true);
      try { await call("POST", base + path, body); await refreshMe(); refresh(); } catch (e) { busy(b, false); fail(e); } }; };
    offerBtn("offer-ok", "/accept-offer");
    offerBtn("offer-more", "/decline-offer", { ask_for_more: true });
    offerBtn("offer-no", "/decline-offer", { ask_for_more: false });
    const altGo = document.getElementById("alt-go");
    if (altGo) altGo.onclick = async () => {
      const consents = {};  // unticked = not allowed (optional, explicit, per source)
      for (const src of req.sources) consents[src.id] = !!$screen.querySelector(`input[name="alt-${src.id}"]`).checked;
      // Loading state per consented source while the (simulated) providers are called.
      $screen.querySelectorAll(".alt-src").forEach(fs => { const st = fs.querySelector(".alt-status"); fs.querySelectorAll("input").forEach(i => i.disabled = true);
        st.innerHTML = consents[fs.dataset.src] ? `<span class="spin" aria-hidden="true"></span>${h(t("alt.connecting"))}` : h(t("alt.out.DECLINED")); });
      busy(altGo, true);
      try {
        const [res] = await Promise.all([call("POST", base + "/alternative-data", { consents }), new Promise(r => setTimeout(r, 700))]);
        const rounds = (res.data_acquisition && res.data_acquisition.layer2) || [];
        const last = rounds[rounds.length - 1] || { results: [], declined: [] };
        await modal(`<h2 id="modal-title">${h(t("alt.results_t"))}</h2><div class="card" style="box-shadow:none;padding:0">${req.sources.map(src => {
          const x = last.results.find(r => r.source === src.id); const out = x ? x.outcome : "DECLINED";
          return `<div class="doc-row"><span class="dm"><b>${h(altLabel(src))}</b></span><span class="chip ${out === "ACCEPTED" ? "green" : out === "UNAVAILABLE" || out === "INVALID" ? "amber" : "grey"}">${h(t("alt.out." + out))}</span></div>`; }).join("")}</div>
          <p class="modal-text">${h(res.status === "IN_REVIEW" ? t("alt.next_review") : t("reason." + (res.customer_reason || "VERIFYING_DATA") + ".b"))}</p>
          <div class="btn-stack" style="margin:14px 0 0"><button class="btn primary" data-v="x">${h(t("common.close"))}</button></div>`);
        refresh();
      } catch (e) { busy(altGo, false); fail(e); refresh(); }
    };
    const can = document.getElementById("cancel");
    if (can) can.onclick = async () => { if (await ask(t("app.cancel") + "?", t("common.confirm"), t("common.back"))) { try { await call("POST", base + "/cancel"); refresh(); } catch (e) { fail(e); } } };
    const wd = document.getElementById("withdraw");
    if (wd) wd.onclick = async () => { if (await ask(t("app.withdraw") + "?", t("common.confirm"), t("common.back"))) { try { await call("POST", base + "/withdraw-consent"); refresh(); } catch (e) { fail(e); } } };
    const ap = document.getElementById("appeal");
    if (ap) ap.onclick = async () => { busy(ap, true); try { await call("POST", base + "/appeal", { reason: document.getElementById("appeal-r").value }); refresh(); } catch (e) { busy(ap, false); fail(e); } };
    if (openStates.includes(a.status) && !altMode) pollUntilChange(() => call("GET", "/api/applications/" + id), x => x.status + "|" + x.version, 4000);
  }

  async function vIncrease(token) {
    const me = await refreshMe();
    const q = new URLSearchParams((location.hash.split("?")[1]) || "");
    const need = +q.get("need") || 0;
    const suggest = Math.min(S.cfg.amount_range.max, Math.max(me.credit.limit_vnd + 1000000, need ? me.credit.outstanding_principal_vnd + me.credit.held_vnd + need : 0));
    paint(`${topbar(t("inc.title"), "", "#/smart-credit")}<div class="card"><dl class="kv"><dt>${h(t("home.sc_limit"))}</dt><dd>${fmt.vnd(me.credit.limit_vnd)}</dd></dl>
      <label class="field">${h(t("inc.amount"))}<input id="amt" type="number" inputmode="numeric" step="500000" value="${Math.ceil(suggest / 500000) * 500000}"></label>
      <label class="field">${h(t("inc.reason"))}<input id="rsn" maxlength="200"></label><p class="note">${h(t("inc.note"))}</p></div>
      <div class="btn-stack"><button class="btn primary" id="go">${h(t("inc.submit"))}</button></div>`, token);
    document.getElementById("go").onclick = async (e) => {
      busy(e.currentTarget, true);
      try { const a = await call("POST", "/api/smart-credit/limit-increase", { requested_limit_vnd: parseInt(document.getElementById("amt").value, 10), reason: document.getElementById("rsn").value, transaction_id: q.get("tx") || null });
        location.hash = "#/smart-credit/app/" + a.application_id; } catch (err) { busy(e.currentTarget, false); fail(err); }
    };
  }

  // ------------------------------------------------------------ scan
  async function vScan(_, token) {
    const qrs = await call("GET", "/api/qr/demo-codes");
    const hi = Store.get("sc.pendingQr", null);
    paint(`${topbar(t("scan.title"), "", "#/home")}<div class="scanner" aria-hidden="true">
        <div class="lockup"><span>VietQR</span><span class="sep"></span><img src="/img/hlb-app-icon.png" alt=""><span>HLB Connect</span></div>
        <div class="frame"><i></i><i></i><i></i><i></i>${icon("qr")}</div><div class="napas">NAPAS 247</div><p>${h(t("scan.camera"))}</p></div>
      <p class="sec-head">${h(t("scan.codes"))}</p>
      <div class="list" id="qrs">${qrs.map(q => `<button class="item" data-payload="${h(q.payload)}" data-kind="${h(q.kind)}" data-ref="${h(q.qr_ref)}" ${q.qr_ref === hi ? 'style="background:var(--tint);box-shadow:inset 4px 0 0 var(--hlb-blue)"' : ""}>
        <span class="qrmini" aria-hidden="true"></span><span class="main"><span class="t">${h(q.merchant_name)}</span><span class="s">${h(q.description)}</span></span>
        <span class="r">${q.kind === "STATIC" ? `<span class="chip blue">${h(t("scan.static"))}</span>` : fmt.vnd(q.amount_vnd)}${q.already_paid ? `<br><span class="chip green">${h(t("scan.paid"))}</span>` : ""}</span></button>`).join("")}</div>`, token);
    document.getElementById("qrs").onclick = async (e) => {
      const b = e.target.closest("[data-payload]"); if (!b) return;
      let amount;
      if (b.dataset.kind === "STATIC") {
        const v = await modal(`<h2 id="modal-title">${h(t("scan.amount"))}</h2><label class="field"><span class="sr-only">${h(t("tx.amount"))}</span><input id="amt-in" type="number" inputmode="numeric" min="1000" step="1000" autofocus></label>
          <div class="modal-actions"><button class="btn ghost" data-v="">${h(t("common.cancel"))}</button><button class="btn primary" data-v="ok">${h(t("common.continue"))}</button></div>`,
          { onOpen: (w) => { w.querySelector("[data-v=ok]").addEventListener("click", () => { amount = parseInt(w.querySelector("#amt-in").value, 10); }, true); } });
        if (v !== "ok" || !amount) return;
      }
      busy(b, true);
      try { const r = await call("POST", "/api/qr/resolve", { payload: b.dataset.payload, amount_vnd: amount }); Store.del("sc.pendingQr"); location.hash = "#/tx/" + r.view.transaction.transaction_id; }
      catch (err) { busy(b, false); if (err.code === "QR_ALREADY_PAID" && err.details.transaction_id) { toast(errText(err), true); setTimeout(() => location.hash = "#/tx/" + err.details.transaction_id, 1200); } else fail(err); }
    };
    if (hi) { const el = $screen.querySelector(`[data-ref="${hi}"]`); if (el) el.scrollIntoView({ block: "center" }); }
  }

  // ------------------------------------------------------------ transaction screens
  function purchaseCard(v) {
    return `<div class="card"><div style="display:flex;gap:12px;align-items:center"><span class="avatar" aria-hidden="true">${h(v.merchant.initials)}</span>
      <div style="flex:1"><div class="note">${h(t("tx.to"))}</div><b>${h(v.merchant.name)}</b><div class="note">${h(v.merchant.category)} · ${h(v.merchant.city)}</div></div></div>
      <div class="hr"></div><div class="note">${h(t("tx.amount"))}</div><div class="amount">${fmt.vnd(v.transaction.amount_vnd)}</div>
      <div class="note">${h(v.transaction.description)} · ${v.transaction.qr_kind === "DYNAMIC" ? h(t("tx.verified")) : h(t("tx.entered"))}</div></div>`;
  }
  function timerBar(v) {
    if (v.seconds_left === null || v.seconds_left === undefined) return "";
    return `<div class="timer ${v.seconds_left < 60 ? "low" : ""}" id="timer" role="timer">${icon("clock", "sm")}<span>${h(t("tx.timer", { t: "" }))}<b id="tleft">${fmt.mmss(v.seconds_left)}</b></span></div>`;
  }
  function wireTimer(v) {
    if (v.seconds_left === null || v.seconds_left === undefined) return;
    const end = Date.now() + v.seconds_left * 1000;
    tickTimer = setInterval(() => {
      const left = (end - Date.now()) / 1000, el = document.getElementById("tleft");
      if (el) el.textContent = fmt.mmss(left);
      if (left < 60) { const b = document.getElementById("timer"); if (b) b.classList.add("low"); }
      if (left <= 0) { clearInterval(tickTimer); tickTimer = null; setTimeout(refresh, 800); }
    }, 1000);
  }
  async function vTx(params, token) {
    const v = await call("GET", "/api/transactions/" + params[0]);
    if (token !== renderToken) return;
    const fn = { CHOOSE_METHOD: sChoose, REVIEW_PAY_NOW: sReviewNow, REVIEW_PAY_LATER: sReviewLater, PROCESSING: sProcessing,
      PAYMENT_COMPLETED: sSuccess, PAYMENT_FAILED: sFailed, CANCELLED: sClosed, EXPIRED: sClosed }[v.outcome];
    await fn(v, token);
    wireTimer(v);
  }
  async function doCancel(id) {
    if (!await ask(t("cancel.ask"), t("common.confirm"), t("common.back"))) return;
    try { await call("POST", `/api/transactions/${id}/cancel`); refresh(); } catch (e) { fail(e); }
  }

  // Step 1: the QR payment confirmation screen with Pay Now and Pay Later as payment options.
  function plOption(v) {
    const cr = v.credit, d = v.pay_later_decision;
    if (!v.merchant.pay_later_eligible) return { ok: false, sub: plText(v.merchant.eligibility_code === "STATIC_QR" ? "STATIC_QR" : "MERCHANT_NOT_ELIGIBLE") };
    const r = S.cfg.amount_range, amt = v.transaction.amount_vnd;
    if (amt < r.min) return { ok: false, sub: plText("AMOUNT_BELOW_MIN") };
    if (cr.line_status === "FROZEN" || cr.line_status === "SUSPENDED") return { ok: false, sub: plText(cr.line_status === "FROZEN" ? "ARREARS" : "DEFAULT") };
    if (d && d.reason_code === "R010-SPLIT") return { ok: true, split: true, sub: plText("SPLIT_TENDER", d), decided: true };
    if (d && d.band !== "GREEN") return { ok: false, sub: plText(d.customer_reason, d), decided: true };
    // No line yet: inline activation is offered for any amount from the minimum. Above the product maximum the server
    // finances at most VND 90.1M (split tender, R010-SPLIT) and the rest is paid now - same rule as for an active line.
    if (cr.line_status === "NONE" || cr.line_status === "CLOSED") return { ok: true, sub: amt > r.max ? plText("AMOUNT_ABOVE_MAX_SPLIT") : t("pm.later_noline"), activate: !cr.open_application };
    if (amt > r.max) return { ok: true, sub: plText("AMOUNT_ABOVE_MAX_SPLIT") };
    return { ok: true, sub: t("pm.later_sub", { avail: fmt.vnd(cr.available_vnd) }) };
  }
  function sChoose(v, token) {
    const id = v.transaction.transaction_id, pl = plOption(v), d = v.pay_later_decision;
    const sufficient = v.account.balance_vnd >= v.transaction.amount_vnd;
    let method = ui.method[id] || (pl.ok ? "PAY_LATER" : "ACCOUNT");
    if (method === "PAY_LATER" && !pl.ok) method = "ACCOUNT";
    const alt = d && d.band !== "GREEN" ? (d.alternatives || []) : [];
    const splitCard = pl.split ? `<div class="card"><h3>${h(t("split.t"))}</h3><dl class="kv"><dt>${h(t("split.pl"))}</dt><dd><b>${fmt.vnd(d.split.pay_later_vnd)}</b></dd>
        <dt>${h(t("split.pn"))}</dt><dd>${fmt.vnd(d.split.pay_now_vnd)}</dd></dl><p class="note" style="margin-bottom:0">${h(t("split.note"))}</p></div>` : "";
    paint(`${topbar(t("tx.title"), "", "#/scan")}${purchaseCard(v)}${v.timer_paused ? "" : timerBar(v)}
      ${d && d.band !== "GREEN" && !pl.split ? notice(d.band === "RED" ? "red" : d.band === "N/A" ? "" : "amber", `<b>${h(t(d.band === "RED" ? "pl.declined_title" : d.band === "N/A" ? "pl.na_title" : "pl.result_title"))}</b><br>${h(plText(d.customer_reason, d))}<br><span class="note">${h(t("pl.paynow_available"))}</span>${d.activation_id ? `<br><a href="#/smart-credit/app/${h(d.activation_id)}">${h(t("pl.activation_result"))} ›</a>` : ""}`) : ""}
      ${method === "PAY_LATER" ? splitCard : ""}
      <div class="card"><fieldset><legend>${h(t("tx.choose"))}</legend>
        <label class="opt"><input type="radio" name="pm" value="ACCOUNT" ${method === "ACCOUNT" ? "checked" : ""}><span><span class="on">${h(t("pm.now"))}</span>
          <span class="od">${h(t("pm.now_sub", { acct: v.account.account_masked, bal: fmt.vnd(v.account.balance_vnd) }))}</span>${sufficient ? "" : `<span class="chip red" style="margin-top:6px">${h(t("pm.insufficient"))}</span>`}</span></label>
        <label class="opt ${pl.ok ? "" : "disabled"}"><input type="radio" name="pm" value="PAY_LATER" ${method === "PAY_LATER" ? "checked" : ""} ${pl.ok ? "" : "disabled"} aria-describedby="pl-sub">
          <span><span class="on">${h(t("pm.later"))}</span><span class="od" id="pl-sub">${h(pl.ok ? pl.sub : t("pm.later_na") + " — " + pl.sub)}</span></span></label></fieldset></div>
      <div class="btn-stack"><button class="btn primary" id="next">${h(method === "PAY_LATER" ? (pl.activate ? t("pm.check_limit") : pl.split ? t("split.use") : t("pm.continue_later")) : t("pm.continue_now"))}</button>
        ${alt.includes("LIMIT_INCREASE") ? `<a class="btn ghost" href="#/smart-credit/increase?tx=${h(id)}&need=${v.transaction.amount_vnd}">${h(t("pl.alt.LIMIT_INCREASE"))}</a>` : ""}
        ${alt.includes("PAY_LOAN") || v.credit.line_status === "FROZEN" ? `<a class="btn ghost" href="#/loans">${h(t("pl.alt.PAY_LOAN"))}</a>` : ""}
        <button class="btn link" id="cancel">${h(t("common.cancel"))}</button></div>`, token);
    $screen.querySelectorAll("input[name=pm]").forEach(r => r.onchange = () => { ui.method[id] = r.value; refresh(); });
    document.getElementById("cancel").onclick = () => doCancel(id);
    document.getElementById("next").onclick = async (e) => {
      const btn = e.currentTarget;
      if (method === "ACCOUNT") { busy(btn, true); try { await call("POST", `/api/transactions/${id}/pay-now`); refresh(); } catch (err) { busy(btn, false); fail(err); } return; }
      if (pl.split) { busy(btn, true); try { await call("POST", `/api/transactions/${id}/split`); refresh(); } catch (err) { busy(btn, false); fail(err); refresh(); } return; }
      let consents;
      if (pl.activate) {
        // Consent is never captured under the checkout countdown: the server pauses the timer while the form is open.
        try { await call("POST", `/api/transactions/${id}/consent-pause`, {}); clearInterval(tickTimer); const tb = document.getElementById("timer"); if (tb) tb.remove(); } catch (e) { /* keep going */ }
        const ok = await modal(`<h2 id="modal-title">${h(t("act.title"))}</h2><p style="font-size:14px;margin-top:0">${h(t("act.intro"))}</p><div id="cf">${consentForm()}</div>
          <div class="modal-actions"><button class="btn ghost" data-v="">${h(t("common.cancel"))}</button><button class="btn primary" data-v="ok" id="cf-ok" disabled>${h(t("act.submit"))}</button></div>`,
          { onOpen: (w) => { const b = w.querySelector("#cf-ok"); w.querySelectorAll("[data-consent]").forEach(x => x.onchange = () => { b.disabled = !allConsented(w); consents = readConsents(w); }); w.addEventListener("click", (ev) => { const d2 = ev.target.closest("[data-doc]"); if (d2) { ev.preventDefault(); ev.stopPropagation(); } }, true); } });
        if (ok !== "ok") { try { await call("POST", `/api/transactions/${id}/consent-pause`, { resume: true }); } catch (e) { /* ignore */ } refresh(); return; }
      }
      busy(btn, true);
      try {
        const r = await call("POST", `/api/transactions/${id}/pay-later`, consents ? { activation_consents: consents } : {});
        if (r.activation && r.activation.status !== "APPROVED") toast(t("pl.act_amber"));
        if (r.pay_later_decision && r.pay_later_decision.band !== "GREEN") ui.method[id] = "ACCOUNT";
        refresh();
      } catch (err) { busy(btn, false); fail(err); }
    };
  }

  // Authentication (simulated Smart OTP or face) -> resolves challenge id when verified.
  async function authenticate(txId, purpose) {
    const ch = await call("POST", `/api/transactions/${txId}/auth`, { purpose });
    const face = ch.method === "FACE";
    let verified = null;
    await modal(`<h2 id="modal-title">${h(t(face ? "auth.face_title" : "auth.otp_title"))}</h2>
      ${face ? `<p class="note">${h(t("auth.face_why", { per: fmt.vnd(S.cfg.auth.face_per_tx_vnd), daily: fmt.vnd(S.cfg.auth.face_daily_vnd) }))}</p><div class="faceframe">${icon("face", "lg")}</div>`
        : `<div class="sim-auth"><div class="note">${h(t("auth.sim_otp"))}</div><div class="code" aria-label="Simulated code">${h(ch.simulated_authenticator_code)}</div><div class="note">${h(t("auth.sim_note"))}</div></div>
           <label class="field">${h(t("auth.otp_label"))}<input id="otp" class="otp" inputmode="numeric" maxlength="6" autocomplete="one-time-code" autofocus></label>`}
      <div class="note" id="auth-msg" role="alert"></div><div class="note">${h(t("auth.expires", { t: "" }))}<b id="auth-left">${fmt.mmss(ch.ttl_seconds)}</b></div>
      <div class="modal-actions"><button class="btn ghost" data-v="">${h(t("common.cancel"))}</button><button class="btn primary" id="auth-go">${h(face ? t("auth.face_start") : t("auth.verify"))}</button></div>`,
      { onOpen: (w, close) => {
        const end = Date.now() + ch.ttl_seconds * 1000;
        const tm = setInterval(() => { const el = w.querySelector("#auth-left"); if (!el) return clearInterval(tm); el.textContent = fmt.mmss((end - Date.now()) / 1000); }, 1000);
        const go = w.querySelector("#auth-go");
        go.onclick = async () => {
          go.disabled = true;
          try {
            await call("POST", `/api/auth/${ch.challenge_id}/verify`, face ? { face_result: "MATCH" } : { code: w.querySelector("#otp").value.trim() });
            verified = ch.challenge_id; clearInterval(tm); close("ok");
          } catch (e) {
            go.disabled = false; w.querySelector("#auth-msg").textContent = errText(e);
            if (["AUTH_LOCKED", "CHALLENGE_EXPIRED", "CHALLENGE_CLOSED"].includes(e.code)) { go.disabled = true; }
          }
        };
        const otp = w.querySelector("#otp");
        if (otp) otp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
      } });
    return verified;
  }
  async function payWithAuth(v, purpose, btn) {
    const id = v.transaction.transaction_id;
    let chId;
    try { chId = await authenticate(id, purpose); } catch (e) { return fail(e); }
    if (!chId) return;
    const k = id + ":" + (v.payments || []).length;
    ui.payKey[k] = ui.payKey[k] || uuid();
    busy(btn, true);
    $screen.insertAdjacentHTML("afterbegin", `<div class="card" style="text-align:center"><div class="spinner" role="progressbar" aria-label="${h(t("proc.title"))}"></div><p class="note">${h(t("proc.title"))}</p></div>`);
    $screen.scrollTop = 0;
    try { await call("POST", `/api/transactions/${id}/pay`, { challenge_id: chId }, { "Idempotency-Key": ui.payKey[k] }); }
    catch (e) { if (e.code !== "PAYMENT_IN_PROGRESS") fail(e); }
    refresh();
  }

  function sReviewNow(v, token) {
    const id = v.transaction.transaction_id;
    paint(`${topbar(t("rv.title"), "", "")}${purchaseCard(v)}${timerBar(v)}
      <div class="card"><dl class="kv"><dt>${h(t("rv.method"))}</dt><dd>${h(t("pm.now"))}</dd><dt>${h(t("rv.from"))}</dt><dd>${h(v.account.account_masked)}</dd>
        <dt>${h(t("rv.charge_today"))}</dt><dd>${fmt.vnd(v.transaction.amount_vnd)}</dd></dl></div>
      ${notice("", h(t(v.auth_required && v.auth_required.method === "FACE" ? "auth.face_title" : "auth.otp_title")))}
      <div class="btn-stack"><button class="btn primary" id="go">${icon("lock", "sm")}${h(t("rv.confirm_now", { amt: fmt.vnd(v.transaction.amount_vnd) }))}</button>
        ${v.merchant.pay_later_eligible && v.credit.line_status !== "FROZEN" ? `<button class="btn ghost" id="pl">${h(t("pm.later"))}</button>` : ""}
        <button class="btn link" id="cancel">${h(t("common.cancel"))}</button></div>`, token);
    document.getElementById("go").onclick = (e) => payWithAuth(v, "ACCOUNT_PAYMENT", e.currentTarget);
    document.getElementById("cancel").onclick = () => doCancel(id);
    const pl = document.getElementById("pl");
    if (pl) pl.onclick = async () => { ui.method[id] = "PAY_LATER"; try { await call("POST", `/api/transactions/${id}/pay-later`); refresh(); } catch (e) { fail(e); } };
  }

  function plansHtml(offers, chosen, affordable) {
    return `<div class="plans" role="radiogroup" aria-label="${h(t("rv.choose_tenure"))}">${offers.map(o => {
      const ok = affordable.includes(o.tenure_months);
      return `<label class="plan ${ok ? "" : "na"}"><input type="radio" name="ten" value="${o.tenure_months}" ${o.tenure_months === chosen ? "checked" : ""} ${ok ? "" : "disabled"}>
        <span class="pt">${h(t("common.months", { n: o.tenure_months }))}${ok && o.tenure_months === affordable[0] ? ` <span class="chip blue">${h(t("rv.recommended"))}</span>` : ""}</span>
        <span class="pm">${fmt.vnd(o.monthly_instalment_vnd)}${h(t("common.per_month"))}</span>
        <span class="pd">${ok ? `${h(t("rv.total_interest"))} ${fmt.vnd(o.total_interest_vnd)} · ${h(t("rv.total"))} ${fmt.vnd(o.total_repayable_vnd)} · ${h(t("rv.ear"))} ${fmt.pct(o.effective_annual_rate_pct)}` : h(t("rv.not_affordable"))}</span></label>`;
    }).join("")}</div>`;
  }
  function termsSummary(tm) {
    return `<dl class="kv"><dt>${h(t("rv.monthly"))}</dt><dd>${fmt.vnd(tm.monthly_instalment_vnd)}${tm.last_instalment_vnd !== tm.monthly_instalment_vnd ? ` (${L.lang === "vi" ? "kỳ cuối" : "last"} ${fmt.vnd(tm.last_instalment_vnd)})` : ""}</dd>
      <dt>${h(t("rv.total_interest"))}</dt><dd>${fmt.vnd(tm.total_interest_vnd)}</dd><dt>${h(t("rv.total"))}</dt><dd>${fmt.vnd(tm.total_repayable_vnd)}</dd>
      <dt>${h(t("rv.ear"))}</dt><dd>${fmt.pct(tm.effective_annual_rate_pct)}</dd><dt>${h(t("rv.first_due"))}</dt><dd>${fmt.date(tm.first_due_date)}</dd><dt>${h(t("rv.last_due"))}</dt><dd>${fmt.date(tm.last_due_date)}</dd></dl>
      <p class="note">${h(t("rv.rate_note", { r: tm.annual_interest_rate_pct, eq: fmt.pct(tm.equivalent_reducing_rate_pct).replace("%", ""), ear: fmt.pct(tm.effective_annual_rate_pct).replace("%", "") }))} ${h(t("rv.overdue", { od: tm.overdue_principal_rate_pct, li: tm.late_interest_rate_pct }))}</p>
      <details class="more" style="margin:8px 0 0;box-shadow:none;border:1px solid var(--line)"><summary>${h(t("rv.schedule"))}</summary><div><table class="schedule"><tbody>
        ${tm.instalments.map(i => `<tr><td>${i.number}</td><td>${fmt.date(i.due_date)}</td><td>${fmt.vnd(i.amount_vnd)}</td></tr>`).join("")}</tbody></table></div></details>`;
  }
  function sReviewLater(v, token) {
    const id = v.transaction.transaction_id, d = v.pay_later_decision, tm = v.terms;
    const agreed = !!v.transaction.agreement_accepted_at || !!ui.agree[id];
    paint(`${topbar(t("rv.title"), "", "")}${purchaseCard(v)}${timerBar(v)}
      ${v.transaction.method === "SPLIT" ? `<div class="card"><h3>${h(t("split.t"))}</h3><dl class="kv"><dt>${h(t("split.pl"))}</dt><dd><b>${fmt.vnd(v.transaction.pay_later_amount_vnd)}</b></dd>
        <dt>${h(t("split.pn"))}</dt><dd>${fmt.vnd(v.transaction.pay_now_amount_vnd)}</dd></dl></div>` : ""}
      <div class="card"><h3>${h(t("rv.choose_tenure"))}</h3>${plansHtml(d.offers, tm.tenure_months, d.affordable_tenures)}</div>
      <div class="card">${termsSummary(tm)}
        <label class="check" style="margin-top:6px"><input type="checkbox" id="agree" ${agreed ? "checked" : ""}><span>${t("rv.agree")}</span></label></div>
      ${v.hold ? notice("", h(t("rv.hold", { amt: fmt.vnd(v.hold.amount_vnd) })) + " " + h(t("rv.nothing_charged"))) : ""}
      <div class="btn-stack"><button class="btn primary" id="go" ${agreed ? "" : "disabled"}>${icon("lock", "sm")}${h(t("rv.confirm_later"))}</button>
        <button class="btn ghost" id="now">${h(t("rv.pay_now_instead"))}</button><button class="btn link" id="cancel">${h(t("common.cancel"))}</button></div>`, token);
    $screen.querySelectorAll("input[name=ten]").forEach(r => r.onchange = async () => { ui.agree[id] = false; try { await call("POST", `/api/transactions/${id}/tenure`, { tenure_months: +r.value }); refresh(); } catch (e) { fail(e); } });
    const ag = document.getElementById("agree"), go = document.getElementById("go");
    ag.onchange = () => { ui.agree[id] = ag.checked; go.disabled = !ag.checked; };
    go.onclick = async () => {
      try { if (!v.transaction.agreement_accepted_at) v = await call("POST", `/api/transactions/${id}/agreement`, { accepted: true }); } catch (e) { return fail(e); }
      payWithAuth(v, "PAY_LATER_AGREEMENT", go);
    };
    document.getElementById("now").onclick = async () => { ui.method[id] = "ACCOUNT"; try { await call("POST", `/api/transactions/${id}/pay-now`); refresh(); } catch (e) { fail(e); } };
    document.getElementById("cancel").onclick = () => doCancel(id);
  }
  function sProcessing(v, token) {
    paint(`${topbar(t("proc.title"), "", "")}<div class="card" style="text-align:center;padding:30px"><div class="spinner" role="progressbar" aria-label="${h(t("proc.title"))}"></div><h2 style="margin-top:14px">${h(t("proc.title"))}</h2><p class="note">${h(t("proc.sub"))}</p></div>`, token);
    pollUntilChange(() => call("GET", "/api/transactions/" + v.transaction.transaction_id), x => x.outcome, 1000);
  }
  async function sSuccess(v, token) {
    const tx = v.transaction, pl = tx.method === "PAY_LATER" || tx.method === "SPLIT";
    let conv = null;
    if (!pl) { try { conv = await call("GET", `/api/transactions/${tx.transaction_id}/conversion`); } catch (e) { conv = null; } }
    paint(`${topbar(t("ok.title"), tx.transaction_id, "#/home")}${status("green", "check", t("ok.title"), `${h(t("ok.paid_to"))} <b>${h(v.merchant.name)}</b>`)}
      <div class="card"><div class="amount">${fmt.vnd(tx.amount_vnd)}</div><div class="hr"></div><dl class="kv"><dt>${h(t("rv.method"))}</dt><dd>${h(t("m." + tx.method))}</dd>
        ${pl && v.terms ? `<dt>${h(t("ok.loan"))}</dt><dd>${h(t("ok.pl", { n: v.terms.tenure_months, m: fmt.vnd(v.terms.monthly_instalment_vnd) }))}</dd>` : ""}
        ${tx.method === "SPLIT" ? `<dt>${h(t("split.pl"))}</dt><dd>${fmt.vnd(tx.pay_later_amount_vnd)}</dd><dt>${h(t("split.pn"))}</dt><dd>${fmt.vnd(tx.pay_now_amount_vnd)}</dd>` : ""}
        <dt>Ref</dt><dd class="mono">${h(tx.payment_ref)}</dd></dl>
        ${v.settlement ? `<p class="note" style="margin:10px 0 0">${h(t("ok.settle", { amt: fmt.vnd(v.settlement.gross_vnd), d: fmt.date(v.settlement.settle_on) }))}</p>` : ""}</div>
      ${tx.converted_loan_ref ? notice("green", h(L.lang === "vi" ? "Giao dịch này đã được chuyển thành trả góp; số tiền đã hoàn vào tài khoản." : "This purchase was converted to instalments; the amount was returned to your account.")) : ""}
      ${conv && conv.eligible ? `<div class="card"><h3>${h(t("ok.convert"))}</h3><p class="note" style="margin-top:0">${h(t("ok.convert_sub", { n: conv.window_days, d: fmt.date(conv.until) }))}</p>
        ${plansHtml(conv.offers, ui.conv[tx.transaction_id] || conv.affordable_tenures[0], conv.affordable_tenures)}
        <p class="note">${h(t("conv.sub", { amt: fmt.vnd(tx.amount_vnd) }))}</p>
        <label class="check"><input type="checkbox" id="cagree"><span>${t("rv.agree")}</span></label><button class="btn primary" id="convert" disabled>${icon("lock", "sm")}${h(t("conv.button"))}</button></div>` : ""}
      <div class="btn-stack">${v.loan ? `<a class="btn ghost" href="#/loans/${h(v.loan.loan_ref)}">${h(t("ok.view_loan"))}</a>` : ""}
        ${tx.converted_loan_ref ? `<a class="btn ghost" href="#/loans/${h(tx.converted_loan_ref)}">${h(t("ok.view_loan"))}</a>` : ""}<a class="btn link" href="#/home">${h(t("common.home"))}</a></div>`, token);
    const cb = document.getElementById("convert");
    if (cb) {
      $screen.querySelectorAll("input[name=ten]").forEach(r => r.onchange = () => { ui.conv[tx.transaction_id] = +r.value; });
      document.getElementById("cagree").onchange = (e) => { cb.disabled = !e.target.checked; };
      cb.onclick = async () => {
        const ten = +($screen.querySelector("input[name=ten]:checked") || {}).value;
        let chId; try { chId = await authenticate(tx.transaction_id, "INSTALMENT_CONVERSION"); } catch (e) { return fail(e); }
        if (!chId) return;
        busy(cb, true);
        try { await call("POST", `/api/transactions/${tx.transaction_id}/convert`, { tenure_months: ten, accepted: true, challenge_id: chId }); refresh(); } catch (e) { busy(cb, false); fail(e); }
      };
    }
  }
  function sFailed(v, token) {
    const id = v.transaction.transaction_id, last = (v.payments || []).slice(-1)[0] || {};
    const pl = v.transaction.method === "PAY_LATER" || v.transaction.method === "SPLIT";
    paint(`${topbar(t("fail.title"), "", "")}${status("red", "x", t("fail.title"), h(t("fail.code." + last.failure_code) !== "fail.code." + last.failure_code ? t("fail.code." + last.failure_code) : ""))}
      ${timerBar(v)}${notice("", h(pl ? t("fail.sub") : t("cancel.sub")))}
      <div class="btn-stack"><button class="btn primary" id="retry">${icon("refresh", "sm")}${h(t("fail.retry"))}</button>
        ${pl ? `<button class="btn ghost" id="now">${h(t("rv.pay_now_instead"))}</button>` : ""}<button class="btn link" id="cancel">${h(t("common.cancel"))}</button></div>`, token);
    document.getElementById("retry").onclick = (e) => payWithAuth(v, pl ? "PAY_LATER_AGREEMENT" : "ACCOUNT_PAYMENT", e.currentTarget);
    document.getElementById("cancel").onclick = () => doCancel(id);
    const now = document.getElementById("now");
    if (now) now.onclick = async () => { try { await call("POST", `/api/transactions/${id}/pay-now`); refresh(); } catch (e) { fail(e); } };
  }
  function sClosed(v, token) {
    const exp = v.outcome === "EXPIRED";
    paint(`${topbar(t(exp ? "exp.title" : "cancel.title"), "", "#/home")}${status("grey", exp ? "clock" : "x", t(exp ? "exp.title" : "cancel.title"), h(t(exp ? "exp.sub" : "cancel.sub")))}
      ${purchaseCard(v)}<div class="btn-stack"><a class="btn primary" href="#/scan">${h(t("qa.scan"))}</a><a class="btn link" href="#/home">${h(t("common.home"))}</a></div>`, token);
  }

  // ------------------------------------------------------------ loans
  async function vLoans(params, token) {
    if (params[0]) return vLoan(params[0], token);
    const r = await call("GET", "/api/loans");
    paint(`${topbar(t("loans.title"), "", "#/home")}
      ${r.credit.line_status === "FROZEN" ? notice("red", h(t("sc.frozen_note"))) : ""}
      ${r.loans.length ? `<div class="list">${r.loans.map(l => `<a class="item" href="#/loans/${h(l.loan_ref)}"><span class="avatar" aria-hidden="true">${icon("loan", "sm")}</span>
        <span class="main"><span class="t">${h(l.merchant_name)}</span><span class="s">${h(t("loans.paid", { a: l.paid_count, b: l.instalment_count }))}${l.next_due ? " · " + h(t("loans.next")) + " " + fmt.date(l.next_due.due_date) : ""}</span></span>
        <span class="r">${fmt.vnd(l.outstanding_vnd)}<br><span class="chip ${l.overdue_count ? "red" : l.status === "CLOSED" ? "green" : "blue"}">${h(l.overdue_count ? t("inst.OVERDUE") : t("loan.status." + l.status))}</span></span></a>`).join("")}</div>`
      : `<div class="list"><div class="empty">${h(t("loans.none"))}</div></div>`}`, token);
  }
  async function vLoan(ref, token) {
    const l = await call("GET", "/api/loans/" + ref);
    const unpaid = l.instalments.filter(i => i.status !== "PAID");
    const n = Math.min(+(ui["pay:" + ref] || 1), unpaid.length || 1);
    const amt = unpaid.slice(0, n).reduce((s, i) => s + i.total_due_vnd, 0);
    paint(`${topbar(l.merchant_name, l.loan_ref, "#/loans")}
      <div class="card"><div class="label">${h(t("loans.outstanding"))}</div><div class="amount blue">${fmt.vnd(l.outstanding_vnd)}</div>
        <div class="note">${h(t("loans.paid", { a: l.paid_count, b: l.instalment_count }))} · ${h(t("loan.status." + l.status))}</div></div>
      ${l.overdue_count ? notice("red", `<b>${h(t("inst.OVERDUE"))}</b> — ${h(t("sc.frozen_note"))}`) : ""}
      ${unpaid.length ? `<div class="card"><label class="field">${h(t("loans.pay_select"))}<select id="n">${unpaid.map((_, i) => `<option value="${i + 1}" ${i + 1 === n ? "selected" : ""}>${i + 1}</option>`).join("")}</select></label>
        <p class="note" style="margin:0 0 10px">${h(t("rv.from"))} ${h(l.account.account_masked)} · ${fmt.vnd(l.account.balance_vnd)}</p>
        <button class="btn primary" id="pay">${h(t("loans.pay_n", { n, amt: fmt.vnd(amt) }))}</button></div>
        <div class="card"><dl class="kv"><dt>${h(t("loans.early"))}</dt><dd>${fmt.vnd(l.early_settlement_quote_vnd)}</dd></dl><p class="note" style="margin:8px 0 0">${h(t("loans.early_note"))}</p></div>` : ""}
      <div class="card"><h3>${h(t("loans.schedule"))}</h3><table class="schedule"><thead><tr><th>#</th><th>${L.lang === "vi" ? "Hạn" : "Due"}</th><th>${L.lang === "vi" ? "Trạng thái" : "Status"}</th><th>${h(t("tx.amount"))}</th></tr></thead><tbody>
        ${l.instalments.map(i => `<tr><td>${i.number}</td><td>${fmt.date(i.due_date)}</td><td><span class="chip ${i.status === "PAID" ? "green" : i.status === "OVERDUE" ? "red" : i.display_status === "SCHEDULED" ? "grey" : "amber"}">${h(t("inst." + i.display_status))}</span></td>
        <td>${fmt.vnd(i.total_due_vnd)}${i.overdue_interest_vnd ? `<br><span class="note">+${fmt.vnd(i.overdue_interest_vnd)} ${h(t("loans.overdue_interest"))}</span>` : ""}</td></tr>`).join("")}</tbody></table>
        <p class="note">${h(t("rv.rate_note", { r: l.terms.annual_interest_rate_pct, eq: l.terms.equivalent_reducing_rate_pct, ear: l.terms.effective_annual_rate_pct }))}</p></div>`, token);
    const sel = document.getElementById("n");
    if (sel) sel.onchange = () => { ui["pay:" + ref] = sel.value; refresh(); };
    const pay = document.getElementById("pay");
    if (pay) pay.onclick = async () => {
      busy(pay, true);
      try { await call("POST", `/api/loans/${ref}/repay`, { instalments: unpaid.slice(0, n).map(i => i.number) }, { "Idempotency-Key": uuid() }); ui["pay:" + ref] = 1; refresh(); }
      catch (e) { busy(pay, false); fail(e); }
    };
  }

  // ------------------------------------------------------------ alerts, activity, profile
  async function vAlerts(_, token) {
    const r = await call("GET", "/api/notifications");
    const N = NOTIF[L.lang] || NOTIF.en;
    paint(`${topbar(t("alerts.title"), "", "#/home")}
      ${r.items.length ? `<div class="list">${r.items.map(n => { const [ti, bo] = N[n.title_key] || [n.title_key, ""]; const p = money(n.params || {});
        return `<a class="item" href="${h(n.link || "#/home")}" ${n.read_at ? "" : 'style="background:#f7f9ff"'}><span class="avatar" aria-hidden="true">${icon({ OVERDUE: "alert", REMINDER: "clock", LOAN: "loan", ACTION: "doc" }[n.kind] || "bell", "sm")}</span>
          <span class="main"><span class="t">${h(ti)}</span><span class="s">${h(bo.replace(/\{(\w+)\}/g, (m, k) => p[k] === undefined ? "" : p[k]))}</span><span class="s">${fmt.dt(n.ts)}</span></span></a>`; }).join("")}</div>
        <div class="btn-stack"><button class="btn ghost" id="read">${h(t("alerts.read"))}</button></div>` : `<div class="list"><div class="empty">${h(t("alerts.none"))}</div></div>`}`, token);
    const rd = document.getElementById("read");
    if (rd) rd.onclick = async () => { await call("POST", "/api/notifications/read"); await refreshMe(); refresh(); };
  }
  async function vActivity(_, token) {
    const hist = await call("GET", "/api/transactions");
    paint(`${topbar(t("activity.title"), "", "#/home")}${histList(hist)}`, token);
  }
  async function vProfile(_, token) {
    const me = await refreshMe();
    paint(`${topbar(t("profile.title"), "", "#/home")}<div class="card"><div style="display:flex;gap:12px;align-items:center"><span class="avatar">${h(me.initials)}</span><div><b>${h(me.display_name)}</b><div class="note">${h(me.account_masked)}</div></div></div></div>
      <div class="card"><h3>${h(t("profile.lang"))}</h3><fieldset><legend class="sr-only">${h(t("profile.lang"))}</legend>
        <label class="opt"><input type="radio" name="lang" value="vi" ${L.lang === "vi" ? "checked" : ""}><span class="on">Tiếng Việt</span></label>
        <label class="opt"><input type="radio" name="lang" value="en" ${L.lang === "en" ? "checked" : ""}><span class="on">English</span></label></fieldset></div>
      <div class="card"><h3>${h(t("profile.about"))}</h3><p class="note" style="margin:0">${h(t("profile.about_text"))}</p></div>
      <div class="btn-stack"><button class="btn ghost" id="out">${icon("logout", "sm")}${h(t("profile.signout"))}</button></div>`, token);
    $screen.querySelectorAll("input[name=lang]").forEach(r => r.onchange = () => { L.set(r.value); refresh(); if (window.Presenter) window.Presenter.render(); });
    document.getElementById("out").onclick = signOut;
  }

  boot();
})();
