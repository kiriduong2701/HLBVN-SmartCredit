// Credit officer workbench (v7 layout). Case assumption: everyone who reaches this dashboard is an authorised officer
// (production: HLBVN staff SSO), so a server session opens automatically - no profile picker, no access code.
//
// Role separation:
//   * "Review & decide" tab  - what a credit officer needs: why the case is here, the key numbers, risk signals, data
//     checks, consent & data sources, decision history, and the decision form (authority shown, rules enforced inline).
//   * "Audit & technical" tab - compliance / model-risk monitoring: the append-only audit log with raw details, the
//     decision trace (rule ids, policy version + hash), the full capacity calculation, API call log and governance.
// Nothing is removed: every technical item from the previous layout is still on screen, just in the second tab.
// The server enforces every rule again (EL-01..EL-10); the form mirrors them so mistakes are caught before submitting.
(function () {
  const h = (s) => fmt.esc(s);
  const $ = (id) => document.getElementById(id);
  const vi = () => L.lang === "vi";
  const tr = (v, e) => (vi() ? v : e);
  const P = (g, k, fb) => PLAIN.get(g, k, fb);
  const qs = new URLSearchParams(location.search);
  const st = { token: Store.get("ow.token", null), officer: Store.get("ow.officer", null),
    sel: qs.get("app") || Store.get("ow.sel", null), lastSig: "", case: null, tab: "review", form: {} };
  if (qs.get("app")) { Store.set("ow.sel", st.sel); history.replaceState(null, "", location.pathname); }
  const call = (m, p, b) => api(m, p, b, st.token);
  const pct = (r, d) => (r === null || r === undefined ? "—" : (Number(r) * 100).toLocaleString(vi() ? "vi-VN" : "en-US", { maximumFractionDigits: d === undefined ? 1 : d }) + "%");
  const yn = (v) => (v === 1 || v === true ? tr("Có", "Yes") : tr("Không", "No"));
  const short = (n) => (n === null || n === undefined ? "—" : fmt.short(n));

  function flash(msg, err) {
    const d = document.createElement("div"); d.className = "toast ow-toast" + (err ? " err" : ""); d.setAttribute("role", err ? "alert" : "status");
    d.textContent = msg; document.body.appendChild(d); setTimeout(() => d.remove(), 5000);
  }
  const STATE_CHIP = { APPROVED: "green", REJECTED: "red", DECLINED: "red", MANUAL_REVIEW: "amber", MORE_INFORMATION_REQUIRED: "blue",
    ASSESSMENT_PENDING: "blue", OFFER_PENDING: "blue", CANCELLED: "grey", POLICY_STOP: "red" };
  const stateChip = (s) => `<span class="chip ${STATE_CHIP[s] || "grey"}">${h(P("state", s, s))}</span>`;
  // Visual tone of a referral reason (queue accent + summary strip): fraud/identity, data integrity, risk judgement, capacity.
  const tone = (rc) => (!rc ? "grey" : rc === "R010-FRD" ? "fraud" : rc === "R010-VER" || rc === "R010-OBL" ? "data" : rc.startsWith("R020") ? "red"
    : rc.startsWith("R001") ? "green" : "amber");
  const segLabel = (s) => (s ? P("segment", s, s) : tr("Chưa rõ nhóm khách hàng", "Segment unknown"));

  // ------------------------------------------------------------------ chrome
  function top() {
    const au = st.authority;
    $("top").innerHTML = `<a class="brand" href="/officer.html" aria-label="Hong Leong Bank"><img src="/img/hlb-logo-horizontal.png" alt="Hong Leong Bank"></a>
      <span class="divider" aria-hidden="true"></span><span class="title">Smart Credit · ${h(tr("Bàn thẩm định tín dụng", "Credit officer workbench"))}</span>
      <span class="spacer"></span>
      <span class="today" id="ow-today"></span>
      ${st.officer ? `<span class="who"><b>${h(st.officer.officer_id)}</b>${au ? ` · ${h(tr("Thẩm quyền", "Authority"))} ${au.exclusive ? "&lt;" : "≤"} ${fmt.vnd(au.max_vnd)}` : ""}</span>` : ""}
      <a class="navlink" href="/navigation.html" target="hlb-navigation">${h(tr("Bảng trình diễn ↗", "Presenter page ↗"))}</a>
      <span class="lang-toggle" role="group" aria-label="Language"><button data-lang="vi" aria-pressed="${vi()}">VI</button><button data-lang="en" aria-pressed="${!vi()}">EN</button></span>`;
    $("banner").textContent = tr("Bản mẫu · dữ liệu và kết nối đều giả lập · phiên chuyên viên mở tự động (không phải SSO). Thẩm quyền đến 90.100.000 ₫ là giả định tình huống, không phải chính sách của HLBVN.",
      "Prototype · all data and connections simulated · officer session opens automatically (not SSO). Authority up to VND 90,100,000 is a case assumption, not HLBVN policy.");
    document.title = "Smart Credit · " + tr("Bàn thẩm định tín dụng", "Credit officer workbench");
    document.querySelectorAll("[data-lang]").forEach(b => b.onclick = () => { L.set(b.dataset.lang); st.lastSig = ""; init(); });
    tickToday();
  }
  function tickToday() {
    const el = $("ow-today"); if (!el) return;
    const d = new Date();
    el.textContent = tr("Hôm nay ", "Today ") + d.toLocaleDateString(vi() ? "vi-VN" : "en-GB", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
    el.title = tr("Ngày hệ thống (thiết bị)", "System date (device)");
  }
  setInterval(tickToday, 60000);

  async function openSession() {
    const s = await api("POST", "/api/session/officer", {});
    st.token = s.token; st.officer = s.officer; Store.set("ow.token", s.token); Store.set("ow.officer", s.officer);
  }

  async function init() {
    if (!st.token) { try { await openSession(); } catch (e) { top(); $("root").innerHTML = `<div class="panel">${h(e.message)}</div>`; return; } }
    top();
    $("root").innerHTML = `<div class="ow-body"><aside id="q" aria-label="${h(tr("Hàng chờ", "Queue"))}"></aside><main id="case"><div class="panel placeholder">${h(tr("Chọn một hồ sơ trong hàng chờ.", "Select a case from the queue."))}</div></main></div>`;
    st.lastSig = "";
    await loadQueue(true);
  }

  // ------------------------------------------------------------------ queue
  async function loadQueue(force) {
    let q;
    try { q = await call("GET", "/api/officer/queue"); }
    catch (e) { if (e.code === "SIGN_IN_REQUIRED" || e.code === "FORBIDDEN_ROLE") { st.token = null; Store.del("ow.token"); return init(); } return; }
    if (!st.authority) { st.authority = q.authority; top(); }
    const sig = JSON.stringify(q.queues) + JSON.stringify(q.stats) + st.sel;
    if (sig === st.lastSig && !force) return;
    st.lastSig = sig;
    const s = q.stats;
    const item = (c) => {
      const amt = c.approvable === true ? `≤ ${short(c.max_approvable_vnd)}` : c.approvable === false ? tr("Chưa duyệt được", "Not approvable") : (c.requested_limit_vnd ? short(c.requested_limit_vnd) : "");
      return `<button class="qitem tone-${tone(c.reason_code)} ${c.application_id === st.sel ? "on" : ""}" data-app="${h(c.application_id)}" aria-pressed="${c.application_id === st.sel}">
        <span class="r1"><span class="nm">${h(c.customer_name)}</span><span class="amt ${c.approvable === false ? "bad" : ""}">${h(amt)}</span></span>
        <span class="r2">${h(P("reason", c.reason_code, c.reason_label || ""))}</span>
        <span class="r3">${c.kind !== "LIMIT_ACTIVATION" ? `<span class="chip grey">${h(P("kind", c.kind, c.kind))}</span>` : ""}${c.is_appeal ? `<span class="chip amber">${h(tr("Khiếu nại", "Appeal"))}</span>` : ""}
          ${c.capacity_basis === "ALTERNATIVE_DATA" || c.alternative_data_rounds ? `<span class="chip blue">${h(tr("Có dữ liệu bổ sung", "Extra data"))}</span>` : ""}
          ${c.age_hours !== null ? `<span>${h(tr("Chờ", "Waiting"))} ${c.age_hours}h</span>` : ""}${c.sla_breached ? `<span class="chip red">${h(tr("Quá SLA", "SLA breached"))}</span>` : ""}</span></button>`;
    };
    const list = (k) => q.queues[k].map(item).join("") || `<p class="note">${h(tr("Không có hồ sơ.", "No cases."))}</p>`;
    const fold = (k, label, hint) => `<details class="panel qfold" ${q.queues[k].length && k !== "decided" ? "open" : ""}><summary><span>${h(label)}</span><span class="count">${q.queues[k].length}</span></summary>
      <p class="note">${h(hint)}</p>${list(k)}</details>`;
    $("q").innerHTML = `<div class="panel qhead"><h2>${h(tr("Hàng chờ của bạn", "Your queue"))}</h2>
        <div class="kpis"><div class="kpi"><span class="v">${s.open_reviews}</span><span class="k">${h(tr("Cần quyết định", "To decide"))}</span></div>
          <div class="kpi ${s.sla_breaches ? "bad" : ""}"><span class="v">${s.sla_breaches}</span><span class="k">${h(tr("Quá SLA", "SLA breaches"))}</span></div>
          <div class="kpi"><span class="v">${s.oldest_age_hours}h</span><span class="k">${h(tr("Chờ lâu nhất", "Oldest"))}</span></div></div>
        <p class="note">${h(tr("Ngày nghiệp vụ (giả lập)", "Business date (simulated)"))}: <b>${h(fmt.date(s.business_date))}</b></p>
        <p class="note">${h(tr(`SLA ${s.sla_hours} giờ · năng lực ${s.daily_review_capacity} hồ sơ/ngày · hôm nay ${s.referred_today} hồ sơ chuyển đến`, `SLA ${s.sla_hours}h · capacity ${s.daily_review_capacity} cases/day · ${s.referred_today} referred today`))}</p>
        ${s.capacity_alert ? `<p class="callout warn">${h(tr("Vượt năng lực xử lý: tạm dừng mở rộng thí điểm (luồng duyệt không bị thay đổi để giải phóng hàng chờ).", "Above review capacity: pilot expansion pauses (routing is never changed to clear a queue)."))}</p>` : ""}</div>
      <div class="panel"><h2>${h(tr("Cần bạn quyết định", "Needs your decision"))} <span class="count">${q.queues.pending_review.length}</span></h2>${list("pending_review")}</div>
      ${fold("qa_sample", tr("Kiểm tra chất lượng (mẫu tự duyệt)", "Quality check (auto-approved sample)"), tr("Mẫu ngẫu nhiên 5% (10% nếu dùng dữ liệu bổ sung) các hồ sơ hệ thống đã tự duyệt.", "Random 5% (10% with extra data) of automatic approvals."))}
      ${fold("awaiting_customer", tr("Hệ thống đang xử lý – không cần bạn", "System handling – no action for you"), tr("Đang chờ khách chia sẻ dữ liệu hoặc hệ thống tự thử lại nguồn bị gián đoạn. Chuyên viên không yêu cầu dữ liệu.", "Waiting for the customer to share data or for an automatic retry. Officers never request data."))}
      ${fold("decided", tr("Đã quyết định gần đây", "Recently decided"), tr("20 hồ sơ gần nhất (chỉ xem).", "Latest 20 cases (read-only)."))}`;
    $("q").onclick = (e) => { const b = e.target.closest("[data-app]"); if (!b) return; st.sel = b.dataset.app; st.tab = "review"; st.form = {}; Store.set("ow.sel", st.sel); loadCase(st.sel); loadQueue(true); };
    if (st.sel && force) loadCase(st.sel, true);
  }

  async function loadCase(id, quiet) {
    let c;
    try { c = await call("GET", "/api/officer/cases/" + id); }
    catch (e) { if (e.status === 404) { st.sel = null; Store.del("ow.sel"); return; } if (!quiet) flash(e.message, true); return; }
    st.case = c; renderCase(c);
  }

  // ------------------------------------------------------------------ helpers for the review tab
  function incomeTotals(cs) {
    const s = (cs && cs.income_streams) || [];
    return { avg: s.length ? s.reduce((a, x) => a + (x.average_vnd || 0), 0) : null, low: s.length ? s.reduce((a, x) => a + (x.stressed_vnd || 0), 0) : null };
  }
  const riskFields = () => ({ unified_credit_score: [tr("Điểm tín dụng", "Credit score"), "int"], on_time_bill_ratio: [tr("Trả hóa đơn đúng hạn", "Bills paid on time"), "pct"],
    rapid_drain_flag: [tr("Rút sạch tiền ngay sau khi nạp", "Funds drained after top-up"), "flag"], internal_delinquency_flag: [tr("Từng trễ hạn Smart Credit", "Earlier Smart Credit arrears"), "flag"],
    alt_on_time_bill_ratio: [tr("Trả hóa đơn đúng hạn (dữ liệu bổ sung)", "Bills on time (extra data)"), "pct"], dti_ratio: [tr("Tỷ lệ nợ / thu nhập", "Debt-to-income"), "pct"] });
  const fmtv = (v, kind) => (v === null || v === undefined ? "—" : kind === "pct" ? pct(v) : kind === "flag" ? yn(v) : fmt.num(v));
  function riskSentence(r) {
    const kind = (riskFields()[r.field] || [null, "int"])[1];
    return PLAIN.tpl(P("risk", r.rule_id, r.message || r.rule_id), { v: fmtv(r.value, kind), t: fmtv(r.threshold, kind) });
  }

  function whyCard(c) {
    const a = c.application, rt = c.routing || {}, cs = c.case_summary || {}, pr = c.profile || {};
    const ind = {}; (pr.risk_indicators || []).forEach(r => { ind[r.rule_id] = r; });
    const reasons = (rt.reasons || []).map(r => {
      const txt = ind[r.rule_id] ? riskSentence(ind[r.rule_id]) : (P("risk", r.rule_id, null) || r.message);
      return `<li><span class="txt">${h(txt)}</span> <span class="code" title="${h(r.message)}">${h(r.rule_id)}</span></li>`;
    }).join("");
    const todo = [];
    if (a.credit_state === "MANUAL_REVIEW") {
      if (a.appeal_count) todo.push(PLAIN.pick(PLAIN.D.unresolved.APPEAL));
      const u = PLAIN.D.unresolved[a.reason_code]; if (u) todo.push(PLAIN.pick(u));
      else (cs.unresolved_issues || []).forEach(x => todo.push(x));
    }
    const lead = P("reason", a.reason_code, c.reason_label || a.reason_code || "");
    return `<section class="panel card-why" aria-labelledby="why-h"><h2 id="why-h"><span class="step">1</span>${h(tr("Vì sao hồ sơ cần bạn", "Why this case needs you"))}</h2>
      <p class="lead">${h(lead)} <span class="code">${h(a.reason_code || "")}</span></p>
      ${reasons ? `<ul class="reasons">${reasons}</ul>` : ""}
      ${a.reason_code && a.reason_code.startsWith("R010-FLAG") || Object.values(ind).some(r => r.triggered && r.source === "DATASET_PERCENTILE") ? `<p class="note">${h(PLAIN.pick(PLAIN.D.risk_note))}</p>` : ""}
      ${todo.length ? `<div class="todo"><b>${h(tr("Điều bạn cần quyết định", "What you need to decide"))}</b><ul>${todo.map(x => `<li>${h(x)}</li>`).join("")}</ul></div>` : ""}
      ${a.credit_state === "MANUAL_REVIEW" ? `<p class="note">${h(tr("Dữ liệu đã được hệ thống thu thập và kiểm tra tự động – bạn không cần yêu cầu thêm. Bạn quyết định trên bằng chứng dưới đây.", "Data was collected and checked automatically – there is nothing to request. Decide on the evidence below."))}</p>` : ""}
      ${a.requested_limit_vnd && a.requested_limit_vnd > (c.eligibility.authority || {}).max_vnd ? `<p class="callout info">${h(tr(`Khách đề nghị ${fmt.vnd(a.requested_limit_vnd)}: phần trên 90.100.000 ₫ đã chuyển bộ phận cho vay của HLB, ngoài Smart Credit.`, `Requested ${fmt.vnd(a.requested_limit_vnd)}: the part above VND 90,100,000 was referred to HLB lending, outside Smart Credit.`))}</p>` : ""}
    </section>`;
  }

  function figuresCard(c) {
    const cs = c.case_summary || {}, el = c.eligibility || {}, a = c.application, cap = c.capacity || {}, ap = (c.profile || {}).applicant || {};
    const inc = incomeTotals(cs);
    const ob = cs.obligations || {};
    const tile = (k, v, sub, cls) => `<div class="fig ${cls || ""}"><span class="k">${h(k)}</span><span class="v">${v}</span>${sub ? `<span class="s">${h(sub)}</span>` : ""}</div>`;
    void a;
    const bind = cs.binding_constraint || cap.binding_constraint;
    const p = cap.parameters || {};
    const sz = (x) => x ? `${fmt.vnd(x.income_vnd)} × 50% − ${fmt.vnd(ob.monthly_vnd || 0)} = ${fmt.vnd(x.monthly_headroom_vnd)} ${tr("/tháng", "/month")}` : "—";
    return `<section class="panel" aria-labelledby="fig-h"><h2 id="fig-h"><span class="step">2</span>${h(tr("Thu nhập & khả năng trả nợ", "Income & repayment capacity"))}</h2>
      <div class="figs sm">
        ${tile(tr("Thu nhập bình quân/tháng", "Average monthly income"), inc.avg !== null ? fmt.vnd(inc.avg) : "—", tr("đã xác minh", "verified"))}
        ${tile(tr("Tháng thu nhập thấp", "Low-income month"), inc.low !== null ? fmt.vnd(inc.low) : "—", tr("phân vị 25%", "25th percentile"))}
        ${tile(tr("Nghĩa vụ nợ/tháng", "Debt payments/month"), ob.known === false ? tr("Chưa rõ", "Unknown") : fmt.vnd(ob.monthly_vnd || 0), ob.basis === "CIC_NO_RECORD" ? tr("CIC không có hồ sơ", "no CIC record") : ob.basis === "TX_OBSERVED" ? tr("quan sát từ giao dịch", "seen in transactions") : ob.basis === "CIC_REPORTED" ? tr("theo CIC", "from CIC") : "")}
        ${tile(tr("Nợ / thu nhập", "Debt-to-income"), pct(ap.dti_ratio), tr("trần tính toán 50%", "ceiling 50%"))}
      </div>
      ${bind ? `<p class="note">${h(tr("Hạn mức bị giới hạn bởi", "The limit is set by"))} <b>${h(P("constraint", bind, bind))}</b>.</p>` : ""}
      ${cap.computable ? `<details class="how"><summary>${h(tr("Cách tính (rút gọn)", "How it is calculated (simplified)"))}</summary>
        <ol class="note"><li>${h(tr("Tự động (tháng thấp):", "Automatic (low month):"))} ${h(sz(cap.stp))} → ${cap.stp ? fmt.vnd(cap.stp.limit_vnd) : "—"}</li>
        <li>${h(tr("Tối đa của chuyên viên (bình quân):", "Officer maximum (average):"))} ${h(sz(cap.officer))} → ${fmt.vnd(cap.officer_max_limit_vnd)}</li>
        <li>${h(tr(`Trần: ${p.income_multiplier_cap || 2} × thu nhập được ghi nhận · trần sản phẩm ${fmt.vnd(p.product_max_limit_vnd)} · làm tròn xuống bước ${fmt.vnd(el.step_vnd)}.`, `Caps: ${p.income_multiplier_cap || 2} × recognised income · product max ${fmt.vnd(p.product_max_limit_vnd)} · rounded down to ${fmt.vnd(el.step_vnd)}.`))}</li></ol>
        <p class="note">${h(tr("Công thức đầy đủ ở tab Nhật ký & kỹ thuật.", "The full formula is in the Audit & technical tab."))}</p></details>`
        : `<p class="callout warn">${h(tr("Chưa tính được khả năng trả nợ từ dữ liệu đã xác minh: ", "Repayment capacity could not be computed from verified data: "))}${h(cap.reason || "—")}</p>`}
    </section>`;
  }

  function signalsCard(c) {
    const pr = c.profile || {}, ap = pr.applicant || {};
    const rows = (pr.risk_indicators || []).filter(r => r.value !== null && r.value !== undefined).sort((x, y) => (y.triggered ? 1 : 0) - (x.triggered ? 1 : 0)).map(r => {
      const f = riskFields()[r.field] || [r.field, "int"];
      const op = { lt: "<", gt: ">", eq: "=" }[r.op] || r.op;
      return `<tr class="${r.triggered ? "trig" : ""}"><td>${h(f[0])}</td><td class="num">${h(fmtv(r.value, f[1]))}</td><td class="note">${h(tr("xem xét nếu", "review if"))} ${h(op)} ${h(fmtv(r.threshold, f[1]))}</td>
        <td>${r.triggered ? `<span class="chip amber">${h(tr("Cần xem", "Check"))}</span>` : `<span class="chip green">${h(tr("Ổn", "OK"))}</span>`}</td></tr>`;
    }).join("");
    const info = (k, v, n) => `<tr><td>${h(k)}</td><td class="num">${v}</td><td class="note" colspan="2">${h(n || "")}</td></tr>`;
    const exp = pr.exposure_view || [];
    return `<section class="panel" aria-labelledby="sig-h"><h2 id="sig-h"><span class="step">3</span>${h(tr("Tín hiệu rủi ro & hồ sơ", "Risk signals & profile"))}</h2>
      <table class="t clean"><thead><tr><th>${h(tr("Tín hiệu", "Signal"))}</th><th class="num">${h(tr("Giá trị", "Value"))}</th><th>${h(tr("Ngưỡng", "Threshold"))}</th><th></th></tr></thead><tbody>
        ${rows}
        ${info(tr("Lịch sử tín dụng CIC", "CIC credit history"), h(ap.has_formal_cic_history === 1 ? tr("Có", "Yes") : tr("Không có", "None")), ap.has_formal_cic_history === 1 ? "" : tr("Không phải lý do từ chối", "Never a reason to decline"))}
        ${info(tr("Biến động thu nhập", "Income variability"), h(pct(ap.inflow_cv, 0)), tr("dùng để tính tháng thu nhập thấp", "used for the low-income month"))}
        ${info(tr("Tỷ lệ nợ / thu nhập", "Debt-to-income"), h(pct(ap.dti_ratio)), tr("đã nằm trong công thức (≤ 50%)", "already in the formula (≤ 50%)"))}
        ${info(tr("Nạp ví điện tử / tháng", "E-wallet top-ups / month"), h(ap.e_wallet_topups_pm == null ? "—" : ap.e_wallet_topups_pm), tr("chỉ để tham khảo, không dùng để quyết định", "for reference only, not a decision input"))}
        ${info(tr("Số dư CASA bình quân", "Average CASA balance"), h(ap.casa_balance_vnd == null ? "—" : fmt.vnd(ap.casa_balance_vnd)), tr("chỉ để tham khảo, không dùng để quyết định", "for reference only, not a decision input"))}
      </tbody></table>
      <h3>${h(tr("Dư nợ hiện tại tại HLB", "Current exposure at HLB"))}</h3>
      <ul class="plain">${exp.map(x => `<li><span>${h(x.label)}${x.status && x.status !== "NONE" ? ` · ${h(x.status)}` : ""}</span><span class="num">${fmt.vnd(x.outstanding_vnd)}${x.monthly_instalment_vnd ? ` · ${fmt.vnd(x.monthly_instalment_vnd)}${tr("/tháng", "/mo")}` : ""}</span></li>`).join("")}
        <li class="tot"><span>${h(tr("Tổng dư nợ", "Total exposure"))}</span><span class="num">${fmt.vnd(pr.total_hlb_exposure_vnd)}</span></li></ul>
      ${(pr.repayment_history || {}).internal_delinquency_flag ? `<p class="callout warn">${h(tr("Có ghi nhận trễ hạn Smart Credit trước đây (dữ liệu nội bộ).", "Earlier Smart Credit arrears recorded (internal data)."))}</p>` : ""}
    </section>`;
  }

  function checksCard(c) {
    const rd = c.readiness || {}, cs = c.case_summary || {}, ch = rd.checks_performed || [];
    const n = (r) => ch.filter(x => x.result === r).length;
    const issues = ch.filter(x => x.result !== "PASS");
    const row = (x) => `<li class="ck ck-${h(x.result)}"><span class="ico" aria-hidden="true">${x.result === "PASS" ? "✓" : x.result === "FAIL" ? "✕" : x.result === "INFO" ? "i" : "!"}</span>
      <span class="txt"><b>${h(P("check", x.check_id, x.name))}</b><span class="note">${h(x.detail)}</span></span><span class="res">${h(P("result", x.result, x.result))}</span></li>`;
    const dq = cs.data_quality_events || [];
    const l1ok = (c.data_acquisition || {}).layer1_sufficient;
    return `<section class="panel" aria-labelledby="chk-h"><h2 id="chk-h"><span class="step">4</span>${h(tr("Kiểm tra dữ liệu", "Data checks"))}</h2>
      <p class="sumline"><span class="chip green">${n("PASS")} ${h(tr("đạt", "passed"))}</span>${n("WARN") ? `<span class="chip amber">${n("WARN")} ${h(tr("lưu ý", "attention"))}</span>` : ""}${n("FAIL") ? `<span class="chip red">${n("FAIL")} ${h(tr("không đạt", "failed"))}</span>` : ""}${n("INFO") ? `<span class="chip blue">${n("INFO")} ${h(tr("thông tin", "info"))}</span>` : ""}
        <span class="note">${h(tr("Dữ liệu tự động đủ để quyết định:", "Automatic data sufficient:"))} <b>${l1ok === true ? h(tr("có", "yes")) : l1ok === false ? h(tr("không – đã dùng dữ liệu bổ sung", "no – extra data used")) : "—"}</b></span></p>
      ${issues.length ? `<ul class="checks">${issues.map(row).join("")}</ul>` : `<p class="note">${h(tr("Tất cả kiểm tra đều đạt.", "All checks passed."))}</p>`}
      ${dq.length ? `<div class="callout info"><b>${h(tr("Giá trị bị loại khỏi tính toán (hệ thống tự xử lý):", "Values excluded from use (handled automatically):"))}</b> ${dq.map(e => h((e.field || "") + " – " + (e.rule_id || ""))).join("; ")}</div>` : ""}
      <details><summary>${h(tr(`Xem tất cả ${ch.length} kiểm tra`, `Show all ${ch.length} checks`))}</summary><ul class="checks">${ch.map(row).join("")}</ul>
        <p class="note">${h(tr("Bộ quy tắc", "Rule set"))} ${h(rd.policy_version || "")} · ${h(tr("đường bằng chứng", "evidence paths"))}: ${h((rd.evidence_paths_met || []).join(", ") || "—")}</p></details>
    </section>`;
  }

  function consentCard(c) {
    const d = c.data_acquisition || { layer1: [], layer2: [] };
    const stc = (x) => ({ OK: "green", ACCEPTED: "green", NO_RECORD: "grey", NOT_FOUND: "grey", NO_DATA: "amber", PARTIAL: "amber", UNAVAILABLE: "red", INVALID: "red" }[x] || "grey");
    const consentName = (k) => k.startsWith("ALT_DATA:") ? tr("Chia sẻ: ", "Share: ") + P("l2", k.slice(9), k.slice(9)) : P("consent", k, k);
    const cons = (c.consents || []).map(x => `<li><span class="ico ${x.withdrawn_at ? "bad" : x.granted ? "ok" : "bad"}" aria-hidden="true">${x.granted && !x.withdrawn_at ? "✓" : "✕"}</span>
      <span class="txt"><b>${h(consentName(x.kind))}</b>${vi() ? "" : `<span class="note">${h(x.purpose || "")}</span>`}</span>
      <span class="res">${x.withdrawn_at ? `<span class="chip red">${h(tr("Đã rút", "Withdrawn"))} ${fmt.dt(x.withdrawn_at)}</span>` : x.granted ? `<span class="chip green">${h(tr("Đã đồng ý", "Given"))}</span>` : `<span class="chip grey">${h(tr("Không đồng ý", "Not given"))}</span>`}<span class="note">${fmt.dt(x.recorded_at)}</span></span></li>`).join("");
    const l1 = (d.layer1 || []).map(e => `<li><span class="txt"><b>${h(P("l1", e.source, e.label))}</b><span class="note">${h(P("l1_summary", e.source + ":" + e.status, e.summary))}</span><span class="note dim">${h(e.legal_basis || "")}</span></span>
      <span class="res"><span class="chip ${stc(e.status)}">${h(P("status", e.status, e.status))}</span></span></li>`).join("");
    const l2 = (d.layer2 || []).map((r, i) => `<li class="round"><span class="txt"><b>${h(tr(`Lần ${i + 1}: hệ thống đề nghị khách chia sẻ`, `Round ${i + 1}: system asked the customer to share`))}</b>
        <span class="note">${fmt.dt(r.answered_at)} · ${h(P("l2_outcome", r.outcome, r.outcome || ""))}</span>
        ${(r.results || []).map(x => `<span class="sub"><span>${h(P("l2", x.source, x.label))}</span> <span class="chip ${stc(x.outcome)}">${h(P("status", x.outcome, x.outcome))}</span>
          ${x.validation ? x.validation.checks.filter(v => !v.passed).map(v => `<span class="note bad">✕ ${h(v.label)}</span>`).join("") : ""}</span>`).join("") || `<span class="note">${h(tr("Không gọi nguồn nào (khách không đồng ý).", "No source called (no consent)."))}</span>`}
      </span></li>`).join("");
    const cs = c.consents || [], given = cs.filter(x => x.granted && !x.withdrawn_at).length, withdrawn = cs.filter(x => x.withdrawn_at).length;
    const l1ok = (d.layer1 || []).filter(e => e.status === "OK" || e.status === "NO_RECORD").length, l1bad = (d.layer1 || []).filter(e => ["UNAVAILABLE", "INVALID", "PARTIAL", "NO_DATA"].includes(e.status)).length;
    const l2n = (d.layer2 || []).length;
    const sum = `<p class="sumline"><span class="chip ${withdrawn ? "red" : "green"}">${h(tr(`Đồng ý ${given}/${cs.length}`, `Consent ${given}/${cs.length}`))}${withdrawn ? " · " + h(tr("có rút", "withdrawn")) : ""}</span>
      <span class="chip ${l1bad ? "amber" : "green"}">${h(tr(`Lớp 1: ${l1ok}/${(d.layer1 || []).length} nguồn đã phản hồi`, `Layer 1: ${l1ok}/${(d.layer1 || []).length} sources answered`))}</span>
      <span class="chip ${l2n ? "blue" : "grey"}">${h(l2n ? tr(`Lớp 2: ${l2n} lần đề nghị`, `Layer 2: ${l2n} request round(s)`) : tr("Lớp 2: không dùng", "Layer 2: not used"))}</span></p>`;
    return `<section class="panel" aria-labelledby="con-h"><h2 id="con-h"><span class="step">5</span>${h(tr("Đồng ý & nguồn dữ liệu", "Consent & data sources"))}</h2>
      ${sum}<details class="srcs" ${withdrawn || l1bad || l2n ? "open" : ""}><summary>${h(tr("Xem chi tiết đồng ý và từng nguồn", "Show consent and source details"))}</summary>
      <h3>${h(tr("Đồng ý của khách hàng", "Customer consent"))}</h3><ul class="rows">${cons || `<li class="note">—</li>`}</ul>
      <h3>${h(tr("Dữ liệu tự động (Lớp 1 – sẵn có tại HLB & CIC)", "Automatic data (Layer 1 – HLB & CIC)"))}</h3><ul class="rows">${l1 || `<li class="note">${h(tr("Chưa lấy.", "Not retrieved yet."))}</li>`}</ul>
      <h3>${h(tr("Dữ liệu bổ sung theo đồng ý (Lớp 2)", "Extra data by consent (Layer 2)"))}</h3>
      <ul class="rows">${l2 || `<li class="note">${h(tr("Không dùng – chỉ đề nghị khi dữ liệu tự động chưa đủ, khách đồng ý riêng từng nguồn.", "Not used – only requested when automatic data is not enough, with consent per source."))}</li>`}</ul>
      </details>
      <p class="note">${h(tr("Mọi kết nối là mô phỏng. Mã gọi API, số lần thử và độ trễ ở tab Nhật ký & kỹ thuật.", "All connections are simulated. API call ids, attempts and latency are in the Audit & technical tab."))}</p>
    </section>`;
  }

  function historyCard(c) {
    const items = (c.decisions || []).map(d => `<li><span class="dot ${d.to_state === "APPROVED" ? "ok" : d.to_state === "REJECTED" || d.to_state === "DECLINED" ? "bad" : ""}" aria-hidden="true"></span>
      <span class="when">${fmt.dt(d.ts)}</span><span class="what"><b>${h(P("actor", d.actor_type, d.actor_type))}</b>${d.actor_type === "OFFICER" ? ` <span class="note">${h(d.actor_id)}</span>` : ""}
        · ${h(P("state", d.from_state, d.from_state || ""))} → <b>${h(P("state", d.to_state, d.to_state || ""))}</b>
        ${d.reason_code ? `<span class="code">${h(d.reason_code)}</span>` : ""}${d.note ? `<span class="note blk">${h(d.note)}</span>` : ""}</span></li>`).join("");
    return `<section class="panel" aria-labelledby="his-h"><h2 id="his-h"><span class="step">6</span>${h(tr("Lịch sử quyết định", "Decision history"))}</h2>
      <ol class="timeline">${items || `<li class="note">—</li>`}</ol>
      <p class="auditline">${h(tr(`Nhật ký kiểm toán: ${c.audit.length} sự kiện, chỉ ghi thêm (không sửa, không xóa) · bộ quy tắc ${(c.routing || {}).policy_version || ""}`, `Audit log: ${c.audit.length} events, append-only (no edits, no deletes) · rule set ${(c.routing || {}).policy_version || ""}`))}
        <button class="linkbtn" data-tab="audit">${h(tr("Xem nhật ký kiểm toán →", "View audit log →"))}</button></p>
    </section>`;
  }

  // ------------------------------------------------------------------ decision column
  function allowedCodes(c, action, limit) {
    const rc = c.reason_codes, el = c.eligibility, a = c.application;
    if (action === "reject") return Object.keys(rc.reject);
    if (action === "confirm_fraud") return Object.keys(rc.fraud);
    if (el.fraud_case) return ["AP-06"];
    if (a.kind === "LINE_REINSTATEMENT") return ["RI-01"];
    let codes = Object.keys(rc.approve).filter(k => k !== "RI-01" && k !== "AP-06");
    if (Number.isInteger(limit) && limit > (el.stp_capacity_vnd || 0)) codes = codes.filter(k => el.above_stp_reason_codes.includes(k));
    return codes;
  }
  const SUGGEST = { "R010-FLAG": "AP-03", "R010-CAPX": "AP-05", "R010-ALT": "AP-05", "R010-VER": "AP-02", "R010-THIN": "AP-01", "R010-FRD": "AP-06" };
  const parseVnd = (s) => { const d = String(s || "").replace(/[^\d]/g, ""); return d ? parseInt(d, 10) : NaN; };

  function validateForm(c) {
    const f = st.form, el = c.eligibility, au = el.authority, errs = {};
    if (f.action === "approve") {
      const lim = f.limit;
      if (!Number.isInteger(lim)) errs.limit = tr("Nhập hạn mức muốn duyệt.", "Enter the limit to approve.");
      else if (au.exclusive ? lim >= au.max_vnd : lim > au.max_vnd) errs.limit = tr(`Vượt thẩm quyền của bạn (tối đa ${fmt.vnd(au.max_vnd)}).`, `Above your authority (max ${fmt.vnd(au.max_vnd)}).`);
      else if (!el.max_approvable_vnd || lim < el.min_limit_vnd || lim > el.max_approvable_vnd) errs.limit = tr(`Phải trong khoảng ${fmt.vnd(el.min_limit_vnd)} – ${fmt.vnd(el.max_approvable_vnd || 0)}.`, `Must be between ${fmt.vnd(el.min_limit_vnd)} and ${fmt.vnd(el.max_approvable_vnd || 0)}.`);
      else if (lim % el.step_vnd) errs.limit = tr(`Làm tròn theo bước ${fmt.vnd(el.step_vnd)}.`, `Use steps of ${fmt.vnd(el.step_vnd)}.`);
    }
    if (!f.reason || !allowedCodes(c, f.action, f.limit).includes(f.reason)) errs.reason = tr("Chọn mã lý do phù hợp.", "Choose a matching reason code.");
    if (!(f.note || "").trim()) errs.note = tr("Ghi chú là bắt buộc (lưu vào nhật ký kiểm toán).", "A note is required (kept in the audit log).");
    return errs;
  }

  function decisionCard(c) {
    const a = c.application, pm = c.permissions, el = c.eligibility, rc = c.reason_codes;
    if (a.qa_status === "PENDING") {
      return `<section class="panel decide" aria-labelledby="dec-h"><h2 id="dec-h">${h(tr("Kiểm tra chất lượng quyết định tự động", "Quality check of an automatic approval"))}</h2>
        <p>${h(tr(`Hệ thống đã tự duyệt ${fmt.vnd(a.approved_limit_vnd)}. Xem số liệu bên trái và cho biết bạn có đồng ý không.`, `The engine approved ${fmt.vnd(a.approved_limit_vnd)}. Review the figures and say whether you agree.`))}</p>
        <p class="note">${h(tr("Không đồng ý sẽ mở rà soát hạn mức; hạn mức của khách không tự thay đổi.", "Disagreeing opens a line review; the customer's limit does not change by itself."))}</p>
        <label class="field">${h(tr("Ghi chú (bắt buộc)", "Note (required)"))}<textarea id="qa-note" rows="3"></textarea></label>
        <div class="actions"><button class="btn primary" data-qa="AGREE">${h(tr("Đồng ý", "Agree"))}</button><button class="btn ghost" data-qa="DISAGREE">${h(tr("Không đồng ý", "Disagree"))}</button></div></section>`;
    }
    if (!pm.can_review) {
      const who = String(a.decided_by || "");
      return `<section class="panel decide ro" aria-labelledby="dec-h"><h2 id="dec-h">${h(tr("Kết quả", "Outcome"))} ${stateChip(a.credit_state)}</h2>
        <dl class="kv">${a.approved_limit_vnd ? `<dt>${h(tr("Hạn mức duyệt", "Approved limit"))}</dt><dd>${fmt.vnd(a.approved_limit_vnd)}</dd>` : ""}
          <dt>${h(tr("Người quyết định", "Decided by"))}</dt><dd>${h(who.startsWith("OFFICER") ? tr("Chuyên viên ", "Officer ") + who.split(":")[1] : who ? P("actor", who, who) : "—")}</dd>
          ${a.decision_reason_code ? `<dt>${h(tr("Lý do", "Reason"))}</dt><dd>${h(P("code", a.decision_reason_code, a.decision_reason_code))} <span class="code">${h(a.decision_reason_code)}</span></dd>` : ""}
          ${a.qa_status ? `<dt>QA</dt><dd>${h(a.qa_status)} · ${h(a.qa_by || "")}</dd>` : ""}</dl>
        ${a.decision_note ? `<p class="note">“${h(a.decision_note)}”</p>` : ""}
        <p class="note">${h(tr("Chỉ quyết định được khi hồ sơ đang chờ chuyên viên.", "Decisions are only possible while the case waits for an officer."))}</p></section>`;
    }
    const f = st.form;
    if (!f.action) {
      f.action = el.max_approvable_vnd ? "approve" : "reject";
      const mx = el.max_approvable_vnd;
      f.limit = mx ? Math.floor(Math.min(mx, a.requested_limit_vnd || mx) / el.step_vnd) * el.step_vnd : NaN;
      f.note = "";
    }
    const codes = allowedCodes(c, f.action, f.limit);
    if (!codes.includes(f.reason)) f.reason = codes.includes(SUGGEST[a.reason_code]) ? SUGGEST[a.reason_code] : (f.action === "reject" && a.reason_code === "R010-FLAG" ? "RJ-03" : codes[0]);
    const errs = st.showErrs ? validateForm(c) : {};
    const failed = (el.checks || []).filter(x => !x.passed);
    const elig = el.max_approvable_vnd
      ? `<p class="okline"><span class="ico ok" aria-hidden="true">✓</span>${h(tr(`Đủ điều kiện duyệt (${el.checks.length}/${el.checks.length} điều kiện)`, `Eligible to approve (${el.checks.length}/${el.checks.length} conditions)`))}</p>`
      : `<p class="okline bad"><span class="ico bad" aria-hidden="true">✕</span>${h(tr("Chưa duyệt được: ", "Not approvable: "))}${h(failed.length ? P("el", failed[0].id, failed[0].label) : tr("mức tối đa thấp hơn hạn mức tối thiểu", "maximum below the minimum"))}</p>`;
    const opt = (k, group) => `<option value="${h(k)}" ${f.reason === k ? "selected" : ""}>${h(k)} · ${h(P("code", k, rc[group][k]))}</option>`;
    const group = f.action === "reject" ? "reject" : f.action === "confirm_fraud" ? "fraud" : "approve";
    const above = f.action === "approve" && Number.isInteger(f.limit) && f.limit > (el.stp_capacity_vnd || 0) && !el.fraud_case;
    const btnLabel = f.action === "approve" ? tr("Duyệt hạn mức", "Approve limit") + (Number.isInteger(f.limit) ? " " + fmt.vnd(f.limit) : "")
      : f.action === "reject" ? tr("Từ chối hồ sơ", "Reject case") : tr("Xác nhận gian lận", "Confirm fraud");
    const quick = [];
    if (el.max_approvable_vnd) quick.push([el.max_approvable_vnd, tr("Tối đa", "Maximum")]);
    if (el.stp_capacity_vnd && el.stp_capacity_vnd >= el.min_limit_vnd && el.stp_capacity_vnd < (el.max_approvable_vnd || 0)) quick.push([el.stp_capacity_vnd, tr("Mức tự động", "Automatic")]);
    if (a.requested_limit_vnd && a.requested_limit_vnd % el.step_vnd === 0 && a.requested_limit_vnd >= el.min_limit_vnd && a.requested_limit_vnd < (el.max_approvable_vnd || 0)) quick.push([a.requested_limit_vnd, tr("Theo đề nghị", "Requested")]);
    return `<section class="panel decide" aria-labelledby="dec-h"><h2 id="dec-h">${h(tr("Quyết định của bạn", "Your decision"))}</h2>
      ${elig}
      <details class="elig"><summary>${h(tr("Xem các điều kiện duyệt", "Show approval conditions"))}</summary><ul class="checks tight">${(el.checks || []).map(x => `<li class="ck ck-${x.passed ? "PASS" : "FAIL"}"><span class="ico" aria-hidden="true">${x.passed ? "✓" : "✕"}</span><span class="txt">${h(P("el", x.id, x.label))} <span class="code" title="${h(x.label + (x.detail ? " – " + x.detail : ""))}">${h(x.id)}</span></span></li>`).join("")}</ul></details>
      <p class="authority">${h(tr("Thẩm quyền của bạn:", "Your authority:"))} <b>${el.authority.exclusive ? "&lt;" : "≤"} ${fmt.vnd(el.authority.max_vnd)}</b>${el.max_approvable_vnd ? ` · ${h(tr("khoảng được duyệt", "allowed range"))} <b>${fmt.vnd(el.min_limit_vnd)} – ${fmt.vnd(el.max_approvable_vnd)}</b> (${h(tr("bước", "step"))} ${fmt.vnd(el.step_vnd)})` : ""}
        ${el.capacity_above_authority ? `<span class="note blk">${h(tr("Khả năng trả nợ cao hơn thẩm quyền nên thẩm quyền là giới hạn.", "Capacity is above your authority, so the authority caps the limit."))}</span>` : ""}</p>
      <fieldset class="seg" role="radiogroup" aria-label="${h(tr("Hành động", "Action"))}">
        <label class="${f.action === "approve" ? "on" : ""} ${el.max_approvable_vnd ? "" : "dis"}"><input type="radio" name="act" value="approve" ${f.action === "approve" ? "checked" : ""} ${el.max_approvable_vnd ? "" : "disabled"}>${h(tr("Duyệt", "Approve"))}</label>
        <label class="${f.action === "reject" ? "on" : ""}"><input type="radio" name="act" value="reject" ${f.action === "reject" ? "checked" : ""}>${h(tr("Từ chối", "Reject"))}</label>
        ${el.fraud_case ? `<label class="${f.action === "confirm_fraud" ? "on" : ""}"><input type="radio" name="act" value="confirm_fraud" ${f.action === "confirm_fraud" ? "checked" : ""}>${h(tr("Gian lận", "Fraud"))}</label>` : ""}
      </fieldset>
      ${f.action === "approve" ? `<label class="field">${h(tr("Hạn mức duyệt (VND)", "Limit to approve (VND)"))}
          <input id="lim" inputmode="numeric" autocomplete="off" value="${Number.isInteger(f.limit) ? fmt.num(f.limit) : ""}" aria-invalid="${!!errs.limit}" aria-describedby="lim-e lim-h"></label>
        ${quick.length ? `<div class="quick">${quick.map(([v, l]) => `<button type="button" class="chipbtn" data-lim="${v}">${h(l)} · ${short(v)}</button>`).join("")}</div>` : ""}
        <p class="err" id="lim-e" role="alert">${h(errs.limit || "")}</p>
        <p class="note" id="lim-h">${above ? h(tr(`Cao hơn hạn mức tự động (${fmt.vnd(el.stp_capacity_vnd)}): chỉ dùng mã ${el.above_stp_reason_codes.join(" / ")}.`, `Above the automatic limit (${fmt.vnd(el.stp_capacity_vnd)}): only ${el.above_stp_reason_codes.join(" / ")} apply.`))
          : (a.requested_limit_vnd && Number.isInteger(f.limit) && f.limit < a.requested_limit_vnd ? h(tr("Thấp hơn mức khách đề nghị: khách sẽ nhận đề nghị hạn mức thấp hơn.", "Below the requested limit: the customer receives a lower-limit offer.")) : "")}</p>` : ""}
      <label class="field">${h(tr("Mã lý do", "Reason code"))}<select id="rc" aria-invalid="${!!errs.reason}" aria-describedby="rc-e">${codes.map(k => opt(k, group)).join("")}</select></label>
      <p class="err" id="rc-e" role="alert">${h(errs.reason || "")}</p>
      <label class="field">${h(tr("Ghi chú cho hồ sơ (bắt buộc)", "Case note (required)"))}<textarea id="note" rows="3" placeholder="${h(tr("Bằng chứng bạn dựa vào để quyết định…", "The evidence you relied on…"))}" aria-invalid="${!!errs.note}" aria-describedby="note-e">${h(f.note || "")}</textarea></label>
      <p class="err" id="note-e" role="alert">${h(errs.note || "")}</p>
      <button class="btn ${f.action === "approve" ? "primary" : "danger"} wide" id="submit">${h(btnLabel)}</button>
      <p class="note">${h(tr(`Quyết định được ghi vào nhật ký kiểm toán và thông báo cho khách. Phiên bản hồ sơ v${a.version}.`, `The decision is written to the audit log and the customer is notified. Case version v${a.version}.`))}</p>
    </section>`;
  }

  // ------------------------------------------------------------------ audit & technical tab (compliance / monitoring)
  function capacityPanel(cap) {
    if (!cap || !cap.parameters) return `<div class="panel"><h2>${h(tr("Tính toán khả năng trả nợ", "Repayment-capacity calculation"))}</h2><div class="note">—</div></div>`;
    const p = cap.parameters, inc = cap.income || {}, ob = cap.obligations || {}, l1 = inc.layer1, l2 = inc.layer2;
    const sz = (x) => x ? `${fmt.vnd(x.income_vnd)} → headroom <span class="${x.monthly_headroom_vnd > 0 ? "" : "res-FAIL"}">${fmt.vnd(x.monthly_headroom_vnd)}</span> → raw ${fmt.vnd(x.raw_limit_vnd)} (${h(x.binding_constraint)})` : "—";
    const cm = (c) => c ? `${h(c.method)}${c.factor !== undefined ? " × " + c.factor : ""}` : "";
    return `<div class="panel"><h2>${h(tr("Tính toán khả năng trả nợ", "Repayment-capacity calculation"))} <span class="chip amber">${h(p.source)} · ${h(p.approval_status)}</span></h2>
      <table class="t"><tr><th>Income</th><th>Average</th><th>Stressed (P25 month)</th><th>Note</th></tr>
        <tr><td>Layer 1 · HLB${l1 && l1.inflow_classified ? " (classified)" : ""}</td><td>${l1 ? fmt.vnd(l1.average_monthly_vnd) : "—"}</td><td>${l1 ? fmt.vnd(l1.conservative_monthly_vnd) : "—"}</td>
          <td class="note">${l1 ? cm(l1.conservative_method) + (l1.classification && l1.classification.classified ? ` · excluded ${fmt.vnd(l1.classification.excluded_total_vnd)} (${h(Object.keys(l1.classification.excluded).join(", "))})` : "") : "not usable"}</td></tr>
        <tr><td>Layer 2 · ${h((inc.source_ids || []).join(", ") || "none")}</td><td>${l2 ? fmt.vnd(l2.average_monthly_vnd) : "—"}</td><td>${l2 ? fmt.vnd(l2.conservative_monthly_vnd) : "—"}</td><td class="note">${l2 ? "summed after de-duplication (AV-07)" : ""}</td></tr></table>
      <table class="t" style="margin-top:8px"><tr><th>Obligations (VND / month)</th><th>Lineage</th><th>Amount</th></tr>
        ${(ob.components || []).map(c => `<tr><td>${h(c.component === "EXTERNAL" ? "External" : "HLB " + (c.product || ""))}</td><td>${h(c.lineage || "")}${c.dti_ratio !== undefined ? " · DTI " + c.dti_ratio : ""}<div class="note">${h(c.note || "")}</div></td><td>${fmt.vnd(c.monthly_vnd)}${c.component === "HLB_INTERNAL" && !c.added ? " (dedup)" : ""}</td></tr>`).join("")}
        <tr><td><b>Total</b></td><td>${h(ob.basis || "")}${ob.reason ? " · " + h(ob.reason) : ""}</td><td><b>${ob.known ? fmt.vnd(ob.monthly_vnd) : "unknown"}</b></td></tr>
        ${(ob.notes || []).map(n => `<tr><td colspan="3" class="note">${h(n)}</td></tr>`).join("")}</table>
      ${cap.computable ? `<table class="t" style="margin-top:8px"><tr><td>Automatic (STP) on stressed income</td><td>${sz(cap.stp)}</td><td><b>${cap.stp ? fmt.vnd(cap.stp.limit_vnd) : "—"}</b>${cap.stp && !cap.stp.meets_minimum ? ' <span class="res-FAIL">below minimum</span>' : ""}</td></tr>
        <tr><td>Officer maximum on average income (${h(cap.officer_basis || "")})</td><td>${sz(cap.officer)}</td><td><b>${cap.officer_max_limit_vnd ? fmt.vnd(cap.officer_max_limit_vnd) : "—"}</b>${cap.verified_obligation_breach ? ' <span class="res-FAIL">obligations exceed ceiling</span>' : ""}</td></tr>
        <tr><td>Ceilings</td><td colspan="2" class="note">DTI ${p.max_total_dti} · ${p.income_multiplier_cap}× recognised income · product ${fmt.vnd(p.product_max_limit_vnd)} · automatic ceiling ${fmt.vnd(p.stp_max_limit_vnd)} · digital-lending headroom ${p.aggregate_headroom_vnd === null ? "n/a" : fmt.vnd(p.aggregate_headroom_vnd)}</td></tr></table>`
        : `<div class="res-WARN" style="margin-top:8px">Not computable: ${h(cap.reason)}</div>`}
      <div class="formula" style="margin-top:8px">${h(cap.formula || "")}</div></div>`;
  }
  function apiLogPanel(c) {
    const d = c.data_acquisition || { layer1: [], layer2: [] };
    const l1 = (d.layer1 || []).map(e => `<tr><td>${h(e.label)}</td><td>${h(e.provider)}<div class="note">${h(e.legal_basis)}</div></td><td>${h(e.status)}</td><td>${h(e.summary)}</td><td class="mono">${h(e.request_id)}<div class="note">${e.attempts} attempt(s) · ${e.latency_ms} ms</div></td></tr>`).join("");
    const l2 = (d.layer2 || []).map((r, i) => (r.results || []).map(x => `<tr><td>L2 #${i + 1} · ${h(x.label)}</td><td>${h(r.requested_by || "")}<div class="note">consent: ${Object.entries(r.consents || {}).map(([k, v]) => `${h(k)} ${v ? "✓" : "✗"}`).join(", ")}</div></td><td>${h(x.outcome)}</td>
      <td>${x.validation ? x.validation.checks.map(v => `<div class="${v.passed ? "res-PASS" : "res-FAIL"}">${v.passed ? "✓" : "✗"} ${h(v.id)} ${h(v.label)}</div>`).join("") : (x.response && x.response.error ? h(x.response.error.message) : "—")}</td>
      <td class="mono">${h((x.response || {}).request_id || "")}<div class="note">${((x.response || {}).attempts || []).length} attempt(s) · simulated</div></td></tr>`).join("")).join("");
    return `<div class="panel"><h2>${h(tr("Nhật ký gọi API dữ liệu (mô phỏng)", "Data API call log (simulated)"))}</h2>
      <table class="t"><tr><th>Source</th><th>Provider · basis</th><th>Status</th><th>Result / validation</th><th>Call</th></tr>${l1}${l2}</table>
      <details style="margin-top:8px"><summary class="note">${h(tr("Nguồn đã đánh giá và loại trừ", "Sources assessed and excluded"))}</summary>${(d.excluded_sources || []).map(x => `<div class="note"><b>${h(x.label)}</b>: ${h(x.reason)}</div>`).join("")}<div class="note">${h(d.note || "")}</div></details></div>`;
  }
  function tracePanel(c) {
    const rt = c.routing || {}, cs = c.case_summary || {}, idn = rt.identity || {};
    return `<div class="panel"><h2>${h(tr("Dấu vết quyết định", "Decision trace"))}</h2>
      <dl class="kv tech"><dt>Band · reason</dt><dd>${h(rt.band || "")} · <span class="mono">${h(rt.reason_code || "")}</span></dd>
        <dt>Secondary codes</dt><dd class="mono">${h((rt.secondary_codes || []).join(", ") || "—")}</dd>
        <dt>Rule ids</dt><dd class="mono">${h((rt.rule_ids || []).join(", ") || "—")}</dd>
        <dt>Route · decided by</dt><dd class="mono">${h(rt.route || "")} · ${h(rt.decided_by || "")}</dd>
        <dt>Policy</dt><dd class="mono">${h(rt.policy_version || "")} · ${h(String(rt.policy_hash || "").slice(0, 16))}…</dd>
        <dt>Engine time</dt><dd>${rt.engine_ms !== undefined ? h(rt.engine_ms) + " ms" : "—"}</dd></dl>
      ${(idn.checks || []).length ? `<h3>Layer 0 · identity & fraud</h3><table class="t">${idn.checks.map(x => `<tr><td class="mono">${h(x.id)}</td><td>${h(x.label)}</td><td class="res-${x.result === "PASS" ? "PASS" : "WARN"}">${h(x.result)}</td></tr>`).join("")}</table>` : ""}
      <h3>${h(tr("Bằng chứng & nguồn gốc dữ liệu", "Evidence & lineage"))}</h3>
      <table class="t"><tr><th>Field</th><th>Value</th><th>Source</th><th>Verified</th><th>Observed</th></tr>
        ${(cs.evidence || []).map(e => `<tr><td class="mono">${h(e.field)}</td><td>${h(typeof e.value === "number" && e.value > 1000 ? fmt.vnd(e.value) : e.value)}</td><td class="mono">${h(e.source)} · L${h(e.layer)}</td><td class="${e.verified ? "res-PASS" : "res-WARN"}">${e.verified ? "yes" : "no"}${e.excluded ? ` · excluded (${h(typeof e.excluded === "string" ? e.excluded : "DQ")})` : ""}</td><td>${h(e.observed_at || "—")}</td></tr>`).join("")}</table>
      <h3>${h(tr("Dòng thu nhập", "Income streams"))}</h3>
      <table class="t"><tr><th>Stream</th><th>Lineage</th><th>Average</th><th>Stressed</th><th>Automatic use</th></tr>
        ${(cs.income_streams || []).map(x => `<tr><td>${h(x.stream)}${x.dedup && x.dedup.length ? `<div class="note">${h(x.dedup.join("; "))}</div>` : ""}</td><td class="mono">${h(x.lineage)}${x.factor != null ? " · factor " + h(x.factor) : ""}${x.multiplier_factor != null ? " · multiple ×" + h(x.multiplier_factor) : ""}</td><td>${fmt.vnd(x.average_vnd)}</td><td>${fmt.vnd(x.stressed_vnd)}</td><td>${x.stp_usable ? "yes" : "officer only"}</td></tr>`).join("") || '<tr><td colspan="5" class="note">—</td></tr>'}</table>
      <p class="note"><b>Risk indicators:</b> ${(cs.risk_indicators || []).map(r => h(r.rule_id) + (r.info_only ? " (info)" : "")).join(", ") || "—"} · <b>Triggered:</b> ${h((cs.triggered_rules || []).filter(Boolean).join(", "))}</p></div>`;
  }
  function auditTab(c) {
    const ev = c.audit || [];
    return `<div class="panel"><h2>${h(tr("Nhật ký kiểm toán", "Audit log"))} <span class="count">${ev.length}</span> <span class="chip grey">${h(tr("chỉ ghi thêm", "append-only"))}</span></h2>
        <p class="note">${h(tr("Cơ sở dữ liệu chặn mọi thao tác sửa/xóa sự kiện. Đặt lại dữ liệu demo vẫn giữ nhật ký.", "The database blocks any update or delete of events. A demo reset keeps the log."))}</p>
        <table class="t audit"><thead><tr><th>#</th><th>${h(tr("Thời gian", "Time"))}</th><th>${h(tr("Ai", "Actor"))}</th><th>${h(tr("Sự kiện", "Event"))}</th><th>${h(tr("Chi tiết", "Details"))}</th></tr></thead><tbody>
        ${ev.map(e => `<tr><td class="mono">${e.event_id}</td><td>${fmt.dt(e.ts)}</td><td>${h(P("actor", e.actor_type, e.actor_type))}<div class="note mono">${h(e.actor_id || "")}</div></td>
          <td><b>${h(P("event", e.event_type, e.event_type))}</b><div class="note mono">${h(e.event_type)}${e.from_state || e.to_state ? " · " + h(e.from_state || "") + " → " + h(e.to_state || "") : ""}</div>${(e.rule_ids || []).length ? `<div class="note mono">${h(e.rule_ids.join(", "))}</div>` : ""}</td>
          <td><details><summary class="note">${h(tr("Xem", "View"))}</summary><pre class="json">${h(JSON.stringify(e.details, null, 2))}</pre></details></td></tr>`).join("")}</tbody></table></div>
      ${tracePanel(c)}${capacityPanel(c.capacity)}${apiLogPanel(c)}
      ${c.governance ? `<details class="panel" open><summary><b>${h(tr("Quản trị: bản mẫu và sản phẩm thật", "Governance: prototype vs production"))}</b></summary><div class="note" style="margin-top:6px">${h(c.governance.prototype)}</div>
        <ul class="note">${c.governance.production_required.map(x => `<li>${h(x)}</li>`).join("")}</ul><div class="note">${h(c.governance.unverified_claim)}</div></details>` : ""}`;
  }

  // ------------------------------------------------------------------ render
  function renderCase(c) {
    const a = c.application, cust = c.profile.customer, ap = c.profile.applicant || {};
    const live = cust.derived_from === "LIVE_INPUT";
    const due = a.sla_due_at ? (new Date(a.sla_due_at) - Date.now()) / 36e5 : null;
    const sla = a.credit_state !== "MANUAL_REVIEW" || due === null ? "" : c.sla_breached ? `<span class="chip red">${h(tr("Quá hạn SLA", "SLA breached"))}</span>`
      : `<span class="note">${h(tr(`Còn ${Math.max(0, Math.round(due))} giờ theo SLA`, `${Math.max(0, Math.round(due))}h left on SLA`))}</span>`;
    const el = c.eligibility || {}, rt = c.routing || {}, au = el.authority || {};
    const band = a.band || rt.band;
    const strip = `<div class="case-strip" role="group" aria-label="${h(tr("Tóm tắt hồ sơ", "Case summary"))}">
        <div class="cs tone-${tone(a.reason_code)}"><span class="k">${h(tr("Đánh giá của hệ thống", "Engine assessment"))}</span>
          <span class="v">${band ? `<span class="chip ${band === "GREEN" ? "green" : band === "RED" ? "red" : "amber"}">${h(band)}</span> ` : ""}${h(P("reason", a.reason_code, c.reason_label || a.reason_code || "—"))}</span>
          <span class="s"><span class="code">${h(a.reason_code || "—")}</span>${(rt.secondary_codes || []).length ? ` + ${h(rt.secondary_codes.join(", "))}` : ""}</span></div>
        <div class="cs hi"><span class="k">${h(tr("Tối đa bạn được duyệt", "Most you can approve"))}</span><span class="v">${el.max_approvable_vnd ? fmt.vnd(el.max_approvable_vnd) : `<span class="bad">${h(tr("Chưa duyệt được", "Not approvable"))}</span>`}</span>
          <span class="s">${h(el.max_approvable_vnd ? tr("theo thu nhập bình quân đã xác minh", "on verified average income") : tr("thu nhập chưa đủ cho mức tối thiểu", "income does not reach the minimum"))}</span></div>
        <div class="cs"><span class="k">${h(tr("Hạn mức tự động", "Automatic limit"))}</span><span class="v">${el.stp_capacity_vnd ? fmt.vnd(el.stp_capacity_vnd) : "—"}</span><span class="s">${h(tr("tháng thu nhập thấp", "low-income month"))}</span></div>
        <div class="cs"><span class="k">${h(tr("Khách đề nghị", "Requested"))}</span><span class="v">${a.requested_limit_vnd ? fmt.vnd(a.requested_limit_vnd) : h(tr("Không nêu", "Not stated"))}</span><span class="s">${h(a.requested_limit_vnd ? "" : tr("hạn mức theo khả năng trả nợ", "limit follows capacity"))}</span></div>
        <div class="cs"><span class="k">${h(tr("Thẩm quyền của bạn", "Your authority"))}</span><span class="v">${au.max_vnd ? (au.exclusive ? "&lt; " : "≤ ") + fmt.vnd(au.max_vnd) : "—"}</span><span class="s">${h(tr("giả định tình huống", "case assumption"))}</span></div>
      </div>`;
    $("case").innerHTML = `<div class="panel case-head"><div class="who">
          <h1>${h(cust.display_name)}</h1>
          <p class="note">${h(segLabel(ap.customer_profile))} · ${h(P("kind", a.kind, a.kind))}${a.appeal_count ? " · " + h(tr("Khiếu nại", "Appeal")) : ""}</p>
          <p class="note dim">${h(a.application_id)} · ${h(cust.customer_id)} · ${h(live ? tr("hồ sơ giả định nhập trực tiếp khi trình diễn", "hypothetical customer typed in during the pitch") : tr(`hồ sơ giả lập từ dòng dữ liệu ${cust.derived_from}`, `fictional, from dataset row ${cust.derived_from}`))}</p></div>
        <div class="st">${stateChip(a.credit_state)}${sla}</div>${strip}</div>
      <div class="tabs" role="tablist" aria-label="${h(tr("Chế độ xem hồ sơ", "Case views"))}">
        <button role="tab" id="t-review" aria-controls="p-review" aria-selected="${st.tab === "review"}" data-tab="review">${h(tr("Thẩm định & quyết định", "Review & decide"))}</button>
        <button role="tab" id="t-audit" aria-controls="p-audit" aria-selected="${st.tab === "audit"}" data-tab="audit">${h(tr("Nhật ký & kỹ thuật", "Audit & technical"))} <span class="count">${c.audit.length}</span></button></div>
      <div role="tabpanel" id="p-review" aria-labelledby="t-review" ${st.tab === "review" ? "" : "hidden"}>
        <div class="review-grid"><div class="col-main">${whyCard(c)}${figuresCard(c)}${signalsCard(c)}${checksCard(c)}${consentCard(c)}${historyCard(c)}</div>
          <div class="col-side" id="decide">${decisionCard(c)}</div></div></div>
      <div role="tabpanel" id="p-audit" aria-labelledby="t-audit" ${st.tab === "audit" ? "" : "hidden"}>${st.tab === "audit" ? auditTab(c) : ""}</div>`;
    wire(c);
  }

  function rerenderDecision(c, focusId) {
    const el = $("decide"); if (!el) return;
    el.innerHTML = decisionCard(c); wireDecision(c);
    if (focusId) { const f = $(focusId); if (f) { f.focus(); if (f.setSelectionRange && f.value) f.setSelectionRange(f.value.length, f.value.length); } }
  }

  function wire(c) {
    $("case").querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { st.tab = b.dataset.tab; renderCase(c); const t = $("t-" + st.tab); if (t) t.focus(); });
    const tl = $("case").querySelector("[role=tablist]");
    if (tl) tl.onkeydown = (e) => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { st.tab = st.tab === "review" ? "audit" : "review"; renderCase(c); $("t-" + st.tab).focus(); } };
    wireDecision(c);
  }

  function wireDecision(c) {
    const root = $("decide"); if (!root) return;
    root.querySelectorAll("[data-qa]").forEach(b => b.onclick = async () => {
      const note = ($("qa-note").value || "").trim(); if (!note) return flash(tr("Ghi chú là bắt buộc.", "A note is required."), true);
      try { const r = await call("POST", `/api/officer/cases/${c.application.application_id}/qa`, { outcome: b.dataset.qa, note }); st.case = r; renderCase(r); loadQueue(true); flash(tr("Đã ghi nhận kiểm tra chất lượng.", "QA recorded.")); }
      catch (e) { flash(e.message, true); }
    });
    root.querySelectorAll("input[name=act]").forEach(r => r.onchange = () => { st.form.action = r.value; st.form.reason = null; st.showErrs = false; rerenderDecision(c); });
    const lim = $("lim");
    if (lim) {
      lim.oninput = () => { st.form.limit = parseVnd(lim.value); };  // codes + hints refresh on change/Enter, so typing stays smooth
      lim.onchange = () => { st.form.limit = parseVnd(lim.value); rerenderDecision(c); };
      lim.onkeydown = (e) => { if (e.key === "Enter") { st.form.limit = parseVnd(lim.value); rerenderDecision(c, "lim"); } };
    }
    root.querySelectorAll("[data-lim]").forEach(b => b.onclick = () => { st.form.limit = +b.dataset.lim; rerenderDecision(c); });
    const rc = $("rc"); if (rc) rc.onchange = () => { st.form.reason = rc.value; };
    const note = $("note"); if (note) note.oninput = () => { st.form.note = note.value; };
    const sub = $("submit"); if (sub) sub.onclick = () => decide(c);
  }

  async function decide(c) {
    const a = c.application, f = st.form;
    if ($("lim")) f.limit = parseVnd($("lim").value);
    if ($("rc")) f.reason = $("rc").value;
    if ($("note")) f.note = $("note").value;
    st.showErrs = true;
    const errs = validateForm(c);
    if (Object.keys(errs).length) { rerenderDecision(c, errs.limit ? "lim" : errs.reason ? "rc" : "note"); return; }
    const what = f.action === "approve" ? tr(`Duyệt hạn mức ${fmt.vnd(f.limit)} cho ${c.profile.customer.display_name}?`, `Approve a limit of ${fmt.vnd(f.limit)} for ${c.profile.customer.display_name}?`)
      : f.action === "reject" ? tr(`Từ chối hồ sơ của ${c.profile.customer.display_name}?`, `Reject ${c.profile.customer.display_name}'s case?`)
      : tr(`Xác nhận gian lận? Hạn mức sẽ bị dừng (HS-05).`, `Confirm fraud? The line will be stopped (HS-05).`);
    if (!await ask(what + " (" + f.reason + ")", tr("Xác nhận", "Confirm"), tr("Quay lại", "Back"))) return;
    try {
      const r = await call("POST", `/api/officer/cases/${a.application_id}/decision`, { action: f.action, reason_code: f.reason, note: f.note.trim(),
        expected_version: a.version, approved_limit_vnd: f.action === "approve" ? f.limit : null });
      flash(tr("Đã lưu quyết định và thông báo cho khách.", "Decision saved; customer notified."));
      st.form = {}; st.showErrs = false; st.case = r; renderCase(r); loadQueue(true);
      try { new BroadcastChannel("smart-credit-demo").postMessage({ type: "refresh" }); } catch (e) { /* older browsers */ }
    } catch (e) { flash(e.message, true); st.form = {}; loadCase(a.application_id); }
  }

  init();
  setInterval(() => { if (st.token) loadQueue(false); }, 5000);
})();
