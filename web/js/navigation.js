// Presenter page (separate from the customer app; nothing here is customer-facing). Four distinct sections:
//   1. Customer Persona Showcase - five fixed, read-only reference profiles (dataset facts + a fresh engine check).
//   2. Live Scenario Simulator   - any inputs, recalculated by the ACTUAL engine in a server-side sandbox
//                                  (POST /api/presenter/simulate). Personas are loaded as COPIES; editing never touches them.
//                                  Optional: run the same customer end-to-end in the customer app (POST /api/presenter/live-demo).
//   3. Risk Failure Simulation   - the three risk scenarios, each ending in a defined safe state.
//   4. Presenter Tools           - live current date/time, simulated business date, open checkouts, reset.
// Process explanations (static text) and engine results (server output) are rendered in visibly different boxes.
// No access code (case assumption); run one local instance per audience.
(function () {
  const $n = document.getElementById("nav");
  const N = {
    token: Store.get("sc.presenterToken", null), cfg: null, show: null, state: null, open: [], sel: null, msg: "", toolMsg: "",
    sim: { form: null, base: null, touched: {}, showErrs: false, result: null, baseResult: null, busy: false, seq: 0, error: "", timer: null },
    live: Store.get("sc.liveCid", null), status: null, poll: null, liveMsg: "",
  };
  const h = (s) => fmt.esc(s);
  const vi = () => L.lang === "vi";
  const tr = (v, e) => (vi() ? v : e);
  const T = (o) => (o ? (o[L.lang] || o.en || "") : "");
  const P = (g, k, fb) => PLAIN.get(g, k, fb);
  let bc = null; try { bc = new BroadcastChannel("smart-credit-demo"); bc.onmessage = (e) => { if (e.data && e.data.type === "refresh") pollNow(); }; } catch (e) { /* older browsers */ }
  const ping = () => { if (bc) bc.postMessage({ type: "refresh" }); };
  const parseInt10 = (s) => { const d = String(s === undefined || s === null ? "" : s).replace(/[^\d]/g, ""); return d ? parseInt(d, 10) : NaN; };
  const MONEY = ["monthly_inflow_vnd", "purchase_amount_vnd", "casa_balance_vnd"], PCT = ["inflow_cv", "dti_ratio", "on_time_bill_ratio"];
  const FLAGS = ["has_formal_cic_history", "has_observable_tx", "rapid_drain_flag", "regular_weekly_pattern"];

  async function session() {
    if (N.token) return N.token;
    const s = await api("POST", "/api/session/presenter", {});
    N.token = s.token; Store.set("sc.presenterToken", s.token); return N.token;
  }
  async function call(m, path, b) {
    await session();
    try { return await api(m, path, b, N.token); }
    catch (e) {
      if (e.code === "SIGN_IN_REQUIRED") { N.token = null; Store.del("sc.presenterToken"); await session(); return api(m, path, b, N.token); }
      throw e;
    }
  }
  function openCustomer(cid, route, qr) {
    const q = new URLSearchParams({ as: cid }); if (qr) q.set("qr", qr);
    window.open("/?" + q.toString() + (route || "#/home"), "hlb-customer");
  }

  // ------------------------------------------------------------------ labels
  const LABEL = () => ({
    display_name: tr("Họ tên (giả định)", "Name (hypothetical)"), customer_profile: tr("Nhóm khách hàng", "Customer segment"),
    monthly_inflow_vnd: tr("Thu nhập bình quân / tháng", "Average monthly inflow"), inflow_cv: tr("Mức biến động thu nhập (%)", "Income variability (%)"),
    has_formal_cic_history: tr("Lịch sử tín dụng CIC", "CIC credit history"), unified_credit_score: tr("Điểm tín dụng (350–850)", "Credit score (350–850)"),
    dti_ratio: tr("Nghĩa vụ nợ hiện có (% thu nhập)", "Existing debt payments (% of income)"), on_time_bill_ratio: tr("Hóa đơn trả đúng hạn (%)", "Bills paid on time (%)"),
    purchase_amount_vnd: tr("Giá trị đơn hàng (VND)", "Purchase amount (VND)"), merchant_id: tr("Cửa hàng đối tác", "Partner merchant"),
    has_observable_tx: tr("Lịch sử giao dịch tại HLB", "HLB transaction history"), rapid_drain_flag: tr("Rút sạch tiền ngay sau khi nạp", "Funds drained right after top-up"),
    regular_weekly_pattern: tr("Chu kỳ giao dịch hằng tuần", "Regular weekly pattern"), e_wallet_topups_pm: tr("Số lần nạp ví điện tử / tháng", "E-wallet top-ups / month"),
    casa_balance_vnd: tr("Số dư tài khoản bình quân 90 ngày", "Average 90-day account balance"),
    requested_loan_vnd: tr("Khoản vay đề nghị", "Requested loan"), loan_tenure_months: tr("Kỳ hạn (tháng)", "Tenure (months)"), application_date: tr("Ngày nộp hồ sơ", "Application date"),
    approved_loan_vnd: tr("Khoản vay được duyệt", "Approved loan"), interest_rate_pct: tr("Lãi suất (%/năm)", "Interest rate (% p.a.)"), is_default_90d: tr("Nợ quá hạn ≥ 90 ngày", "90+ days past due"),
    manual_approval_status: tr("Kết quả thẩm định thủ công", "Manual review decision"), manual_decision_days: tr("Thời gian thủ công (ngày)", "Manual turnaround (days)"),
    manual_review_cost_vnd: tr("Chi phí thủ công", "Manual review cost"), is_abandoned: tr("Bỏ dở hồ sơ", "Abandoned"), stp_approval_status: tr("Kết quả STP (bộ dữ liệu)", "STP decision (dataset)"),
    stp_decision_minutes: tr("Thời gian STP (phút)", "STP turnaround (minutes)"), stp_review_cost_vnd: tr("Chi phí STP", "STP cost"),
  });
  const val = (k, v) => {
    if (v === null || v === undefined || v === "") return "—";
    if (["monthly_inflow_vnd", "purchase_amount_vnd", "casa_balance_vnd", "requested_loan_vnd", "approved_loan_vnd", "manual_review_cost_vnd", "stp_review_cost_vnd"].includes(k)) return fmt.vnd(v);
    if (PCT.includes(k)) return fmt.pct(v * 100);
    if (FLAGS.includes(k) || k === "is_default_90d" || k === "is_abandoned") return +v === 1 ? tr("Có", "Yes") : tr("Không", "No");
    if (k === "manual_approval_status" || k === "stp_approval_status") return +v === 1 ? tr("Duyệt", "Approved") : tr("Từ chối", "Declined");
    if (k === "customer_profile") return P("segment", v, v);
    if (k === "merchant_id") { const m = N.cfg && N.cfg.merchants.find(x => x.merchant_id === v); return m ? m.name : v; }
    if (k === "application_date") return fmt.date(v);
    return typeof v === "number" ? fmt.num(v) : String(v);
  };
  const BAND = { GREEN: ["green", "Tự động duyệt", "Approved automatically"], AMBER: ["amber", "Cần thêm bước", "Needs another step"],
    RED: ["red", "Từ chối có xác minh", "Verified decline"], "N/A": ["grey", "Không áp dụng", "Not applicable"] };
  const band = (b) => { const x = BAND[b] || ["grey", b || "—", b || "—"]; return `<span class="chip ${x[0]}">${h(b || "—")} · ${h(tr(x[1], x[2]))}</span>`; };
  const NEXT = {
    PAY_LATER_FULL: ["Trả sau toàn bộ đơn hàng – khách chọn kỳ hạn và xác thực.", "Whole purchase on Pay Later – the customer picks a plan and authenticates.", "green"],
    SPLIT_TENDER: ["Thanh toán tách phần: trả sau phần trong hạn mức, trả ngay phần còn lại (khách tự chọn).", "Split tender: Pay Later up to the limit, Pay Now for the rest (the customer chooses).", "green"],
    COUNTER_OFFER: ["Đề nghị hạn mức thấp hơn – chờ khách đồng ý.", "Lower limit offered – waiting for the customer to accept.", "green"],
    OFFICER_REVIEW: ["Chuyển chuyên viên tín dụng thẩm định. Tại quầy: trả ngay, chuyển trả góp trong 7 ngày nếu được duyệt.", "Referred to a credit officer. At the counter: pay now, convert to instalments within 7 days if approved.", "amber"],
    FRAUD_REVIEW: ["Chuyển xác minh danh tính / gian lận. Chỉ cho trả ngay.", "Identity / fraud review. Pay Now only.", "amber"],
    SHARE_DATA: ["Hệ thống đề nghị khách chia sẻ thêm dữ liệu, từng nguồn một (mặc định không chọn).", "The system asks the customer to share more data, source by source (unticked by default).", "amber"],
    AUTOMATIC_RETRY: ["Nguồn dữ liệu gián đoạn: tự thử lại, không quyết định trên dữ liệu thiếu. Khách vẫn trả ngay được.", "Data source down: automatic retry, never a decision on missing data. Pay Now still works.", "amber"],
    DECLINED_APPEAL: ["Từ chối trên dữ liệu đã xác minh – khách được khiếu nại một lần; vẫn trả ngay được.", "Declined on verified data – one appeal; Pay Now still works.", "red"],
    POLICY_STOP: ["Dừng theo chính sách.", "Stopped by policy.", "red"],
    PAY_NOW_ONLY_SCOPE: ["Ngoài phạm vi sản phẩm – Trả sau không áp dụng, khách trả ngay.", "Outside product scope – Pay Later does not apply; the customer pays now.", "grey"],
    PAY_NOW_FRAUD_HOLD: ["Tín hiệu gian lận tại quầy: chỉ cho trả ngay, chuyển xác minh.", "Fraud signal at checkout: Pay Now only, sent for review.", "amber"],
    PAY_NOW_SYSTEM_DOWN: ["Hệ thống ngân hàng gián đoạn: Trả sau tạm dừng, khách trả ngay.", "Bank systems down: Pay Later paused, the customer pays now.", "amber"],
    LINE_APPROVED_CONVERT: ["Hạn mức được duyệt sau khi có dữ liệu – đơn hàng có thể chuyển trả góp trong 7 ngày.", "Line approved after the extra data – the purchase can be converted to instalments within 7 days.", "green"],
    PAY_NOW: ["Khách trả ngay (xem lý do).", "The customer pays now (see reason).", "grey"],
    NOT_ASSESSED: ["Chưa thẩm định được.", "Not assessed.", "grey"],
  };
  const nextTxt = (k) => (NEXT[k] ? tr(NEXT[k][0], NEXT[k][1]) : k);
  const NEXT_SHORT = { PAY_LATER_FULL: ["Trả sau toàn bộ", "Full Pay Later"], SPLIT_TENDER: ["Tách phần", "Split tender"], COUNTER_OFFER: ["Hạn mức thấp hơn", "Counter-offer"],
    OFFICER_REVIEW: ["Chuyên viên", "Officer review"], FRAUD_REVIEW: ["Xác minh gian lận", "Fraud review"], SHARE_DATA: ["Chia sẻ dữ liệu", "Share data"],
    AUTOMATIC_RETRY: ["Tự thử lại", "Automatic retry"], DECLINED_APPEAL: ["Từ chối · khiếu nại", "Declined · appeal"], POLICY_STOP: ["Dừng", "Policy stop"],
    PAY_NOW_ONLY_SCOPE: ["Trả ngay (ngoài phạm vi)", "Pay Now (out of scope)"], PAY_NOW_FRAUD_HOLD: ["Trả ngay (gian lận)", "Pay Now (fraud hold)"],
    PAY_NOW_SYSTEM_DOWN: ["Trả ngay (hệ thống)", "Pay Now (systems down)"], LINE_APPROVED_CONVERT: ["Chuyển trả góp", "Convert later"], PAY_NOW: ["Trả ngay", "Pay Now"], NOT_ASSESSED: ["Chưa thẩm định", "Not assessed"] };
  const nextShort = (k) => (NEXT_SHORT[k] ? tr(NEXT_SHORT[k][0], NEXT_SHORT[k][1]) : (k || "—"));
  const CONSTR = (c) => P("constraint", c, c);
  const initials = (n) => { const w = n.split(" ").filter(Boolean); return ((w[0] || "")[0] + (w.length > 1 ? w[w.length - 1][0] : "")).toUpperCase(); };

  // ------------------------------------------------------------------ load
  async function load() {
    try {
      [N.cfg, N.state, N.open, N.risk] = await Promise.all([call("GET", "/api/presenter/scenario-lab"), call("GET", "/api/presenter/state"), call("GET", "/api/presenter/open-checkouts"), call("GET", "/api/presenter/risk")]);
    } catch (e) { N.msg = errText(e); }
    render();
    loadShowcase();
    if (N.cfg && !N.sim.form) loadBaseline({ kind: "persona", id: "P1" });
    if (N.live) { pollNow(); startPoll(); }
  }
  async function loadShowcase() {
    try { N.show = await call("GET", "/api/presenter/personas"); } catch (e) { N.show = { error: errText(e) }; }
    renderPersonas(); renderDetail();
  }
  async function reloadState() {
    try { [N.state, N.open, N.cfg] = await Promise.all([call("GET", "/api/presenter/state"), call("GET", "/api/presenter/open-checkouts"), call("GET", "/api/presenter/scenario-lab")]); } catch (e) { N.toolMsg = errText(e); }
    renderRisk(); renderTools(); renderSimHeader(); renderStatus();
  }
  function activeSwitches() { return N.state ? Object.keys(N.state.controls).filter(k => N.state.controls[k]) : []; }
  function renderStatus() {
    const el = document.getElementById("pg-status"); if (!el) return;
    const on = activeSwitches();
    el.innerHTML = on.length ? `<a href="#sec-tools" class="chip amber" title="${h(on.map(swLabel).join(", "))}">${h(tr(`${on.length} sự cố đang bật`, `${on.length} switch(es) on`))}</a>`
      : `<span class="chip green">${h(tr("Bình thường", "All normal"))}</span>`;
  }

  // ------------------------------------------------------------------ page skeleton
  function header() {
    document.getElementById("nav-title").textContent = tr("Bảng trình diễn", "Presenter page");
    document.title = "Smart Credit · " + tr("Bảng trình diễn", "Presenter page");
    document.getElementById("open-cust").textContent = tr("Ứng dụng khách hàng ↗", "Customer app ↗");
    document.getElementById("open-off").textContent = tr("Bàn chuyên viên tín dụng ↗", "Credit officer workbench ↗");
    document.querySelectorAll("[data-lang]").forEach(b => { b.setAttribute("aria-pressed", String(b.dataset.lang === L.lang)); b.onclick = () => { L.set(b.dataset.lang); render(); }; });
  }
  function render() {
    header();
    if (!N.cfg || !N.state) { $n.innerHTML = `<div class="nav-box"><p>${h(N.msg || tr("Đang tải…", "Loading…"))}</p></div>`; return; }
    const sec = [["sec-personas", tr("Khách hàng mẫu", "Customer personas")], ["sec-sim", tr("Mô phỏng trực tiếp", "Live simulator")],
      ["sec-risk", tr("Vòng rủi ro", "Risk round")], ["sec-tools", tr("Công cụ trình diễn", "Presenter tools")]];
    $n.innerHTML = `
      <nav class="pg-nav" aria-label="${h(tr("Các phần của trang", "Page sections"))}">
        ${sec.map(([id, t], i) => `<a href="#${id}"><b>${i + 1}</b>${h(t)}</a>`).join("")}
        <span class="sp"></span><span id="pg-status" aria-live="polite"></span><span class="pg-clock" id="clock-top" aria-live="off"></span>
      </nav>
      <section class="nav-box pg-sec" id="sec-personas" aria-labelledby="h-personas"></section>
      <section class="nav-box pg-sec sim" id="sec-sim" aria-labelledby="h-sim">
        <div id="sim-head"></div>
        <div class="sim-grid"><div class="sim-left"><div id="sim-form"></div><div id="sim-live"></div></div>
          <div class="sim-right"><div id="sim-result" aria-live="polite"></div><div id="sim-process"></div></div></div>
      </section>
      <section class="nav-box pg-sec" id="sec-risk" aria-labelledby="h-risk"></section>
      <section class="nav-box pg-sec" id="sec-tools" aria-labelledby="h-tools"></section>`;
    renderPersonas(); renderDetail(); renderSimHeader(); renderSimForm(); renderSimResult(); renderProcess(); renderLive(); renderRisk(); renderTools(); renderStatus(); tick();
  }

  // ================================================================== 1. persona showcase
  function personaCard(p) {
    const e = p.engine, a = e.assessment, lim = e.limit.approved_limit_vnd;
    const alias = p.name_source.kind === "FICTIONAL_ALIAS" ? tr("Tên giả định cho demo", "Fictional demo alias")
      : tr(`Tên hư cấu (khách demo ${p.name_source.customer_id})`, `Fictional name (demo customer ${p.name_source.customer_id})`);
    const di = p.dataset.engine_inputs;
    const ok = p.verification.matches;
    const exp = p.expected.band;
    return `<article class="pcard ${N.sel === p.id ? "on" : ""}" aria-labelledby="pn-${h(p.id)}">
      <div class="pc-top"><span class="avatar" aria-hidden="true">${h(initials(p.display_name))}</span>
        <div><h3 id="pn-${h(p.id)}">${h(p.display_name)}</h3><span class="alias">${h(alias)}</span></div><span class="pid">${h(p.id)}</span></div>
      <div class="pc-id"><code>${h(p.applicant_id)}</code></div>
      <div class="pc-seg">${h(P("segment", p.segment, p.segment))}</div>
      <div class="pc-scn"><span class="chip ${(BAND[exp] || ["grey"])[0]}">${h(exp)}</span> ${h(T(p.scenario))}</div>
      <dl class="kpis">
        <div><dt>${h(tr("Thu nhập/tháng", "Inflow/month"))}</dt><dd>${h(fmt.short(di.monthly_inflow_vnd))}${di.has_observable_tx ? "" : "*"}</dd></div>
        <div><dt>${h(tr("Điểm tín dụng", "Credit score"))}</dt><dd>${h(di.unified_credit_score)}</dd></div>
        <div><dt>${h(tr("HĐ đúng hạn", "Bills on time"))}</dt><dd>${h(fmt.pct(di.on_time_bill_ratio * 100))}</dd></div>
        <div><dt>${h(tr("Nợ / thu nhập", "Debt / income"))}</dt><dd>${h(fmt.pct(di.dti_ratio * 100))}</dd></div>
      </dl>
      <div class="pc-flags"><span class="flag ${di.has_formal_cic_history ? "yes" : "no"}">CIC ${di.has_formal_cic_history ? "✓" : "✗"}</span>
        <span class="flag ${di.has_observable_tx ? "yes" : "no"}">${h(tr("Giao dịch HLB", "HLB transactions"))} ${di.has_observable_tx ? "✓" : "✗"}</span></div>
      <div class="pc-eng ${ok ? "ok" : "bad"}"><span class="lbl">${h(tr("Kết quả hệ thống (vừa chạy)", "Engine result (just run)"))}</span>
        <b>${h(a.band || "—")} · ${h(a.reason_code || "")}</b>${lim ? ` · ${h(fmt.short(lim))}` : ""}
        <span class="vchk">${ok ? "✓ " + h(tr("khớp lộ trình", "matches pathway")) : "⚠ " + h(tr("khác lộ trình", "differs"))}</span></div>
      <div class="pc-act"><button type="button" data-detail="${h(p.id)}" aria-expanded="${N.sel === p.id}" aria-controls="pdetail">${h(N.sel === p.id ? tr("Ẩn chi tiết", "Hide details") : tr("Chi tiết", "Details"))}</button>
        <button type="button" class="primary" data-loadp="${h(p.id)}">${h(tr("Nạp bản sao vào mô phỏng", "Load copy into simulator"))}</button></div>
    </article>`;
  }
  function renderPersonas() {
    const el = document.getElementById("sec-personas"); if (!el) return;
    const s = N.show;
    const nOk = s && s.personas ? s.personas.filter(p => p.verification.matches).length : 0;
    el.innerHTML = `<div class="pg-head"><span class="num">1</span><div><h2 id="h-personas">${h(tr("Khách hàng mẫu: Smart Credit phục vụ ai", "Customer personas: who Smart Credit serves"))}</h2>
        <p class="note">${h(tr("Năm hồ sơ tham chiếu cố định, chỉ xem – mỗi hồ sơ là một dòng thật trong bộ dữ liệu 10.000 hồ sơ, đại diện một nhóm khách hàng. Muốn thử nghiệm, hãy nạp bản sao vào mô phỏng; hồ sơ gốc không bao giờ thay đổi.", "Five fixed, read-only reference profiles – each is a real row of the 10,000-record dataset and represents one customer segment. To experiment, load a copy into the simulator; the original never changes."))}</p></div></div>
      ${s && s.personas ? `<p class="verify-line"><span class="chip ${nOk === 5 ? "green" : "amber"}">${nOk}/5 ${h(tr("khớp", "match"))}</span>
        ${h(tr("Mỗi hồ sơ vừa được chạy qua đúng bộ quy tắc hiện hành, trong điều kiện vận hành bình thường", "Each profile was just run through the current engine under normal operating conditions"))} ·
        ${h(tr("ngày nghiệp vụ", "business date"))} ${h(fmt.date(s.business_date))} · ${h(tr("chính sách", "policy"))} <code>${h(s.policy_version)}</code>
        <button type="button" class="linkish" id="reverify">${h(tr("Chạy lại", "Re-run"))}</button></p>
        <div class="pgrid">${s.personas.map(personaCard).join("")}</div>
        <div id="pdetail"></div>
        <p class="note">${h(tr("Tên hiển thị là hư cấu. Chỉ số lấy nguyên văn từ bộ dữ liệu; không bổ sung tuổi, giới tính, nghề nghiệp hay thuộc tính nào khác. * Không có giao dịch quan sát được: thu nhập không phải thu nhập HLB đã xác minh.", "Display names are fictional. Indicators are copied verbatim from the dataset; no age, gender, occupation or other attribute is added. * No observable transactions: the inflow is not verified HLB income."))}</p>`
      : `<p class="note">${h(s && s.error ? s.error : tr("Đang chạy kiểm tra hệ thống cho 5 hồ sơ…", "Running the engine check for the 5 profiles…"))}</p>`}`;
    el.querySelectorAll("[data-detail]").forEach(b => b.onclick = () => { N.sel = N.sel === b.dataset.detail ? null : b.dataset.detail; renderPersonas(); renderDetail(); if (N.sel) document.getElementById("pdetail").scrollIntoView({ behavior: "smooth", block: "nearest" }); });
    el.querySelectorAll("[data-loadp]").forEach(b => b.onclick = () => { loadBaseline({ kind: "persona", id: b.dataset.loadp }); document.getElementById("sec-sim").scrollIntoView({ behavior: "smooth" }); });
    const rv = document.getElementById("reverify"); if (rv) rv.onclick = () => { N.show = null; renderPersonas(); loadShowcase(); };
  }
  function engineSummary(e, compact) {
    const a = e.assessment, l = e.limit, c = e.checkout;
    const reasons = (a.reasons || []).map(r => `<li>${h(r.message)} <span class="code">${h(r.rule_id || "")}</span></li>`).join("");
    return `<div class="eng-sum">
      <div class="row">${band(a.band)} <span class="code">${h(a.reason_code || "")}</span> <span class="note">${h(P("reason", a.reason_code, ""))}</span></div>
      <dl class="figs2">
        <div><dt>${h(tr("Hạn mức duyệt", "Approved limit"))}</dt><dd>${l.approved_limit_vnd ? fmt.vnd(l.approved_limit_vnd) : "—"}</dd></div>
        <div><dt>${h(tr("Giới hạn bởi", "Set by"))}</dt><dd>${l.binding_constraint && l.approved_limit_vnd ? h(CONSTR(l.binding_constraint)) : "—"}</dd></div>
        <div><dt>${h(tr("Tại quầy", "At checkout"))}</dt><dd>${h(c.reason_code || "—")}${c.split ? ` · ${h(fmt.short(c.split.pay_later_vnd))} + ${h(fmt.short(c.split.pay_now_vnd))}` : ""}</dd></div>
        <div><dt>${h(tr("Chuyên viên", "Officer"))}</dt><dd>${e.officer_referral.referred ? h(tr("Có", "Yes")) : h(tr("Không", "No"))}</dd></div>
      </dl>
      <p class="next n-${h((NEXT[e.next_action] || ["", "", "grey"])[2])}"><b>${h(tr("Bước tiếp theo", "Next action"))}:</b> ${h(nextTxt(e.next_action))}</p>
      ${!compact && reasons ? `<ul class="why">${reasons}</ul>` : ""}
      ${e.branches ? `<div class="branches">${["share", "decline"].map(k => { const b = e.branches[k]; return `<div class="br"><span class="lbl">${h(k === "share" ? tr("Nếu khách chia sẻ", "If the customer shares") : tr("Nếu khách từ chối", "If the customer declines"))}</span>${band(b.assessment.band)} <span class="code">${h(b.assessment.reason_code || "")}</span>${b.limit.approved_limit_vnd ? ` · ${h(fmt.vnd(b.limit.approved_limit_vnd))}` : ""}<small>${h(nextTxt(b.next_action))}</small></div>`; }).join("")}</div>` : ""}
    </div>`;
  }
  function renderDetail() {
    const el = document.getElementById("pdetail"); if (!el) return;
    const p = N.sel && N.show && N.show.personas ? N.show.personas.find(x => x.id === N.sel) : null;
    if (!p) { el.innerHTML = ""; el.className = ""; return; }
    const lab = LABEL(), d = p.dataset;
    const rows = (o) => Object.entries(o).map(([k, v]) => `<tr><th scope="row">${h(lab[k] || k)}</th><td>${h(val(k, v))}</td><td><code>${h(k)}</code></td></tr>`).join("");
    el.className = "pdetail";
    el.innerHTML = `<div class="pd-head"><h3>${h(p.id)} · ${h(p.display_name)} <code>${h(p.applicant_id)}</code></h3><button type="button" class="linkish" id="pd-close">${h(tr("Đóng", "Close"))}</button></div>
      <div class="pd-grid">
        <div><h4><span class="src-tag fact">${h(tr("Dữ liệu gốc", "Dataset fact"))}</span> ${h(tr("Đầu vào hệ thống", "Engine inputs"))}</h4>
          <table class="kv"><tbody>${rows(d.engine_inputs)}</tbody></table>
          <h4><span class="src-tag fact">${h(tr("Dữ liệu gốc", "Dataset fact"))}</span> ${h(tr("Hồ sơ vay", "Application"))}</h4>
          <table class="kv"><tbody>${rows(d.application)}</tbody></table>
          <details class="extra"><summary>${h(tr("Cột kết quả lịch sử – không phải đầu vào của hệ thống", "Historical outcome columns – never engine inputs"))}</summary>
            <table class="kv"><tbody>${rows(d.outcomes_not_used)}</tbody></table></details>
          <p class="note">${h(tr("Nguồn", "Source"))}: <code>${h(d.file)}</code></p></div>
        <div><h4>${h(tr("Vì sao chọn hồ sơ này", "Why this profile was selected"))}</h4><p>${h(T(p.why))}</p>
          <h4>${h(tr("Lộ trình quyết định minh họa", "Decision pathway demonstrated"))}</h4><p>${h(T(p.pathway))}</p>
          <p class="share"><b>${h(fmt.pct(p.segment_share.pct))}</b> ${h(T(p.segment_share.label))} <small>(${h(tr("mô phỏng lại 10.000 hồ sơ", "replay of 10,000 rows"))})</small></p>
          ${p.replaces ? `<div class="callout"><b>${h(tr("Thay ứng viên", "Replaces candidate"))} <code>${h(p.replaces.candidate)}</code></b><p>${h(T(p.replaces.why))}</p></div>` : ""}
          ${p.data_notes.map(n => `<div class="callout warn"><b>${h(tr("Lưu ý dữ liệu", "Data note"))}</b><p>${h(T(n))}</p></div>`).join("")}
          <h4><span class="src-tag sim">${h(tr("Mô phỏng", "Simulated"))}</span> ${h(tr("Yếu tố của kịch bản", "Scenario elements"))}</h4>
          <ul class="pl"><li>${h(tr("Cửa hàng", "Merchant"))}: ${h(p.simulated.merchant_name)} – ${h(tr("bộ dữ liệu không có trường cửa hàng", "the dataset has no merchant field"))}</li>
            <li>${h(tr("Giá trị đơn hàng", "Purchase"))}: ${h(fmt.vnd(p.simulated.purchase_amount_vnd))} = <code>requested_loan_vnd</code></li>
            <li>${h(tr("Ngày nghiệp vụ giả lập", "Simulated business date"))}: ${h(fmt.date(p.simulated.business_date))}</li>
            <li>${h(tr("Định danh eKYC, mã QR và mọi kết nối dữ liệu là mô phỏng", "eKYC identity, QR code and every data connection are simulated"))}</li></ul></div>
        <div><h4><span class="src-tag eng">${h(tr("Kết quả hệ thống", "Engine output"))}</span> ${h(tr("Vừa chạy", "Just run"))}</h4>
          ${engineSummary(p.engine)}
          <p class="note">${p.verification.matches ? "✓ " + h(tr("Khớp lộ trình đã mô tả.", "Matches the documented pathway.")) : "⚠ " + h(tr("Khác lộ trình: ", "Differs from the pathway: ")) + h(p.verification.differences.map(x => `${x.field}: ${x.actual}`).join(", "))}</p>
          <button type="button" class="primary" data-loadp2="${h(p.id)}">${h(tr("Nạp bản sao vào mô phỏng", "Load copy into simulator"))}</button></div>
      </div>`;
    document.getElementById("pd-close").onclick = () => { N.sel = null; renderPersonas(); renderDetail(); };
    el.querySelector("[data-loadp2]").onclick = (ev) => { loadBaseline({ kind: "persona", id: ev.currentTarget.dataset.loadp2 }); document.getElementById("sec-sim").scrollIntoView({ behavior: "smooth" }); };
  }

  // ================================================================== 2. live scenario simulator
  const BLANK = () => ({ display_name: "", customer_profile: "Salaried, no credit history", monthly_inflow_vnd: null, inflow_cv: null, has_formal_cic_history: 0,
    unified_credit_score: null, dti_ratio: null, on_time_bill_ratio: null, purchase_amount_vnd: null, merchant_id: "M-ABC",
    has_observable_tx: 1, rapid_drain_flag: 0, regular_weekly_pattern: 1, e_wallet_topups_pm: 6, casa_balance_vnd: null });
  function loadBaseline(b) {
    let inputs, label, sub = "", mods = null;
    if (b.kind === "persona") {
      const p = N.cfg.personas.find(x => x.id === b.id); inputs = p.inputs;
      label = `${p.id} · ${p.display_name}`; sub = `${p.applicant_id} · ${tr("bản sao – hồ sơ gốc không đổi", "copy – the original is unchanged")}`;
    } else if (b.kind === "scenario") {
      const s = N.cfg.failure_scenarios.find(x => x.id === b.id); inputs = s.inputs;
      label = `${s.id} · ${T(s.title)}`; mods = { base: s.base, base_inputs: s.base_inputs, modifications: s.modifications };
      const bp = s.base.persona ? N.cfg.personas.find(x => x.id === s.base.persona) : null;
      sub = bp ? `${tr("từ", "from")} ${bp.id} · ${bp.display_name}` : `${tr("từ dòng dữ liệu", "from dataset row")} ${s.base.dataset_row}`;
    } else { inputs = BLANK(); label = tr("Khách hàng mới (trống)", "New customer (blank)"); sub = tr("Nhập từ đầu", "Start from scratch"); }
    N.sim.base = { kind: b.kind, id: b.id || null, inputs: JSON.parse(JSON.stringify(inputs)), label, sub, mods };
    N.sim.form = JSON.parse(JSON.stringify(inputs)); N.sim.touched = {}; N.sim.showErrs = false; N.sim.result = null; N.sim.baseResult = null; N.sim.error = "";
    renderSimHeader(); renderSimForm(); renderSimResult();
    if (b.kind !== "blank") runSim(true);
  }
  function display(name, v) {
    if (v === null || v === undefined || (typeof v === "number" && isNaN(v))) return "";
    if (MONEY.includes(name)) return fmt.num(v);
    if (PCT.includes(name)) return String(Math.round(v * 1000) / 10);
    return String(v);
  }
  function readField(name, raw) {
    const spec = N.cfg.fields[name];
    if (raw === "" || raw === null || raw === undefined) return spec.kind === "text" ? "" : null;
    if (spec.kind === "text" || spec.kind === "enum") return raw;
    if (spec.kind === "flag") return +raw;
    if (PCT.includes(name)) { const x = parseFloat(String(raw).replace(",", ".")); return isNaN(x) ? NaN : Math.round(x * 10) / 1000; }
    if (MONEY.includes(name)) return parseInt10(raw);
    const n = Number(String(raw).trim()); return Number.isInteger(n) ? n : NaN;
  }
  function validate(f) { // mirrors server/services/live_demo.py (the server re-checks every value)
    const errs = {};
    Object.entries(N.cfg.fields).forEach(([name, spec]) => {
      const v = f[name];
      if (v === null || v === undefined || v === "") { if (spec.required) errs[name] = tr("Bắt buộc.", "Required."); return; }
      if (spec.kind === "text" && v.length > spec.max_len) errs[name] = tr(`Tối đa ${spec.max_len} ký tự.`, `At most ${spec.max_len} characters.`);
      if ((spec.kind === "int" || spec.kind === "ratio") && (typeof v !== "number" || isNaN(v))) { errs[name] = tr("Nhập một số hợp lệ.", "Enter a valid number."); return; }
      if (spec.kind === "int" && !Number.isInteger(v)) errs[name] = tr("Nhập số nguyên.", "Enter a whole number.");
      if ((spec.kind === "int" || spec.kind === "ratio") && (v < spec.min || v > spec.max)) {
        const show = (x) => PCT.includes(name) ? Math.round(x * 100) + "%" : MONEY.includes(name) ? fmt.vnd(x) : fmt.num(x);
        errs[name] = tr(`Trong khoảng ${show(spec.min)} – ${show(spec.max)}.`, `Between ${show(spec.min)} and ${show(spec.max)}.`);
      }
    });
    return errs;
  }
  function modified() {
    if (!N.sim.base) return [];
    return Object.keys(N.cfg.fields).filter(k => {
      const a = N.sim.base.inputs[k], b = N.sim.form[k];
      const na = a === undefined || a === null || a === "", nb = b === undefined || b === null || b === "";
      return !(na && nb) && a !== b;
    });
  }
  function renderSimHeader() {
    const el = document.getElementById("sim-head"); if (!el || !N.cfg) return;
    const b = N.sim.base, lab = LABEL();
    const on = (k, id) => b && b.kind === k && b.id === id;
    const sw = N.cfg.active_switches || [];
    el.innerHTML = `<div class="pg-head"><span class="num">2</span><div><h2 id="h-sim">${h(tr("Mô phỏng kịch bản trực tiếp", "Live scenario simulator"))}</h2>
        <p class="note">${h(tr("Tạo một khách hàng giả định hoặc nạp bản sao của một hồ sơ mẫu, rồi sửa bất kỳ đầu vào nào. Mỗi thay đổi được tính lại bởi đúng bộ quy tắc hiện hành trên máy chủ (môi trường tách biệt, không lưu gì) – không có kết quả dựng sẵn.", "Create a hypothetical customer or load a copy of a persona, then change any input. Every change is recalculated by the actual engine on the server (isolated sandbox, nothing stored) – nothing is scripted."))}</p></div></div>
      <div class="starts" role="group" aria-label="${h(tr("Điểm bắt đầu", "Start from"))}">
        <span class="st-lbl">${h(tr("Bắt đầu từ", "Start from"))}</span>
        <button type="button" class="stb ${on("blank", null) ? "on" : ""}" data-start="blank" aria-pressed="${on("blank", null)}">${h(tr("Trống", "Blank"))}</button>
        <span class="st-grp">${h(tr("Hồ sơ mẫu", "Personas"))}</span>
        ${N.cfg.personas.map(p => `<button type="button" class="stb ${on("persona", p.id) ? "on" : ""}" data-start="persona:${h(p.id)}" aria-pressed="${on("persona", p.id)}" title="${h(p.display_name)} · ${h(T(p.scenario))}"><b>${h(p.id)}</b> ${h(p.display_name.split(" ").slice(-1)[0])}</button>`).join("")}
        <span class="st-grp">${h(tr("Kịch bản lỗi", "Failure scenarios"))}</span>
        ${N.cfg.failure_scenarios.map(s => `<button type="button" class="stb fail ${on("scenario", s.id) ? "on" : ""}" data-start="scenario:${h(s.id)}" aria-pressed="${on("scenario", s.id)}" title="${h(T(s.demonstrates))}"><b>${h(s.id)}</b> ${h(T(s.title))}</button>`).join("")}
      </div>
      ${b ? `<div class="baseline"><div><span class="lbl">${h(tr("Mốc so sánh", "Baseline"))}</span> <b>${h(b.label)}</b> <span class="note">${h(b.sub)}</span>
          ${b.mods ? `<div class="mods">${h(tr("Thay đổi của kịch bản", "Scenario changes"))}: ${Object.entries(b.mods.modifications).map(([k, v]) => `<span class="mod">${h(lab[k] || k)}: ${h(val(k, b.mods.base_inputs[k]))} → <b>${h(val(k, v))}</b></span>`).join(" ")}</div>` : ""}
          ${b.kind === "scenario" ? `<div class="note">${h(T((N.cfg.failure_scenarios.find(x => x.id === b.id) || {}).demonstrates))}</div>` : ""}</div>
        <div id="moddiff"></div></div>` : ""}
      ${sw.length ? `<p class="hint warn" role="status">${h(tr("Đang bật sự cố (mục 3) – áp dụng cho mô phỏng: ", "Failure switches on (section 3) – applied to the simulator: "))}<b>${h(sw.map(k => swLabel(k)).join(", "))}</b></p>` : ""}`;
    el.querySelectorAll("[data-start]").forEach(x => x.onclick = () => { const [k, id] = x.dataset.start.split(":"); loadBaseline({ kind: k, id }); });
    renderModDiff();
  }
  function renderModDiff() {
    const el = document.getElementById("moddiff"); if (!el) return;
    const m = modified(), lab = LABEL();
    el.innerHTML = m.length ? `<div class="mods user"><span class="lbl">${h(tr(`Bạn đã sửa ${m.length} đầu vào`, `You modified ${m.length} input(s)`))}</span>
        ${m.map(k => `<span class="mod">${h(lab[k] || k)}: ${h(val(k, N.sim.base.inputs[k]))} → <b>${h(val(k, N.sim.form[k]))}</b></span>`).join(" ")}
        <button type="button" class="linkish" id="revert">${h(tr("Khôi phục mốc", "Revert to baseline"))}</button></div>`
      : `<span class="note">${h(tr("Chưa sửa đầu vào nào.", "No inputs modified yet."))}</span>`;
    const rv = document.getElementById("revert"); if (rv) rv.onclick = () => { N.sim.form = JSON.parse(JSON.stringify(N.sim.base.inputs)); N.sim.touched = {}; renderSimForm(); renderModDiff(); N.sim.result = N.sim.baseResult; renderSimResult(); };
  }
  function scopeHint(amount) {
    const sc = N.cfg.product_scope;
    if (!Number.isInteger(amount)) return "";
    if (amount < sc.min_vnd) return `<p class="hint warn">${h(tr(`Dưới ${fmt.vnd(sc.min_vnd)}: ngoài phạm vi sản phẩm – Trả sau không áp dụng.`, `Below ${fmt.vnd(sc.min_vnd)}: outside product scope – Pay Later does not apply.`))}</p>`;
    if (amount > sc.max_vnd) return `<p class="hint warn">${h(tr(`Vượt trần ${fmt.vnd(sc.max_vnd)}: phần vượt ${fmt.vnd(amount - sc.max_vnd)} luôn phải trả ngay.`, `Above the ${fmt.vnd(sc.max_vnd)} cap: the excess ${fmt.vnd(amount - sc.max_vnd)} is always paid now.`))}</p>`;
    return `<p class="hint">${h(tr(`Trong phạm vi sản phẩm ${fmt.short(sc.min_vnd)} – ${fmt.short(sc.max_vnd)}.`, `Within product scope ${fmt.short(sc.min_vnd)} – ${fmt.short(sc.max_vnd)}.`))}</p>`;
  }
  function depHints(f) {
    const out = [];
    if (f.has_observable_tx === 0) out.push(tr("Không có giao dịch HLB: thu nhập trở thành bản ghi ngân hàng khác (Open Banking) giả lập và tỷ lệ hóa đơn thành bản ghi tiện ích giả lập – chỉ được dùng nếu khách đồng ý chia sẻ. Số dư và chu kỳ hằng tuần không được dùng.", "No HLB transactions: the inflow becomes a simulated other-bank (Open Banking) record and the bill ratio a simulated utility record – used only if the customer agrees to share. Balance and weekly pattern are not used."));
    if (f.has_observable_tx === 0 && f.has_formal_cic_history === 0) out.push(tr("Hồ sơ mỏng (không CIC, không giao dịch): hệ thống sẽ đề nghị khách chia sẻ dữ liệu; kết quả hiển thị cả hai nhánh.", "Thin file (no CIC, no transactions): the engine will ask the customer to share data; both branches are shown."));
    return out.map(t => `<p class="hint">${h(t)}</p>`).join("");
  }
  function renderSimForm() {
    const el = document.getElementById("sim-form"); if (!el || !N.sim.form) { if (el) el.innerHTML = ""; return; }
    const f = N.sim.form, lab = LABEL(), errs = validate(f);
    const showErr = (n) => errs[n] && (N.sim.showErrs || N.sim.touched[n]);
    const err = (n) => `<span class="err" id="e-${n}">${showErr(n) ? h(errs[n]) : ""}</span>`;
    const isMod = (n) => N.sim.base && modified().includes(n) ? " mod" : "";
    const attrs = (n, dis) => `id="f-${n}" name="${n}" aria-describedby="e-${n} d-${n}" aria-invalid="${!!showErr(n)}" ${dis ? "disabled" : ""}`;
    const thin = f.has_observable_tx === 0;
    const money = (n, hint, dis) => `<label class="fld${isMod(n)}"><span class="lb">${h(lab[n])}</span><input ${attrs(n, dis)} inputmode="numeric" autocomplete="off" value="${h(display(n, f[n]))}">
      <span class="desc" id="d-${n}">${hint !== undefined ? h(hint) : (Number.isInteger(f[n]) && f[n] >= 1e6 ? "≈ " + h(fmt.short(f[n])) : "")}</span>${err(n)}</label>`;
    const num = (n, hint, step, dis) => `<label class="fld${isMod(n)}"><span class="lb">${h(lab[n])}</span><input ${attrs(n, dis)} type="number" inputmode="decimal" step="${step || 1}" value="${h(display(n, f[n]))}">
      <span class="desc" id="d-${n}">${h(hint || "")}</span>${err(n)}</label>`;
    const flag = (n, hint) => `<fieldset class="fld${isMod(n)}"><legend class="lb">${h(lab[n])}</legend><div class="seg2" role="radiogroup">
      <label class="${f[n] === 1 ? "on" : ""}"><input type="radio" name="${n}" value="1" ${f[n] === 1 ? "checked" : ""}>${h(tr("Có", "Yes"))}</label>
      <label class="${f[n] === 0 ? "on" : ""}"><input type="radio" name="${n}" value="0" ${f[n] === 0 ? "checked" : ""}>${h(tr("Không", "No"))}</label></div>
      <span class="desc" id="d-${n}">${h(hint || "")}</span>${err(n)}</fieldset>`;
    const sel = (n, opts) => `<label class="fld${isMod(n)}"><span class="lb">${h(lab[n])}</span><select ${attrs(n)}>${opts.map(([v, t]) => `<option value="${h(v)}" ${f[n] === v ? "selected" : ""}>${h(t)}</option>`).join("")}</select><span class="desc" id="d-${n}"></span>${err(n)}</label>`;
    el.innerHTML = `<form id="sf" novalidate>
      <h3>${h(tr("A · Hồ sơ khách hàng", "A · Customer profile"))}</h3>
      <div class="grid">
        <label class="fld${isMod("display_name")}"><span class="lb">${h(lab.display_name)}</span><input ${attrs("display_name")} maxlength="40" autocomplete="off" value="${h(f.display_name || "")}"><span class="desc" id="d-display_name">${h(tr("Không ảnh hưởng quyết định", "Does not affect the decision"))}</span>${err("display_name")}</label>
        ${sel("customer_profile", N.cfg.segments.map(s => [s, P("segment", s, s)]))}
        ${flag("has_formal_cic_history", tr("Không có CIC không bao giờ là lý do từ chối", "No CIC history is never a decline reason"))}
        ${flag("has_observable_tx", thin ? tr("Không: dùng dữ liệu bổ sung nếu khách đồng ý", "No: extra data only with the customer's consent") : tr("6 tháng giao dịch đã xác minh", "6 months of verified transactions"))}
        ${money("monthly_inflow_vnd", thin ? tr("Thu nhập ở ngân hàng khác (Open Banking giả lập)", "Income at another bank (simulated Open Banking)") : undefined)}
        ${num("inflow_cv", tr("Độ lệch chuẩn ÷ trung bình · 5% ổn định · 70% rất biến động", "Std dev ÷ mean · 5% stable · 70% very volatile"), 0.1)}
        ${num("unified_credit_score", tr("Dưới 436 → chuyển chuyên viên", "Below 436 → officer review"))}
        ${num("dti_ratio", tr("Khoản trả nợ hằng tháng ÷ thu nhập", "Monthly debt payments ÷ income"), 0.1)}
        ${num("on_time_bill_ratio", thin ? tr("Bản ghi tiện ích giả lập · dưới 66,3% → chuyên viên", "Simulated utility record · below 66.3% → officer") : tr("Dưới 66,3% → chuyển chuyên viên", "Below 66.3% → officer review"), 0.1)}
        ${flag("rapid_drain_flag", tr("Có → chuyển chuyên viên", "Yes → officer review"))}
      </div>
      <div id="dep">${depHints(f)}</div>
      <h3>${h(tr("B · Đơn hàng tại quầy (QR)", "B · Purchase at the counter (QR)"))}</h3>
      <div class="grid">${money("purchase_amount_vnd")}${sel("merchant_id", N.cfg.merchants.map(m => [m.merchant_id, m.name + " · " + m.category]))}</div>
      <div id="scope">${scopeHint(f.purchase_amount_vnd)}</div>
      <details class="extra" ${["regular_weekly_pattern", "e_wallet_topups_pm", "casa_balance_vnd"].some(k => modified().includes(k)) ? "open" : ""}><summary>${h(tr("Tín hiệu tham khảo (không dùng để quyết định)", "Reference signals (not decision inputs)"))}</summary>
        <div class="grid">
          ${flag("regular_weekly_pattern", thin ? tr("Không dùng khi không có giao dịch HLB", "Not used without HLB transactions") : tr("Chỉ để tham khảo", "Reference only"))}
          ${num("e_wallet_topups_pm", tr("Chỉ để tham khảo", "Reference only"))}
          ${money("casa_balance_vnd", thin ? tr("Không dùng khi không có giao dịch HLB", "Not used without HLB transactions") : tr("Để trống → 40% thu nhập (mặc định giả lập)", "Empty → 40% of inflow (simulated default)"))}
        </div></details>
      <div class="row actions"><button type="submit" class="primary" id="recalc">${h(tr("Tính lại ngay", "Recalculate now"))}</button>
        <label class="tog inline"><input type="checkbox" id="auto" ${N.sim.auto !== false ? "checked" : ""}><span>${h(tr("Tự tính lại khi sửa", "Recalculate as I type"))}</span></label></div>
      ${N.sim.showErrs && Object.keys(errs).length ? `<p class="formerr" role="alert">${h(tr(`Cần sửa ${Object.keys(errs).length} mục.`, `Fix ${Object.keys(errs).length} field(s).`))}</p>` : ""}
    </form>`;
    wireSimForm();
  }
  function refreshFieldUI(name) {
    const errs = validate(N.sim.form);
    const e = document.getElementById("e-" + name), inp = document.getElementById("f-" + name);
    const msg = errs[name] && (N.sim.showErrs || N.sim.touched[name]) ? errs[name] : "";
    if (e) e.textContent = msg;
    if (inp) { inp.setAttribute("aria-invalid", String(!!msg)); const fl = inp.closest(".fld"); if (fl) fl.classList.toggle("mod", modified().includes(name)); }
    if (MONEY.includes(name) && name !== "casa_balance_vnd" && !(name === "monthly_inflow_vnd" && N.sim.form.has_observable_tx === 0)) {
      const d = document.getElementById("d-" + name); const v = N.sim.form[name];
      if (d) d.textContent = Number.isInteger(v) && v >= 1e6 ? "≈ " + fmt.short(v) : "";
    }
    if (name === "purchase_amount_vnd") document.getElementById("scope").innerHTML = scopeHint(N.sim.form.purchase_amount_vnd);
    const fe = document.querySelector("#sf .formerr");
    if (fe) { const n = Object.keys(errs).length; if (!n) fe.remove(); else fe.textContent = tr(`Cần sửa ${n} mục.`, `Fix ${n} field(s).`); }
    renderModDiff();
  }
  function schedule() {
    clearTimeout(N.sim.timer);
    if (N.sim.auto === false) { markStale(); return; }
    N.sim.timer = setTimeout(() => runSim(false), 450);
  }
  function markStale() { const r = document.querySelector("#sim-result .res"); if (r) r.classList.add("stale"); }
  function wireSimForm() {
    const root = document.getElementById("sf");
    root.querySelectorAll("input:not([type=radio]):not(#auto), select").forEach(el => {
      el.addEventListener("input", () => { N.sim.form[el.name] = readField(el.name, el.value); N.sim.touched[el.name] = true; refreshFieldUI(el.name); schedule(); });
      el.addEventListener("blur", () => { if (MONEY.includes(el.name) && Number.isInteger(N.sim.form[el.name])) el.value = display(el.name, N.sim.form[el.name]); refreshFieldUI(el.name); });
    });
    root.querySelectorAll("input[type=radio]").forEach(r => r.addEventListener("change", () => {
      N.sim.form[r.name] = +r.value; N.sim.touched[r.name] = true; renderSimForm(); renderModDiff();
      const x = document.querySelector(`#sf input[name=${r.name}][value="${r.value}"]`); if (x) x.focus(); schedule();
    }));
    document.getElementById("auto").onchange = (e) => { N.sim.auto = e.target.checked; if (N.sim.auto) schedule(); };
    root.onsubmit = (e) => { e.preventDefault(); N.sim.showErrs = true; runSim(false); };
  }
  function body() {
    const b = {}; Object.keys(N.cfg.fields).forEach(k => { const v = N.sim.form[k]; if (v !== null && v !== undefined && v !== "" && !(typeof v === "number" && isNaN(v))) b[k] = v; });
    return b;
  }
  async function runSim(isBaseline) {
    const errs = validate(N.sim.form);
    if (Object.keys(errs).length) {
      if (N.sim.showErrs) renderSimForm();
      N.sim.error = tr("Kết quả tạm dừng: có đầu vào chưa hợp lệ.", "Result paused: some inputs are not valid yet."); markStale(); renderSimStatus(); return;
    }
    const seq = ++N.sim.seq; N.sim.busy = true; N.sim.error = ""; renderSimStatus();
    try {
      const r = await call("POST", "/api/presenter/simulate", { inputs: body() });
      if (seq !== N.sim.seq) return; // a newer edit superseded this run
      N.sim.result = r; if (isBaseline || !N.sim.baseResult) N.sim.baseResult = r;
    } catch (e) {
      if (seq !== N.sim.seq) return;
      N.sim.error = e.code === "INVALID_LIVE_INPUT" ? tr("Máy chủ từ chối một số giá trị – xem các ô được đánh dấu.", "The server rejected some values – see the marked fields.") : errText(e);
      if (e.code === "INVALID_LIVE_INPUT") { N.sim.showErrs = true; renderSimForm(); }
    }
    N.sim.busy = false; renderSimResult();
  }
  function renderSimStatus() {
    const s = document.getElementById("sim-status"); if (!s) return;
    s.innerHTML = N.sim.busy ? `<span class="spin" aria-hidden="true"></span> ${h(tr("Hệ thống đang tính…", "Engine recalculating…"))}` : N.sim.error ? `<span class="warnt">${h(N.sim.error)}</span>` : "";
  }
  function deltaRows(cur, base) {
    if (!base || cur === base) return "";
    const pick = (r) => ({ band: r.assessment.band, reason: r.assessment.reason_code, limit: r.limit.approved_limit_vnd, checkout: r.checkout.reason_code, next: r.next_action });
    const a = pick(base), b = pick(cur);
    const lbl = { band: tr("Kết quả", "Band"), reason: tr("Mã lý do", "Reason code"), limit: tr("Hạn mức", "Limit"), checkout: tr("Tại quầy", "Checkout"), next: tr("Bước tiếp theo", "Next action") };
    const show = (k, v) => k === "limit" ? (v ? fmt.vnd(v) : "—") : k === "next" ? nextShort(v) : (v || "—");
    const ch = Object.keys(a).filter(k => a[k] !== b[k]);
    return `<div class="delta"><span class="lbl">${h(tr("So với mốc", "Versus baseline"))}</span>${ch.length ? ch.map(k => `<span class="mod">${h(lbl[k])}: ${h(show(k, a[k]))} → <b>${h(show(k, b[k]))}</b></span>`).join(" ") : `<span class="note">${h(tr("Kết quả không đổi.", "Same result."))}</span>`}</div>`;
  }
  function renderSimResult() {
    const el = document.getElementById("sim-result"); if (!el) return;
    const r = N.sim.result;
    if (!r) {
      el.innerHTML = `<div class="res empty"><span class="src-tag eng">${h(tr("Kết quả hệ thống", "Engine output"))}</span><p class="note">${h(N.sim.base && N.sim.base.kind === "blank" ? tr("Nhập các trường bắt buộc; kết quả sẽ hiện ở đây.", "Fill in the required fields; the result appears here.") : tr("Đang tính…", "Calculating…"))}</p><p id="sim-status"></p></div>`;
      renderSimStatus(); return;
    }
    const a = r.assessment, l = r.limit, c = r.checkout, nx = NEXT[r.next_action] || ["", "", "grey"];
    const reasons = (a.reasons || []).map(x => `<li>${h(x.message)} <span class="code">${h(x.rule_id || "")}</span></li>`).join("");
    const opts = (c.payment_options || []).map(o => ({ PAY_NOW: tr("Trả ngay", "Pay Now"), PAY_LATER: tr("Trả sau", "Pay Later"), SPLIT: tr("Tách phần", "Split"), CONVERT_LATER: tr("Chuyển trả góp sau", "Convert later"), LIMIT_INCREASE: tr("Đề nghị tăng hạn mức", "Request a limit increase") }[o] || o)).join(" · ");
    el.innerHTML = `<div class="res">
      <div class="res-head"><span class="src-tag eng">${h(tr("Kết quả hệ thống · chỉ đọc", "Engine output · read-only"))}</span><span id="sim-status" class="note"></span></div>
      <div class="verdict2 v-${h((BAND[a.band] || ["grey"])[0])}"><div>${band(a.band)} <span class="code">${h(a.reason_code || "")}</span></div>
        <b class="vt">${h(P("reason", a.reason_code, a.reason_code || "—"))}</b></div>
      ${deltaRows(r, N.sim.baseResult)}
      <dl class="figs2 big">
        <div><dt>${h(tr("Hạn mức duyệt", "Approved limit"))}</dt><dd>${l.approved_limit_vnd ? fmt.vnd(l.approved_limit_vnd) : "—"}</dd></div>
        <div><dt>${h(tr("Khả năng tự động", "Automatic capacity"))}</dt><dd>${l.automatic_capacity_vnd !== null && l.automatic_capacity_vnd !== undefined ? fmt.vnd(l.automatic_capacity_vnd) : "—"}</dd></div>
        <div><dt>${h(tr("Tối đa chuyên viên duyệt", "Officer maximum"))}</dt><dd>${l.officer_max_limit_vnd ? fmt.vnd(l.officer_max_limit_vnd) : "—"}</dd></div>
        <div><dt>${h(tr("Giới hạn bởi", "Binding constraint"))}</dt><dd>${l.binding_constraint ? h(CONSTR(l.binding_constraint)) : "—"}</dd></div>
        <div><dt>${h(tr("Thu nhập ghi nhận (tháng thấp)", "Recognised income (low month)"))}</dt><dd>${l.recognised_income_vnd ? fmt.vnd(l.recognised_income_vnd) : "—"}</dd></div>
        <div><dt>${h(tr("Nghĩa vụ nợ / tháng", "Obligations / month"))}</dt><dd>${l.obligations_vnd !== null && l.obligations_vnd !== undefined ? fmt.vnd(l.obligations_vnd) : "—"}</dd></div>
      </dl>
      ${reasons ? `<h4>${h(tr("Lý do", "Reasons"))}</h4><ul class="why">${reasons}</ul>` : ""}
      <h4>${h(tr("Tại quầy (kiểm tra giao dịch)", "At checkout (transaction checks)"))}</h4>
      <p class="co"><span class="code">${h(c.reason_code || "—")}</span> ${h(P("reason", c.reason_code, c.message || ""))}${c.split ? ` – ${h(tr("trả sau", "Pay Later"))} <b>${fmt.vnd(c.split.pay_later_vnd)}</b> + ${h(tr("trả ngay", "Pay Now"))} <b>${fmt.vnd(c.split.pay_now_vnd)}</b>` : ""}
        ${opts ? `<br><small>${h(tr("Lựa chọn thanh toán", "Payment options"))}: ${h(opts)}</small>` : ""}</p>
      <div class="two">
        <div><h4>${h(tr("Chuyển chuyên viên", "Officer referral"))}</h4><p>${r.officer_referral.referred ? `<span class="chip amber">${h(tr("Có", "Yes"))}</span> ${h(r.officer_referral.route === "FRAUD_REVIEW" ? tr("xác minh gian lận", "fraud review") : tr("thẩm định tín dụng", "credit review"))}` : `<span class="chip grey">${h(tr("Không", "No"))}</span>`}</p></div>
        <div><h4>${h(tr("Yêu cầu dữ liệu", "Data request"))}</h4><p>${r.data_request.requested ? h(r.data_request.sources.map(s => s.label || s.id).join(", ")) : `<span class="chip grey">${h(tr("Không", "No"))}</span>`}</p></div>
      </div>
      <p class="next n-${h(nx[2])}"><b>${h(tr("Bước tiếp theo", "Next action"))}:</b> ${h(nextTxt(r.next_action))}</p>
      ${r.branches ? `<h4>${h(tr("Hai nhánh tiếp theo (đều do hệ thống tính)", "The two follow-up branches (both computed by the engine)"))}</h4>
        <div class="branches">${["share", "decline"].map(k => { const b = r.branches[k]; return `<div class="br"><span class="lbl">${h(k === "share" ? tr("Khách chia sẻ mọi nguồn được đề nghị", "Customer shares every requested source") : tr("Khách không chia sẻ", "Customer shares nothing"))}</span>
          ${band(b.assessment.band)} <span class="code">${h(b.assessment.reason_code || "")}</span>${b.limit.approved_limit_vnd ? ` · <b>${fmt.vnd(b.limit.approved_limit_vnd)}</b>` : ""}
          ${b.layer2_results.length ? `<small>${h(b.layer2_results.map(x => `${x.source}: ${x.outcome}`).join(" · "))}</small>` : ""}<small>${h(nextTxt(b.next_action))}</small></div>`; }).join("")}</div>
        ${r.derived && r.derived.inflow_role === "SIMULATED_OPEN_BANKING_RECORD" ? "" : `<p class="note">${h(tr("Hồ sơ có giao dịch HLB nên không có bản ghi ngân hàng khác giả lập: nguồn bổ sung trả về “không tìm thấy”.", "This profile has HLB transactions, so no simulated other-bank record exists: extra sources return “not found”."))}</p>`}` : ""}
      ${r.error ? `<p class="hint warn">${h(r.error.code)}: ${h(r.error.message)}</p>` : ""}
      <details class="extra"><summary>${h(tr("Vết kỹ thuật", "Technical trace"))}</summary>
        <ul class="pl small"><li>${h(tr("Tuyến", "Route"))}: <code>${h(a.route || "—")}</code> · ${h(tr("trạng thái", "state"))} <code>${h(a.credit_state || "—")}</code> · ${h(tr("người quyết định", "decided by"))} <code>${h(a.decided_by || "—")}</code></li>
          <li>${h(tr("Quy tắc", "Rules"))}: <code>${h((a.rule_ids || []).join(", ") || "—")}</code>${a.secondary_codes && a.secondary_codes.length ? ` · ${h(tr("mã phụ", "secondary"))} <code>${h(a.secondary_codes.join(", "))}</code>` : ""}</li>
          <li>Layer 0: ${h((a.layer0 || []).map(x => `${x.id} ${x.result}`).join(" · ") || "—")}</li>
          <li>${h(tr("Kiểm tra tại quầy", "Checkout checks"))}: ${h((c.checks || []).map(x => `${x.id} ${x.result}`).join(" · ") || "—")}</li>
          <li>${h(tr("Chính sách", "Policy"))} <code>${h(a.policy_version || "")}</code> · sha256 <code>${h((a.policy_hash || "").slice(0, 12))}…</code> · ${h(tr("hệ thống", "engine"))} ${h(a.engine_ms)} ms · ${h(tr("tổng", "total"))} ${h(r.total_ms)} ms</li>
          <li>${h(tr("Ngày nghiệp vụ", "Business date"))} ${h(fmt.date(r.business_date))} · ${h(tr("tính lúc", "evaluated"))} ${h(fmt.time(r.evaluated_at))} · ${h(tr("môi trường tách biệt, không lưu", "isolated sandbox, nothing stored"))}${r.failure_switches_applied.length ? ` · ${h(tr("sự cố áp dụng", "switches applied"))}: ${h(r.failure_switches_applied.join(", "))}` : ""}</li></ul></details>
    </div>`;
    renderSimStatus();
  }
  function renderProcess() {
    const el = document.getElementById("sim-process"); if (!el) return;
    const steps = [
      [tr("Đầu vào", "Input"), tr("Hồ sơ + đơn hàng QR; đồng ý xử lý dữ liệu bắt buộc", "Profile + QR purchase; the required data-processing consent")],
      [tr("Layer 0", "Layer 0"), tr("Danh tính, thiết bị, trùng lặp, tần suất", "Identity, device, duplicates, velocity")],
      [tr("Layer 1 + kiểm tra dữ liệu", "Layer 1 + data checks"), tr("Dữ liệu HLB/CIC đã xác minh; loại giá trị mâu thuẫn hoặc cũ", "Verified HLB/CIC data; contradictory or stale values excluded")],
      [tr("Khả năng trả nợ", "Capacity"), tr("Hạn mức = min(ngân sách trả nợ, bội số thu nhập ghi nhận, 90,1 triệu)", "Limit = min(debt-service budget, recognised-income multiple, 90.1M)")],
      [tr("Định tuyến", "Routing"), tr("GREEN tự duyệt · AMBER lấy thêm dữ liệu / thử lại / chuyên viên · RED từ chối có xác minh", "GREEN approve · AMBER more data / retry / officer · RED verified decline")],
      [tr("Tại quầy", "Checkout"), tr("Phạm vi, cửa hàng, hệ thống, hạn mức khả dụng, khả năng chi trả; luôn có Trả ngay", "Scope, merchant, systems, available limit, affordability; Pay Now always offered")],
    ];
    el.innerHTML = `<details class="process"><summary><span class="src-tag proc">${h(tr("Giải thích quy trình", "Process explanation"))}</span> ${h(tr("Hệ thống quyết định như thế nào", "How the engine decides"))}</summary>
      <p class="note">${h(tr("Mô tả tĩnh để thuyết trình – không phải kết quả tính toán.", "Static description for the pitch – not a computed result."))}</p>
      <ol class="flow">${steps.map(s => `<li><b>${h(s[0])}</b> – ${h(s[1])}</li>`).join("")}</ol></details>`;
  }

  // ------------------------------------------------------------------ end-to-end run in the customer app (existing live journey)
  const STAGE = {
    WAITING_CUSTOMER: ["Đang chờ khách quét QR và chọn Trả sau trên ứng dụng…", "Waiting for the customer to scan the QR and pick Pay Later…", "wait"],
    ASSESSING: ["Hệ thống đang thẩm định…", "The engine is assessing…", "wait"],
    RETRYING: ["Nguồn dữ liệu gián đoạn: hệ thống tự thử lại.", "A data source is down: the system retries automatically.", "amber"],
    APPROVED: ["Đã duyệt tự động", "Approved automatically", "green"], OFFICER_APPROVED: ["Chuyên viên đã duyệt", "Approved by the credit officer", "green"],
    COUNTER_OFFER: ["Duyệt ở hạn mức thấp hơn – chờ khách nhận", "Lower limit – waiting for the customer", "green"],
    OFFICER_REVIEW: ["Đã chuyển chuyên viên tín dụng", "Referred to a credit officer", "amber"],
    DATA_REQUESTED: ["Hệ thống đề nghị khách chia sẻ thêm dữ liệu", "The system asks the customer to share more data", "amber"],
    DECLINED: ["Từ chối trên dữ liệu đã xác minh", "Declined on verified data", "red"], OFFICER_REJECTED: ["Chuyên viên đã từ chối", "Rejected by the credit officer", "red"],
    STOPPED: ["Dừng theo chính sách", "Stopped by policy", "red"], CANCELLED: ["Khách đã hủy", "Cancelled by the customer", "grey"],
    PAID: ["Đã thanh toán – khoản trả góp đã tạo", "Paid – instalment loan booked", "green"],
  };
  function renderLive() {
    const el = document.getElementById("sim-live"); if (!el) return;
    const s = N.status;
    let inner = "";
    if (N.live && s) {
      const a = s.application, c = s.checkout, stg = STAGE[s.stage] || [s.stage, s.stage, "grey"];
      const limit = a && (a.approved_limit_vnd || s.line.limit_vnd);
      inner = `<div class="journey"><p class="who"><b>${h(s.display_name)}</b> <code>${h(s.customer_id)}</code></p>
        <p class="verdict v-${h(stg[2])}"><b>${h(stg[vi() ? 0 : 1])}</b>${a && a.band ? ` <span class="code">${h(a.band)} · ${h(a.reason_code || "")}</span>` : ""}
          ${limit ? `<span class="why">${h(tr("Hạn mức", "Limit"))}: <b>${fmt.vnd(limit)}</b></span>` : ""}
          ${c && c.split ? `<span class="why">${h(tr("Trả sau", "Pay Later"))} ${fmt.vnd(c.split.pay_later_vnd)} + ${h(tr("trả ngay", "Pay Now"))} ${fmt.vnd(c.split.pay_now_vnd)}</span>` : ""}</p>
        <div class="row"><button type="button" id="res-app">${h(tr("Mở ứng dụng khách hàng ↗", "Open customer app ↗"))}</button>
          ${a && ["OFFICER_REVIEW", "OFFICER_APPROVED", "OFFICER_REJECTED"].includes(s.stage) ? `<a class="btnlike" href="/officer.html?app=${encodeURIComponent(a.application_id)}" target="hlb-officer">${h(tr("Mở hồ sơ ở bàn chuyên viên ↗", "Open case in officer workbench ↗"))}</a>` : ""}
          <button type="button" class="linkish" id="res-clear">${h(tr("Bỏ theo dõi", "Stop following"))}</button></div>
        <p class="note">${h(tr("Trạng thái từ máy chủ, tự cập nhật mỗi 3 giây", "Server status, updates every 3 seconds"))} · ${fmt.time(s.checked_at)}</p></div>`;
    }
    el.innerHTML = `<div class="e2e"><h3>${h(tr("C · Chạy trọn hành trình trong ứng dụng khách hàng", "C · Run the full journey in the customer app"))}</h3>
      <p class="note">${h(tr("Tạo khách hàng hư cấu với đúng các đầu vào trên và một đơn hàng QR, rồi mở ứng dụng khách hàng ở cửa sổ riêng. Khách quét QR, chọn Trả sau và đồng ý – quyết định hiện ở cả hai nơi và trong bàn chuyên viên.", "Creates a fictional customer with exactly the inputs above plus a QR order, and opens the customer app in its own window. The customer scans, picks Pay Later and consents – the decision shows in both places and in the officer workbench."))}</p>
      <div class="row"><button type="button" id="run-e2e">${h(tr("Tạo khách hàng & mở ứng dụng ↗", "Create customer & open app ↗"))}</button>${N.liveMsg ? `<span class="warnt">${h(N.liveMsg)}</span>` : ""}</div>${inner}</div>`;
    document.getElementById("run-e2e").onclick = runE2E;
    const app = document.getElementById("res-app");
    if (app) app.onclick = () => { const q = N.status && N.status.checkout ? null : "QR-LIVE-" + N.live.slice(5); openCustomer(N.live, q ? "#/scan" : "#/tx/" + N.status.checkout.transaction_id, q); };
    const cl = document.getElementById("res-clear");
    if (cl) cl.onclick = () => { N.live = null; N.status = null; Store.del("sc.liveCid"); stopPoll(); renderLive(); };
  }
  async function runE2E() {
    N.sim.showErrs = true;
    if (Object.keys(validate(N.sim.form)).length) { renderSimForm(); N.liveMsg = tr("Sửa các ô được đánh dấu trước.", "Fix the marked fields first."); renderLive(); return; }
    const btn = document.getElementById("run-e2e"); btn.disabled = true;
    try {
      const r = await call("POST", "/api/presenter/live-demo", body());
      N.live = r.customer_id; Store.set("sc.liveCid", r.customer_id); N.status = r.status; N.liveMsg = "";
      openCustomer(r.customer_id, "#/scan", r.qr_ref); startPoll();
    } catch (e) { N.liveMsg = errText(e); }
    renderLive();
  }
  async function pollNow() {
    if (!N.live) return;
    try { N.status = await call("GET", "/api/presenter/live-demo/" + encodeURIComponent(N.live)); }
    catch (e) { if (e.status === 404) { N.live = null; N.status = null; Store.del("sc.liveCid"); stopPoll(); } }
    renderLive();
  }
  function startPoll() { stopPoll(); N.poll = setInterval(() => { if (!document.hidden) pollNow(); }, 3000); }
  function stopPoll() { if (N.poll) clearInterval(N.poll); N.poll = null; }

  // ================================================================== 3. risk round: three failure simulations
  const SW = {
    cic_unavailable: ["CIC không phản hồi", "CIC enquiry times out"], alt_data_api_unavailable: ["API dữ liệu bổ sung quá thời gian", "Extra-data APIs time out"],
    alt_data_incomplete: ["Dữ liệu bổ sung chỉ có 3 tháng", "Extra data covers only 3 months"], core_system_unavailable: ["Hệ thống ngân hàng quá tải / gián đoạn", "Bank systems overloaded / down"],
    device_anomaly: ["Bất thường thiết bị / phiên", "Device / session anomaly"], force_next_payment_failure: ["Lần thanh toán tới thất bại", "Next payment fails"],
    force_next_auth_failure: ["Lần xác thực tới thất bại", "Next authentication fails"],
  };
  const swLabel = (k) => SW[k] ? tr(SW[k][0], SW[k][1]) : k;
  const SW_NOTE = { cic_unavailable: ["→ tự thử lại (R010-API), không quyết định trên dữ liệu thiếu", "→ automatic retry (R010-API), no decision on missing data"],
    alt_data_api_unavailable: ["→ khi khách chia sẻ: tự thử lại, không chuyển chuyên viên", "→ when the customer shares: automatic retry, no officer"],
    alt_data_incomplete: ["→ bị loại khi kiểm tra AV-02 (cần ≥ 6 tháng)", "→ rejected by check AV-02 (6 months needed)"],
    core_system_unavailable: ["→ màn hình khách: Trả sau tạm dừng, Trả ngay (TX-03)", "→ customer screen: Pay Later paused, Pay Now (TX-03)"],
    device_anomaly: ["→ chỉ trả ngay, xác minh gian lận (R010-FRD)", "→ Pay Now only, fraud review (R010-FRD)"],
    force_next_payment_failure: ["→ không trừ tiền, giữ hạn mức, thử lại", "→ nothing charged, hold kept, retry"], force_next_auth_failure: ["→ từ chối mã, cho nhập lại", "→ code rejected, customer retries"] };
  const togRow = (k) => { const on = N.state.controls[k]; return `<label class="tog"><input type="checkbox" data-ctl="${k}" ${on ? "checked" : ""}><span><b>${h(swLabel(k))}</b><small>${h(tr(SW_NOTE[k][0], SW_NOTE[k][1]))}</small></span></label>`; };
  const RISK_TABS = [["R1", ["Quá tải hệ thống", "System overload"]], ["R2", ["Lỗi quy tắc & bằng chứng", "Rule & evidence errors"], ["tương đương rủi ro AI", "AI-risk equivalent"]], ["R3", ["Dữ liệu thiếu & bất thường", "Missing & anomalous data"]]];
  const RK = { tab: Store.get("sc.riskTab", "R1"), ovl: null, ovlBusy: false, ovlRes: null, rule: null, ruleRes: null, ruleBusy: false, data: null, dataRes: null, dataBusy: false, err: "" };
  const KIND = { MISSING: ["Thiếu", "Missing"], STALE: ["Cũ", "Stale"], INCONSISTENT: ["Mâu thuẫn", "Inconsistent"], INVALID: ["Bất khả", "Invalid"], INCOMPLETE: ["Không đủ kỳ", "Incomplete"], MISSING_FIELD: ["Thiếu trường", "Missing field"], CONFIG: ["Cấu hình", "Configuration"], EVIDENCE: ["Bằng chứng", "Evidence"] };
  const RULE_TXT = { "R-VER-01": ["Giá trị mâu thuẫn với cờ nguồn – loại khỏi tính toán", "Value contradicts its source flag – excluded from use"], "R-VER-02": ["Nguồn chưa được xác minh – loại khỏi tính toán", "Source not verified – excluded from use"],
    "R-VER-03": ["Nguồn quá cũ (> 90 ngày) – loại khỏi tính toán", "Source too old (> 90 days) – excluded from use"], "R-INV-01": ["Giá trị ngoài khoảng cho phép – lỗi toàn vẹn dữ liệu", "Value outside its allowed range – data-integrity defect"] };
  function renderRisk() {
    const el = document.getElementById("sec-risk"); if (!el || !N.state) return;
    const R = N.risk; const anyOn = Object.keys(SW).filter(k => ["cic_unavailable", "alt_data_api_unavailable", "alt_data_incomplete", "core_system_unavailable"].includes(k)).some(k => N.state.controls[k]);
    el.innerHTML = `<div class="pg-head"><span class="num">3</span><div><h2 id="h-risk">${h(tr("Vòng rủi ro: ba mô phỏng sự cố", "Risk round: three failure simulations"))}</h2>
        <p class="note">${h(tr("Mỗi mô phỏng chạy trên đúng bộ quy tắc hiện hành (môi trường tách biệt) và kết thúc ở một trạng thái an toàn được định nghĩa. Không thao tác nào ở đây thay đổi quy tắc tín dụng.", "Each simulation runs on the actual engine (isolated sandbox) and ends in a defined safe state. Nothing here changes a credit rule."))}</p></div>
        ${anyOn ? `<span class="chip amber">${h(tr("Sự cố đang bật trên ứng dụng", "Live switch on"))}</span>` : ""}</div>
      <div class="rtabs" role="tablist" aria-label="${h(tr("Ba rủi ro", "Three risks"))}">${RISK_TABS.map(([id, t, sub]) => `<button role="tab" id="rt-${id}" aria-selected="${RK.tab === id}" aria-controls="rp" data-rtab="${id}"><span class="rid">${id}</span><span><b>${h(tr(t[0], t[1]))}</b>${sub ? `<small>${h(tr(sub[0], sub[1]))}</small>` : ""}</span></button>`).join("")}</div>
      <div id="rp" role="tabpanel" aria-labelledby="rt-${RK.tab}">${!R ? `<p class="note">${h(tr("Đang tải…", "Loading…"))}</p>` : RK.tab === "R1" ? r1() : RK.tab === "R2" ? r2() : r3()}</div>
      ${RK.err ? `<p class="nav-status" role="status">${h(RK.err)}</p>` : ""}`;
    wireRisk();
  }
  // ---------- R1 system overload
  function r1() {
    const R = N.risk.overload, p = RK.ovl || R.presets[1], res = RK.ovlRes;
    const B = R.bounds;
    const inp = (k, lab, unit) => `<label class="fld"><span class="lb">${h(lab)}</span><input type="number" id="ov-${k}" min="${B[k][0]}" max="${B[k][1]}" value="${p[k]}"><span class="desc">${h(unit || "")}</span></label>`;
    return `<div class="rgrid2"><div class="rleft">
        <h3>${h(tr("Kịch bản tải", "Load scenario"))}</h3>
        <div class="starts">${R.presets.map(x => `<button type="button" class="stb ${RK.ovl && RK.ovl.id === x.id ? "on" : ""}" data-ovp="${h(x.id)}">${h(T(x.title))} <small>${fmt.num(x.requests)}/${x.window_s}s</small></button>`).join("")}</div>
        <div class="grid">${inp("requests", tr("Số yêu cầu Trả sau", "Pay Later requests"), tr("trong cửa sổ bên dưới", "in the window below"))}${inp("window_s", tr("Cửa sổ (giây)", "Window (seconds)"))}
          ${inp("workers", tr("Luồng xử lý song song", "Parallel decision workers"))}${inp("queue_max", tr("Hàng đợi tối đa", "Queue limit"), tr("vượt → trả lời ngay bằng phương án dự phòng", "beyond → answered at once with the fallback"))}
          ${inp("budget_ms", tr("Ngân sách thời gian (ms)", "Decision time budget (ms)"), tr("tại quầy khách không chờ lâu hơn", "the shopper never waits longer at the counter"))}</div>
        <div class="row"><button type="button" class="primary" id="ov-run" ${RK.ovlBusy ? "disabled" : ""}>${h(RK.ovlBusy ? tr("Đang chạy…", "Running…") : tr("Chạy mô phỏng tải", "Run load simulation"))}</button></div>
        <p class="note">${h(tr("Giả định mô phỏng: độ trễ hệ thống phía sau (lõi, CIC, eKYC) trung vị 350 ms. Thời gian tính của bộ quy tắc được đo thật. Quy mô luồng, hàng đợi và ngân sách là thiết lập cổng cần HLBVN IT định cỡ – không phải số liệu HLBVN.", "Simulated assumptions: downstream latency (core, CIC, eKYC) median 350 ms. Engine compute time is measured. Pool, queue and budget are gateway settings for HLBVN IT to size – not HLBVN data."))}</p>
        <h3>${h(tr("Cho khán giả thấy trên ứng dụng thật", "Show it in the live apps"))}</h3>
        ${["core_system_unavailable", "cic_unavailable", "alt_data_api_unavailable"].map(togRow).join("")}
      </div><div class="rright">${res ? r1res(res) : `<div class="rempty">${h(tr("Chọn kịch bản và bấm Chạy.", "Pick a scenario and press Run."))}</div>`}</div></div>`;
  }
  function chart(tl) {
    const W = 520, H = 120, n = tl.length, bw = W / n, mx = Math.max(1, ...tl.map(b => b.decided + b.fallback)), mq = Math.max(1, ...tl.map(b => b.in_system));
    const bars = tl.map((b, i) => { const hd = b.decided / mx * (H - 14), hf = b.fallback / mx * (H - 14); return `<rect x="${i * bw + 1}" y="${H - hd}" width="${bw - 2}" height="${hd}" class="c-dec"/><rect x="${i * bw + 1}" y="${H - hd - hf}" width="${bw - 2}" height="${hf}" class="c-fb"/>`; }).join("");
    const line = tl.map((b, i) => `${(i + .5) * bw},${H - b.in_system / mq * (H - 14)}`).join(" ");
    return `<svg class="tchart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${h(tr("Biểu đồ theo thời gian", "Timeline chart"))}">${bars}<polyline points="${line}" class="c-q"/></svg>
      <div class="legend"><span><i class="c-dec"></i>${h(tr("Đã quyết định trong ngân sách", "Decided within budget"))}</span><span><i class="c-fb"></i>${h(tr("Trả lời bằng phương án dự phòng", "Answered with fallback"))}</span><span><i class="c-q"></i>${h(tr("Yêu cầu đang chờ (tối đa ", "Requests in system (peak "))}${fmt.num(mq)})</span><span class="note">${h(tr("trục thời gian", "time axis"))}: 0 → ${h(tl[tl.length - 1].t)}s</span></div>`;
  }
  function r1res(r) {
    const P = r.protected, U = r.unprotected, over = r.params.requests / r.params.window_s > P.capacity_rps;
    const tile = (k, v, s, cls) => `<div class="mt ${cls || ""}"><span class="k">${h(k)}</span><span class="v">${v}</span>${s ? `<span class="s">${h(s)}</span>` : ""}</div>`;
    return `<div class="rres">
      <div class="rstate ${over ? "warn" : "ok"}"><b>${h(over ? tr("Quá tải: tải vượt năng lực – cổng kiểm soát bảo vệ hệ thống", "Overloaded: demand exceeds capacity – the gateway controls protect the system") : tr("Trong năng lực: mọi yêu cầu được quyết định đúng hạn", "Within capacity: every request decided on time"))}</b>
        <span>${h(tr("Tải", "Load"))} ${h(P.offered_rps)} ${h(tr("yêu cầu/giây so với năng lực", "req/s vs capacity"))} ≈ ${h(P.capacity_rps)}</span></div>
      <div class="mtiles">${tile(tr("Quyết định đúng hạn", "Decided on time"), fmt.num(P.decided), fmt.pct(100 * P.decided / P.requests))}
        ${tile(tr("Trả lời dự phòng", "Fallback answers"), fmt.num(P.fallback), `${fmt.num(P.timed_out)} ${tr("quá hạn", "timed out")} · ${fmt.num(P.shed)} ${tr("vượt hàng đợi", "over queue")}`, P.fallback ? "amber" : "")}
        ${tile(tr("Thời gian quyết định p95", "Decision time p95"), (P.latency_ms.p95 / 1000).toFixed(2) + " s", tr("tối đa", "max") + " " + (P.latency_ms.max / 1000).toFixed(2) + " s")}
        ${tile(tr("Chờ tối đa của khách", "Longest wait for a shopper"), (Math.max(P.latency_ms.max, r.params.budget_ms * (P.fallback ? 1 : 0)) / 1000).toFixed(1) + " s", tr("không bao giờ vượt ngân sách", "never above the budget"))}</div>
      ${chart(P.timeline)}
      <div class="cmp"><div class="cmp-a"><span class="lbl">${h(tr("Có cơ chế bảo vệ", "With safeguards"))}</span><b>p95 ${(P.all_latency_p95_ms / 1000).toFixed(1)} s</b><small>${h(tr("Không ai chờ quá", "No one waits more than"))} ${(r.params.budget_ms / 1000).toFixed(1)} s · ${h(tr("xong sau", "done after"))} ${P.drained_after_s} s</small></div>
        <div class="cmp-b"><span class="lbl">${h(tr("Nếu không có cơ chế bảo vệ (cùng tải)", "Without safeguards (same load)"))}</span><b>p95 ${(U.latency_ms.p95 / 1000).toFixed(1)} s</b><small>${fmt.num(U.waited_over_counter_s)} ${h(tr("khách chờ > 30 s tại quầy", "shoppers wait > 30 s at the counter"))} · ${h(tr("tồn đọng đến", "backlog until"))} ${U.drained_after_s} s</small></div></div>
      <div class="safe"><b>${h(tr("Trạng thái an toàn khi quá hạn hoặc vượt hàng đợi", "Safe state on timeout or full queue"))}:</b> ${h(tr("không ghi quyết định tín dụng nào, không tạm giữ hạn mức, 0 phê duyệt trong lúc dự phòng; khách được Trả ngay, thử lại, hoặc chuyển trả góp trong 7 ngày khi có hạn mức – cùng hành vi quy tắc TX-03 / R010-API đã có.", "no credit decision written, no hold placed, 0 approvals during fallback; the shopper can Pay Now, retry, or convert within 7 days once a line exists – the same behaviour the rules already define (TX-03 / R010-API)."))}</div>
      <details class="extra"><summary>${h(tr("Bộ quy tắc thật trong mô phỏng", "The real engine inside the simulation"))}</summary>
        <p class="note">${h(tr("Thời gian tính đo được", "Measured engine compute"))}: <b>${h(r.engine.measured_ms_per_decision)} ms</b> ${h(tr("mỗi quyết định – nút thắt là các lệnh gọi phía sau, không phải quy tắc.", "per decision – the bottleneck is downstream calls, not the rules."))}</p>
        <ul class="pl small">${Object.entries(r.decided_outcomes).map(([k, v]) => `<li>${h(nextShort(k))}: ${fmt.num(v)}</li>`).join("")}</ul>
        <p class="note">${h(tr("Mỗi yêu cầu đúng hạn nhận kết quả thật của một trong 5 hồ sơ mẫu (luân phiên).", "Each on-time request gets the real result of one of the 5 personas (round robin)."))}</p></details></div>`;
  }
  // ---------- R2 rule & evidence errors
  function r2() {
    const R = N.risk, res = RK.ruleRes;
    return `<div class="why-eq"><b>${h(tr("Vì sao đây là rủi ro tương đương “AI không chắc chắn / sai”", "Why this is our equivalent of “AI uncertainty / incorrect outputs”"))}</b>
        <p>${h(tr("Smart Credit dùng bộ quy tắc xác định, không dùng mô hình AI để quyết định tín dụng – nên không có “độ không chắc chắn” thống kê. Rủi ro tương đương là một quyết định sai nhưng tự tin: (1) quy tắc bị cấu hình sai, (2) quy tắc nhận bằng chứng không đáng tin, hoặc (3) nhiều quy tắc cùng kích hoạt. Dưới đây mỗi trường hợp được thử trên bản sao chính sách hoặc hồ sơ trong môi trường tách biệt.", "Smart Credit decides with a deterministic rule set, not an AI credit model – so there is no statistical “uncertainty”. The equivalent risk is a confidently wrong decision: (1) the rules are misconfigured, (2) the rules receive unreliable evidence, or (3) several rules fire at once. Each case below is tried on a copy of the policy or of a profile, in an isolated sandbox."))}</p>
        <p class="note">${h(tr("Chính sách đang chạy", "Live policy"))}: <code>${h(R.live_policy.version)}</code> · sha256 <code>${h(R.live_policy.hash.slice(0, 12))}…</code> ${R.live_policy.matches_lock ? `<span class="chip green">${h(tr("khớp bản khóa", "matches lock"))}</span>` : `<span class="chip red">${h(tr("không khớp bản khóa", "does not match lock"))}</span>`} · ${h(tr("Trang này không thể sửa quy tắc.", "This page cannot change the rules."))}</p></div>
      <div class="cases">${R.rule_cases.map(c => `<button type="button" class="case ${RK.rule === c.id ? "on" : ""}" data-rcase="${h(c.id)}"><span class="cid">${h(c.id)}</span><span class="chip ${c.kind === "CONFIG" ? "blue" : "amber"}">${h(tr(KIND[c.kind][0], KIND[c.kind][1]))}</span><b>${h(T(c.title))}</b><small>${h(T(c.what))}</small></button>`).join("")}</div>
      <div class="rres">${RK.ruleBusy ? `<p class="note"><span class="spin"></span> ${h(tr("Đang chạy trên bộ quy tắc thật…", "Running on the actual engine…"))}</p>` : res ? (res.controls ? r2config(res) : r2evidence(res)) : `<div class="rempty">${h(tr("Chọn một trường hợp.", "Pick a case."))}</div>`}</div>
      <p class="note">${h(tr(`Các lớp bảo vệ khác đang chạy: mẫu QA ${R.qa_sample_pct.green}% quyết định tự duyệt (${R.qa_sample_pct.layer2}% nếu dùng dữ liệu bổ sung) do chuyên viên kiểm tra; chuyên viên quyết định với mã lý do; khách được khiếu nại một lần với RED; mọi quyết định ghi phiên bản và mã băm chính sách.`, `Other safeguards always on: ${R.qa_sample_pct.green}% QA sample of automatic approvals (${R.qa_sample_pct.layer2}% with extra data) checked by officers; officer decisions need reason codes; one appeal for any RED; every decision is stamped with the policy version and hash.`))}</p>`;
  }
  function ladder(steps) { return `<ol class="ladder">${steps.map((s, i) => [s[0], (i + 1) + " · " + s[1].replace(/^\d+ · /, ""), s[2]]).map(s => `<li class="${s[0]}"><span class="st">${s[0] === "stop" ? "✕" : s[0] === "pass" ? "→" : s[0] === "info" ? "i" : "✓"}</span><div><b>${h(s[1])}</b><small>${s[2]}</small></div></li>`).join("")}</ol>`; }
  function r2config(r) {
    const c = r.controls, sv = c.startup_validation, vl = c.version_lock, sr = r.shadow_replay;
    const steps = [];
    steps.push(sv.service_refuses_to_start ? ["stop", tr("1 · Kiểm tra cấu hình khi khởi động: CHẶN", "1 · Start-up configuration check: BLOCKED"), h(sv.errors.join("; ")) + " – " + h(tr("dịch vụ không khởi động với chính sách này.", "the service will not start with this policy."))]
      : ["pass", tr("1 · Kiểm tra cấu hình khi khởi động: không phát hiện", "1 · Start-up configuration check: not detected"), h(tr("Giá trị hợp lệ về hình thức – vì vậy cần các lớp sau.", "The value is well-formed – which is why the next layers exist."))]);
    if (!sv.service_refuses_to_start) steps.push(["stop", tr("2 · Khóa phiên bản (mã băm nội dung): CHẶN PHÁT HÀNH", "2 · Version lock (content hash): RELEASE REFUSED"), `${h(tr("Cùng phiên bản", "Same version"))} <code>${h(vl.policy_version)}</code>, ${h(tr("mã băm", "hash"))} <code>${h(vl.candidate_hash.slice(0, 12))}…</code> ≠ <code>${h(vl.locked_hash.slice(0, 12))}…</code>. ${h(tr("Mọi thay đổi ngưỡng phải tăng phiên bản và được duyệt.", "Any threshold change needs a version bump and approval."))}`]);
    if (sr) steps.push(["info", tr("3 · Chạy thử song song trên 10.000 hồ sơ: lộ tác động", "3 · Shadow replay on 10,000 rows: impact exposed"), `<b>${fmt.num(sr.decisions_changed)}</b> ${h(tr("quyết định sẽ đổi", "decisions would change"))} (${h(Object.entries(sr.flips).map(([k, v]) => k + " " + fmt.num(v)).join(", "))}) · GREEN ${fmt.num(sr.green.locked)} → <b>${fmt.num(sr.green.candidate)}</b> · ${h(tr("chuyển chuyên viên", "officer referrals"))} ${fmt.num(sr.officer_referrals.locked)} → <b>${fmt.num(sr.officer_referrals.candidate)}</b>${sr.higher_limits ? ` · ${fmt.num(sr.higher_limits)} ${h(tr("hạn mức cao hơn, trung bình +", "higher limits, average +"))}${h(fmt.short(sr.avg_limit_increase_vnd))}` : ""}`]);
    steps.push(["ok", tr("4 · Dịch vụ đang chạy: không bị ảnh hưởng", "4 · Live service: unaffected"), `${h(tr("Vẫn dùng chính sách đã khóa", "Still on the locked policy"))} <code>${h(c.live_service.policy_hash.slice(0, 12))}…</code> – ${h(tr("không khách hàng nào nhận quyết định từ cấu hình sai.", "no customer receives a decision from the wrong configuration."))}`]);
    return `<div class="chg"><span class="lbl">${h(tr("Thay đổi thử (bản sao)", "Trial change (copy)"))}</span> <code>${h(r.change.parameter)}</code>: ${h(String(r.change.from))} → <b>${h(String(r.change.to))}</b></div>${ladder(steps)}
      ${sr ? `<p class="note">${h(tr("Nhóm bị ảnh hưởng", "Affected segments"))}: ${h(Object.entries(sr.by_segment).map(([k, v]) => P("segment", k, k) + " " + fmt.num(v)).join(" · "))}. ${h(tr("Giả định chạy thử như scripts/segment_replay.py; chỉ mang tính minh họa.", "Replay assumptions as in scripts/segment_replay.py; indicative only."))}</p>` : ""}`;
  }
  function mini(res, label) { const a = res.assessment; return `<div class="br"><span class="lbl">${h(label)}</span>${band(a.band)} <span class="code">${h(a.reason_code || "")}</span>${a.secondary_codes && a.secondary_codes.length ? `<small>${h(tr("mã phụ được ghi", "secondary codes recorded"))}: ${h(a.secondary_codes.join(", "))}</small>` : ""}<small>${h(nextTxt(res.next_action))}</small></div>`; }
  function r2evidence(r) {
    if (r.case.id === "E1") {
      const b = r.branch, l2 = b.layer2_results || [];
      return `<p class="chg"><span class="lbl">${h(tr("Hồ sơ", "Profile"))}</span> ${h(r.persona.id)} · ${h(r.persona.display_name)} – ${h(tr("bản ghi Open Banking giả lập được đánh dấu khớp chỉ theo tên (không khớp CCCD)", "the simulated Open Banking record matches on name only (not on national ID)"))}</p>
        ${ladder([["info", tr("1 · Hệ thống đề nghị khách chia sẻ dữ liệu", "1 · The engine asks the customer to share data"), `${h(r.result.assessment.reason_code)} · ${h(tr("khách đồng ý mọi nguồn", "the customer ticks every source"))}`],
          ["stop", tr("2 · Kiểm tra AV-01 (khớp danh tính): bản ghi bị loại", "2 · Check AV-01 (identity match): record rejected"), h(l2.map(x => `${x.source}: ${x.outcome}`).join(" · ")) + " – " + h(tr("dữ liệu không khớp không bao giờ được dùng để tính hạn mức.", "mismatched data is never used to size a limit."))],
          ["ok", tr("3 · Kết quả: chuyển xác minh gian lận, không phê duyệt", "3 · Outcome: fraud review, no approval"), `${band(b.assessment.band)} <span class="code">${h(b.assessment.reason_code)}</span> · ${h(nextTxt(b.next_action))}`]])}`;
    }
    const comps = r.components || [];
    return `<p class="chg"><span class="lbl">${h(tr("Hồ sơ", "Profile"))}</span> ${h(r.persona.id)} · ${h(r.persona.display_name)} – ${h(tr("hai cờ rủi ro (RF-02, RF-03) và nợ đã xác minh = 100% thu nhập", "two risk flags (RF-02, RF-03) and verified debts = 100% of income"))}</p>
      <div class="branches three">${comps.map(c => mini(c.result, T(c.label))).join("")}${mini(r.result, tr("Cả hai cùng lúc", "Both at once"))}</div>
      <p class="note">${h(tr("Thứ tự ưu tiên cố định (decision_rules §3): nợ đã xác minh vượt khả năng (bước 6) đứng trước cờ rủi ro (bước 9). Kết quả luôn giống nhau với cùng dữ liệu, và quy tắc còn lại được ghi làm mã phụ để chuyên viên và kiểm toán thấy.", "Fixed precedence (decision_rules §3): a verified obligations breach (step 6) comes before risk flags (step 9). The same data always gives the same outcome, and the other rule is recorded as a secondary code for officers and audit."))}</p>`;
  }
  // ---------- R3 missing & anomalous data
  function r3() {
    const R = N.risk, res = RK.dataRes;
    return `<div class="cases six">${R.data_cases.map(c => `<button type="button" class="case ${RK.data === c.id ? "on" : ""}" data-dcase="${h(c.id)}"><span class="cid">${h(c.id)}</span><span class="chip ${["INVALID", "INCONSISTENT"].includes(c.kind) ? "red" : "amber"}">${h(tr(KIND[c.kind][0], KIND[c.kind][1]))}</span><b>${h(T(c.title))}</b></button>`).join("")}</div>
      <div class="rres">${RK.dataBusy ? `<p class="note"><span class="spin"></span> ${h(tr("Đang chạy trên bộ quy tắc thật…", "Running on the actual engine…"))}</p>` : res ? r3res(res) : `<div class="rempty">${h(tr("Chọn một trường hợp dữ liệu.", "Pick a data case."))}</div>`}</div>
      <h3>${h(tr("Cho khán giả thấy trên ứng dụng thật", "Show it in the live apps"))}</h3>${togRow("alt_data_incomplete")}`;
  }
  function r3res(r) {
    const x = r.result, rd = x.readiness, a = x.assessment, b = r.baseline;
    const det = [];
    (rd.validation_errors || []).forEach(e => det.push(["bad", e.rule_id, e.field, RULE_TXT[e.rule_id] ? tr(RULE_TXT[e.rule_id][0], RULE_TXT[e.rule_id][1]) : e.message]));
    (rd.data_quality_events || []).forEach(e => det.push(["warn", e.rule_id, e.field, RULE_TXT[e.rule_id] ? tr(RULE_TXT[e.rule_id][0], RULE_TXT[e.rule_id][1]) : e.message]));
    (rd.checks_not_passed || []).filter(c => c.result === "FAIL" && !(rd.validation_errors || []).length).forEach(c => det.push(["warn", c.check_id, "", P("check", c.check_id, c.name) + (c.detail ? " – " + c.detail : "")]));
    if (!det.length && x.limit.binding_constraint && b.approved_limit_vnd !== x.limit.approved_limit_vnd) det.push(["warn", "", "customer_profile", tr("Không rõ nhóm: thu nhập chưa phân loại được ghi nhận thận trọng – khách không bị hỏi thêm.", "Segment unknown: unclassified income is recognised conservatively – the customer is not asked.")]);
    if (!det.length) det.push(["ok", "", "", tr("Không có giá trị nào bị loại; thiếu bằng chứng được xử lý ở bước định tuyến.", "No value excluded; the missing evidence is handled at routing.")]);
    return `<div class="dgrid">
        <div><h4>${h(tr("1 · Thay đổi (bản ghi trong môi trường tách biệt)", "1 · What was changed (sandbox record)"))}</h4><p>${h(T(r.case.change))}</p>
          <p class="note">${h(tr("Từ", "From"))} ${h(r.persona.id)} · ${h(r.persona.display_name)} (<code>${h(r.persona.applicant_id)}</code>)${r.switches.length ? " · " + h(r.switches.map(swLabel).join(", ")) : ""}</p></div>
        <div><h4>${h(tr("2 · Hệ thống phát hiện", "2 · What the engine detected"))}</h4><ul class="det">${det.map(d => `<li class="${d[0]}">${d[1] ? `<span class="code">${h(d[1])}</span>` : ""}${d[2] ? ` <code>${h(d[2])}</code>` : ""} ${h(d[3])}</li>`).join("")}</ul>
          ${a.fields_not_used && a.fields_not_used.length ? `<p class="note">${h(tr("Không dùng để quyết định", "Not used for the decision"))}: ${h(a.fields_not_used.join(", "))}</p>` : ""}
          <p class="note">${h(tr("Trạng thái dữ liệu", "Data status"))}: <code>${h(rd.status || "—")}</code></p></div>
        <div><h4>${h(tr("3 · Kết quả", "3 · Outcome"))}</h4>
          <p class="cmpline"><span>${h(tr("Bản ghi sạch", "Clean record"))}: <b>${h(b.band)}</b> <span class="code">${h(b.reason_code)}</span>${b.approved_limit_vnd ? " · " + fmt.short(b.approved_limit_vnd) : ""}</span>
            <span>→ ${band(a.band)} <span class="code">${h(a.reason_code)}</span>${x.limit.approved_limit_vnd ? " · " + fmt.short(x.limit.approved_limit_vnd) : ""}</span></p>
          <p class="next n-${h((NEXT[x.next_action] || ["", "", "grey"])[2])}">${h(nextTxt(x.next_action))}</p>
          ${x.branches ? `<div class="branches">${mini(x.branches.share, tr("Khách chia sẻ", "Customer shares"))}${mini(x.branches.decline, tr("Khách không chia sẻ", "Customer declines"))}</div>` : ""}</div></div>`;
  }
  function wireRisk() {
    const el = document.getElementById("sec-risk");
    el.querySelectorAll("[data-rtab]").forEach(b => b.onclick = () => { RK.tab = b.dataset.rtab; Store.set("sc.riskTab", RK.tab); renderRisk(); const t = document.getElementById("rt-" + RK.tab); if (t) t.focus(); });
    const tl = el.querySelector(".rtabs"); if (tl) tl.onkeydown = (e) => { const ids = RISK_TABS.map(x => x[0]); const i = ids.indexOf(RK.tab); if (e.key === "ArrowRight" || e.key === "ArrowLeft") { RK.tab = ids[(i + (e.key === "ArrowRight" ? 1 : 2)) % 3]; Store.set("sc.riskTab", RK.tab); renderRisk(); document.getElementById("rt-" + RK.tab).focus(); } };
    el.querySelectorAll("[data-ctl]").forEach(x => x.onchange = () => setSwitch(x.dataset.ctl, x.checked));
    el.querySelectorAll("[data-ovp]").forEach(b => b.onclick = () => { RK.ovl = N.risk.overload.presets.find(x => x.id === b.dataset.ovp); runOverload(); });
    const run = document.getElementById("ov-run"); if (run) run.onclick = () => {
      const p = {}; ["requests", "window_s", "workers", "queue_max", "budget_ms"].forEach(k => { p[k] = parseInt(document.getElementById("ov-" + k).value, 10); });
      RK.ovl = Object.assign({ id: "custom" }, p); runOverload();
    };
    el.querySelectorAll("[data-rcase]").forEach(b => b.onclick = async () => { RK.rule = b.dataset.rcase; RK.ruleBusy = true; RK.err = ""; renderRisk(); try { RK.ruleRes = await call("POST", "/api/presenter/risk/rules", { case: RK.rule }); } catch (e) { RK.err = errText(e); } RK.ruleBusy = false; renderRisk(); });
    el.querySelectorAll("[data-dcase]").forEach(b => b.onclick = async () => { RK.data = b.dataset.dcase; RK.dataBusy = true; RK.err = ""; renderRisk(); try { RK.dataRes = await call("POST", "/api/presenter/risk/data", { case: RK.data }); } catch (e) { RK.err = errText(e); } RK.dataBusy = false; renderRisk(); });
  }
  async function runOverload() {
    const p = RK.ovl; RK.ovlBusy = true; RK.err = ""; renderRisk();
    try { RK.ovlRes = await call("POST", "/api/presenter/risk/overload", { requests: p.requests, window_s: p.window_s, workers: p.workers, queue_max: p.queue_max, budget_ms: p.budget_ms }); }
    catch (e) { RK.err = e.code === "INVALID_OVERLOAD_INPUT" ? tr("Tham số ngoài khoảng cho phép: ", "Parameter out of range: ") + Object.keys(e.details.fields || {}).join(", ") : errText(e); }
    RK.ovlBusy = false; renderRisk();
  }
  async function setSwitch(key, on) {
    try { await call("POST", "/api/presenter/controls", { key, value: on ? "1" : "0" }); RK.err = ""; await reloadState(); ping(); if (N.sim.form) runSim(false); }
    catch (e) { RK.err = errText(e); renderRisk(); }
  }

  // ================================================================== 4. presenter tools
  function todayISO() { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
  function nowText(long) {
    const d = new Date(), loc = vi() ? "vi-VN" : "en-GB";
    const date = d.toLocaleDateString(loc, long ? { weekday: "long", day: "2-digit", month: "long", year: "numeric" } : { day: "2-digit", month: "2-digit", year: "numeric" });
    return date + " · " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: long ? "2-digit" : undefined });
  }
  function tick() {
    const a = document.getElementById("clock-top"); if (a) a.textContent = tr("Hôm nay ", "Today ") + nowText(false);
    const b = document.getElementById("clock-now"); if (b) b.textContent = nowText(true);
    const z = document.getElementById("clock-tz"); if (z) { try { z.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { z.textContent = ""; } }
  }
  setInterval(tick, 1000);
  function renderTools() {
    const el = document.getElementById("sec-tools"); if (!el || !N.state) return;
    const bd = N.state.business_date, today = todayISO();
    el.innerHTML = `<div class="pg-head"><span class="num">4</span><div><h2 id="h-tools">${h(tr("Công cụ trình diễn", "Presenter tools"))}</h2></div></div>
      <div class="tgrid">
        <div class="tcard"><h3>${h(tr("Ngày giờ hiện tại", "Current date & time"))}</h3><p class="clock" id="clock-now"></p><p class="note" id="clock-tz"></p>
          <p class="note">${h(tr("Theo đồng hồ của máy trình chiếu, cập nhật mỗi giây.", "From the presenting device's clock, updated every second."))}</p></div>
        <div class="tcard"><h3>${h(tr("Ngày nghiệp vụ giả lập", "Simulated business date"))}</h3><p class="clock">${h(fmt.date(bd))}</p>
          <p class="note">${h(tr("Dùng cho hạn trả, độ mới dữ liệu và mọi phép tính của hệ thống (kể cả mô phỏng).", "Drives due dates, data freshness and every engine calculation (including the simulator)."))}</p>
          <div class="row">${[1, 30, 40, 95].map(d => `<button type="button" data-adv="${d}">+${d} ${h(tr("ngày", d === 1 ? "day" : "days"))}</button>`).join("")}
            ${today > bd ? `<button type="button" id="to-today">${h(tr("Đặt bằng hôm nay", "Set to today"))} (${h(fmt.date(today))})</button>` : ""}</div></div>
        <div class="tcard"><h3>${h(tr("Giao dịch đang mở", "Open checkouts"))}</h3>
          ${N.open.length ? N.open.map(x => `<div class="row"><span class="note">${h(x.customer_id)} · ${h(x.merchant_name)} · ${fmt.vnd(x.amount_vnd)}</span><button type="button" data-exp="${h(x.transaction_id)}">${h(tr("Hết hạn ngay", "Expire now"))}</button></div>`).join("")
            : `<p class="note">${h(tr("Không có giao dịch đang mở.", "No unfinished checkout."))}</p>`}</div>
        <div class="tcard"><h3>${h(tr("Trạng thái sự cố", "Switch status"))}</h3>
          ${activeSwitches().length ? `<ul class="pl small">${activeSwitches().map(k => `<li>${h(swLabel(k))}</li>`).join("")}</ul><div class="row"><button type="button" id="clear-sw">${h(tr("Tắt tất cả", "Clear all switches"))}</button></div>`
            : `<p class="note"><span class="chip green">${h(tr("Bình thường", "All normal"))}</span> ${h(tr("Không có sự cố nào đang bật.", "No switch is on."))}</p>`}
          <details class="extra"><summary>${h(tr("Kiểm thử vận hành khác (ứng dụng khách hàng)", "Other operational tests (customer app)"))}</summary>
            ${["device_anomaly", "force_next_payment_failure", "force_next_auth_failure"].map(togRow).join("")}
            <div class="row"><button type="button" id="tamper">${h(tr("Thử QR bị sửa", "Try a tampered QR"))}</button></div></details></div>
        <div class="tcard"><h3>${h(tr("Dữ liệu demo", "Demo data"))}</h3>
          <div class="row"><button type="button" id="reload">${h(tr("Làm mới", "Refresh"))}</button><button type="button" id="reset">${h(tr("Đặt lại dữ liệu demo", "Reset demo data"))}</button></div>
          <p class="note">${h(tr("Đặt lại xóa khách hàng giả định, tắt mọi sự cố, giữ nhật ký kiểm toán. Hồ sơ mẫu không bị ảnh hưởng.", "Reset removes hypothetical customers, clears every switch and keeps the audit log. Personas are unaffected."))}</p>
          <div class="row"><a class="btnlike" href="/" target="hlb-customer">${h(tr("Ứng dụng khách hàng ↗", "Customer app ↗"))}</a><a class="btnlike" href="/officer.html" target="hlb-officer">${h(tr("Bàn chuyên viên ↗", "Officer workbench ↗"))}</a></div></div>
      </div>
      ${N.toolMsg ? `<p class="nav-status" role="status">${h(N.toolMsg)}</p>` : ""}`;
    tick();
    const after = async (m) => { N.toolMsg = m || ""; await reloadState(); ping(); pollNow(); loadShowcase(); if (N.sim.form) runSim(false); };
    el.querySelectorAll("[data-ctl]").forEach(x => x.onchange = () => setSwitch(x.dataset.ctl, x.checked));
    const cs = document.getElementById("clear-sw");
    if (cs) cs.onclick = async () => { try { for (const k of activeSwitches()) await call("POST", "/api/presenter/controls", { key: k, value: "0" }); await after(tr("Đã tắt mọi sự cố.", "All switches cleared.")); } catch (e) { N.toolMsg = errText(e); renderTools(); } };
    const tb = document.getElementById("tamper");
    if (tb) tb.onclick = async () => { // own short-lived session for a fictional customer; never touches the open customer app
      let tok = null;
      try {
        tok = (await api("POST", "/api/session/customer", { customer_id: "CUS-001" })).token;
        const q = (await api("GET", "/api/qr/demo-codes", undefined, tok)).find(x => x.kind === "DYNAMIC");
        const bad = q.payload.replace(/54(\d\d)(\d+)/, (m, l, a) => "54" + l + String(Number(a[0]) % 9 + 1) + a.slice(1));
        await api("POST", "/api/qr/resolve", { payload: bad }, tok);
        N.toolMsg = tr("Không mong đợi: QR được chấp nhận", "Unexpected: accepted");
      } catch (e) { N.toolMsg = tr("Đã chặn QR bị sửa: ", "Tampered QR blocked: ") + e.code; }
      finally { if (tok) api("POST", "/api/session/sign-out", {}, tok).catch(() => {}); }
      renderTools();
    };
    el.querySelectorAll("[data-adv]").forEach(b => b.onclick = async () => { try { await call("POST", "/api/presenter/clock", { advance_days: +b.dataset.adv }); await after(); } catch (e) { N.toolMsg = errText(e); renderTools(); } });
    const tt = document.getElementById("to-today");
    if (tt) tt.onclick = async () => { try { await call("POST", "/api/presenter/clock", { date: todayISO() }); await after(); } catch (e) { N.toolMsg = errText(e); renderTools(); } };
    el.querySelectorAll("[data-exp]").forEach(b => b.onclick = async () => { try { await call("POST", `/api/presenter/transactions/${b.dataset.exp}/expire-now`); await after(tr("Đã hết hạn giao dịch.", "Checkout expired.")); } catch (e) { N.toolMsg = errText(e); renderTools(); } });
    document.getElementById("reload").onclick = () => after();
    document.getElementById("reset").onclick = async () => {
      if (!await ask(tr("Đặt lại toàn bộ dữ liệu demo cho mọi người dùng?", "Reset all demo data for everyone?"), "OK", tr("Quay lại", "Back"))) return;
      try { await call("POST", "/api/presenter/reset"); N.live = null; N.status = null; Store.del("sc.liveCid"); stopPoll(); renderLive(); await after(tr("Đã đặt lại dữ liệu demo.", "Demo data reset.")); }
      catch (e) { N.toolMsg = errText(e); renderTools(); }
    };
  }

  load();
})();
