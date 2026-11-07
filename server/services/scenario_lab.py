"""Presenter page, sections 1 and 2: the Customer Persona Showcase and the Live Scenario Simulator.

Customer Persona Showcase
    Exactly five fixed, read-only reference profiles - one per customer segment of the supplied 10,000-row dataset. Every
    engine input is copied verbatim from a named dataset row (DATASET_ROWS below; tests/test_scenario_lab.py re-reads
    data/reference/hlbvn_dataset.xlsx and fails if a single value differs). Display names are either the fictional name
    the row already carries in the demo fixtures, or a new FICTIONAL demo alias - never a real identity. No age, gender,
    occupation, income or credit attribute that is absent from the dataset is added. The only scenario-specific (simulated)
    elements are stated per persona: the partner merchant (the dataset has no merchant field), the purchase amount (taken
    from the row's requested_loan_vnd), and the simulated business date.

Live Scenario Simulator
    Runs the ACTUAL engine for any set of inputs, in a throw-away sandbox: a fresh in-memory service built from the same
    policy file and fixtures, so the real code path runs end to end (create customer -> scan dynamic QR -> Pay Later with
    the legal data step -> Layer 0 -> Layer 1 -> readiness -> capacity -> routing -> checkout rules; and, when the engine asks
    for Layer 2 data, the "customer shares" and "customer declines" branches). Nothing is scripted and nothing is written to
    the demo database, the reference personas or the dataset. The documented `expected` pathway of each persona is only
    compared against the live result; it never feeds the result.

Nothing in this module changes a threshold, formula, route, reason code or policy parameter.
"""
import copy
import time
from typing import Any, Dict, List, Optional

from ..store.db import now_iso, open_database
from .base import ServiceError, _j
from .live_demo import FIELDS, LIVE_MERCHANTS, SEGMENTS

DATASET_FILE = "data/reference/hlbvn_dataset.xlsx"
# Pre-assessment columns: the only dataset columns the engine may read (decision_rules.md: outcome columns are never inputs).
ENGINE_INPUT_FIELDS = ["customer_profile", "has_formal_cic_history", "has_observable_tx", "monthly_inflow_vnd", "inflow_cv",
                       "unified_credit_score", "casa_balance_vnd", "dti_ratio", "on_time_bill_ratio", "e_wallet_topups_pm",
                       "regular_weekly_pattern", "rapid_drain_flag"]
APPLICATION_FIELDS = ["application_date", "requested_loan_vnd", "loan_tenure_months"]
OUTCOME_FIELDS = ["approved_loan_vnd", "interest_rate_pct", "is_default_90d", "manual_approval_status", "manual_decision_days",
                  "manual_review_cost_vnd", "is_abandoned", "stp_approval_status", "stp_decision_minutes", "stp_review_cost_vnd"]
# Failure switches that change an ASSESSMENT (payment/authentication switches only act later, at payment time).
ASSESSMENT_SWITCHES = ("core_system_unavailable", "cic_unavailable", "alt_data_api_unavailable", "alt_data_incomplete",
                       "device_anomaly")

# ---------------------------------------------------------------------------------------------------------------- dataset
# Verbatim copies of the dataset rows used below (application_date converted from the Excel serial to ISO).
DATASET_ROWS: Dict[str, Dict[str, Any]] = {
    "HLB_POP_100000005": {"application_date": "2026-04-01", "customer_profile": "Salaried, with credit history",
                          "has_formal_cic_history": 1, "has_observable_tx": 1, "monthly_inflow_vnd": 16969073, "inflow_cv": 0.0681,
                          "unified_credit_score": 681, "casa_balance_vnd": 7096492, "dti_ratio": 0.3145, "on_time_bill_ratio": 0.9715,
                          "e_wallet_topups_pm": 6, "regular_weekly_pattern": 1, "rapid_drain_flag": 0, "requested_loan_vnd": 20969073,
                          "approved_loan_vnd": 20969073, "loan_tenure_months": 12, "interest_rate_pct": 16.3, "is_default_90d": 0,
                          "manual_approval_status": 1, "manual_decision_days": 2.07, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 1, "stp_decision_minutes": 1.43, "stp_review_cost_vnd": 12000},
    "HLB_POP_100000160": {"application_date": "2026-04-02", "customer_profile": "Salaried, no credit history",
                          "has_formal_cic_history": 0, "has_observable_tx": 1, "monthly_inflow_vnd": 11724186, "inflow_cv": 0.1288,
                          "unified_credit_score": 530, "casa_balance_vnd": 16503159, "dti_ratio": 0.2798, "on_time_bill_ratio": 0.8438,
                          "e_wallet_topups_pm": 9, "regular_weekly_pattern": 0, "rapid_drain_flag": 0, "requested_loan_vnd": 20543855,
                          "approved_loan_vnd": 20543855, "loan_tenure_months": 6, "interest_rate_pct": 20.65, "is_default_90d": 0,
                          "manual_approval_status": 1, "manual_decision_days": 3.46, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 1, "stp_decision_minutes": 2.33, "stp_review_cost_vnd": 12000},
    "HLB_POP_100001384": {"application_date": "2026-04-13", "customer_profile": "First-time borrowers, digitally active",
                          "has_formal_cic_history": 0, "has_observable_tx": 0, "monthly_inflow_vnd": 10331332, "inflow_cv": 0.288,
                          "unified_credit_score": 479, "casa_balance_vnd": 5556626, "dti_ratio": 0.2512, "on_time_bill_ratio": 0.7022,
                          "e_wallet_topups_pm": 9, "regular_weekly_pattern": 1, "rapid_drain_flag": 0, "requested_loan_vnd": 2170246,
                          "approved_loan_vnd": 0, "loan_tenure_months": 3, "interest_rate_pct": 29.81, "is_default_90d": 0,
                          "manual_approval_status": 0, "manual_decision_days": 2.91, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 0, "stp_decision_minutes": 2.5, "stp_review_cost_vnd": 12000},
    "HLB_POP_100004375": {"application_date": "2026-05-10", "customer_profile": "Online merchants (MSME)",
                          "has_formal_cic_history": 0, "has_observable_tx": 1, "monthly_inflow_vnd": 25405285, "inflow_cv": 0.3306,
                          "unified_credit_score": 616, "casa_balance_vnd": 8289103, "dti_ratio": 0.2572, "on_time_bill_ratio": 0.897,
                          "e_wallet_topups_pm": 8, "regular_weekly_pattern": 1, "rapid_drain_flag": 0, "requested_loan_vnd": 57764188,
                          "approved_loan_vnd": 0, "loan_tenure_months": 6, "interest_rate_pct": 24.17, "is_default_90d": 0,
                          "manual_approval_status": 0, "manual_decision_days": 4.03, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 0, "stp_decision_minutes": 1.36, "stp_review_cost_vnd": 12000},
    "HLB_POP_100000020": {"application_date": "2026-04-01", "customer_profile": "Gig / platform workers",
                          "has_formal_cic_history": 0, "has_observable_tx": 1, "monthly_inflow_vnd": 14621915, "inflow_cv": 0.5842,
                          "unified_credit_score": 423, "casa_balance_vnd": 1697966, "dti_ratio": 0.2601, "on_time_bill_ratio": 0.5873,
                          "e_wallet_topups_pm": 16, "regular_weekly_pattern": 0, "rapid_drain_flag": 0, "requested_loan_vnd": 10266529,
                          "approved_loan_vnd": 0, "loan_tenure_months": 9, "interest_rate_pct": 29.71, "is_default_90d": 0,
                          "manual_approval_status": 0, "manual_decision_days": 3.47, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 1, "stp_decision_minutes": 2.2, "stp_review_cost_vnd": 12000},
    # Starting numbers for failure scenario F5 only (not a persona).
    "HLB_POP_100001351": {"application_date": "2026-04-13", "customer_profile": "Salaried, with credit history",
                          "has_formal_cic_history": 1, "has_observable_tx": 1, "monthly_inflow_vnd": 61673103, "inflow_cv": 0.0585,
                          "unified_credit_score": 658, "casa_balance_vnd": 83743412, "dti_ratio": 0.2758, "on_time_bill_ratio": 0.9642,
                          "e_wallet_topups_pm": 3, "regular_weekly_pattern": 1, "rapid_drain_flag": 0, "requested_loan_vnd": 41103123,
                          "approved_loan_vnd": 41103123, "loan_tenure_months": 18, "interest_rate_pct": 16.07, "is_default_90d": 0,
                          "manual_approval_status": 1, "manual_decision_days": 1.92, "manual_review_cost_vnd": 380000,
                          "is_abandoned": 0, "stp_approval_status": 1, "stp_decision_minutes": 2.66, "stp_review_cost_vnd": 12000},
}

SEGMENT_REPLAY_SOURCE = "docs/test_logs/segment_replay.json (scripts/segment_replay.py, policy 2026.11-demo.6, 10,000 rows)"

# ---------------------------------------------------------------------------------------------------------------- personas
PERSONAS: List[Dict[str, Any]] = [
    {"id": "P1", "applicant_id": "HLB_POP_100000005", "display_name": "Tạ Mỹ Kim Tiên",
     "name_source": {"kind": "RETAINED_FIXTURE_NAME", "customer_id": "CUS-001"},
     "merchant_id": "M-ABC",
     "scenario": {"vi": "Tự động duyệt · Trả sau toàn bộ", "en": "Automatic approval · Full Pay Later"},
     "pathway": {"vi": "Hồ sơ đầy đủ và đã xác minh → hệ thống tự duyệt hạn mức theo khả năng trả nợ → toàn bộ đơn hàng được trả sau.",
                 "en": "Complete, verified profile → the engine approves a capacity-based limit automatically → the whole purchase goes on Pay Later."},
     "why": {"vi": "Đại diện cho nhóm đông nhất (35,5%) và lộ trình phổ biến nhất của nhóm: có lịch sử CIC, 6 tháng giao dịch tại HLB, lương rất ổn định (biến động 6,8%), điểm 681, 97% hóa đơn trả đúng hạn, nghĩa vụ nợ 31% thu nhập. Đây là mốc chuẩn của luồng xử lý thẳng (STP).",
             "en": "Represents the largest segment (35.5%) and its most common pathway: CIC history, 6 months of HLB transactions, very stable salary (6.8% variability), score 681, 97% of bills paid on time, debt payments at 31% of income. It is the straight-through-processing baseline."},
     "expected": {"band": "GREEN", "reason_code": "R001", "checkout_reason_code": "R001", "next_action": "PAY_LATER_FULL"},
     "segment_share": {"pct": 91.4, "label": {"vi": "nhóm này được tự động duyệt (GREEN)", "en": "of this segment is approved automatically (GREEN)"}},
     "data_notes": []},
    {"id": "P2", "applicant_id": "HLB_POP_100000160", "display_name": "Nguyễn Hoài An",
     "name_source": {"kind": "FICTIONAL_ALIAS"},
     "merchant_id": "M-SLA",
     "scenario": {"vi": "Tự động duyệt · Không có CIC", "en": "Automatic approval · No CIC history"},
     "pathway": {"vi": "Không có hồ sơ CIC nhưng có dòng lương đã xác minh tại HLB → hệ thống vẫn tự duyệt dựa trên giao dịch; thiếu CIC không bao giờ là lý do từ chối.",
                 "en": "No CIC record but verified salary inflows at HLB → the engine still approves on transaction evidence; missing CIC is never a decline reason."},
     "why": {"vi": "Chứng minh khả năng tiếp cận tín dụng cho người chưa có lịch sử vay: lương ổn định (biến động 12,9%), 84% hóa đơn đúng hạn, nghĩa vụ nợ 28%. Hạn mức bị giới hạn bởi bội số thu nhập và vẫn đủ cho toàn bộ đơn hàng.",
             "en": "Shows inclusion of a customer with no borrowing history: stable salary (12.9% variability), 84% of bills on time, debt payments at 28%. The limit is set by the income multiple and still covers the whole purchase."},
     "expected": {"band": "GREEN", "reason_code": "R001", "checkout_reason_code": "R001", "next_action": "PAY_LATER_FULL"},
     "segment_share": {"pct": 77.8, "label": {"vi": "nhóm này được tự động duyệt (GREEN)", "en": "of this segment is approved automatically (GREEN)"}},
     "data_notes": []},
    {"id": "P3", "applicant_id": "HLB_POP_100001384", "display_name": "Phan Khánh Linh",
     "name_source": {"kind": "FICTIONAL_ALIAS"},
     "merchant_id": "M-FPL",
     "scenario": {"vi": "Chờ bổ sung dữ liệu · Khách chọn chia sẻ", "en": "Evidence needed · Customer chooses to share"},
     "pathway": {"vi": "Không CIC, không giao dịch tại HLB → hệ thống không thể xác minh khả năng trả nợ, nên đề nghị khách chia sẻ thu nhập ở ngân hàng khác (Open Banking) và hóa đơn tiện ích, từng nguồn một. Chia sẻ → tự duyệt; từ chối → RED có quyền khiếu nại.",
                 "en": "No CIC, no HLB transactions → the engine cannot verify repayment capacity, so it asks the customer to share other-bank income (Open Banking) and utility bills, source by source. Shares → automatic approval; declines → RED with one appeal."},
     "why": {"vi": "Nhóm gần như vô hình với ngân hàng (Exhibit 1). Đây là lộ trình phổ biến nhất của nhóm (51% hồ sơ không có giao dịch quan sát được). Hồ sơ gần trung vị của nhóm nhất trong số các hồ sơ sạch có cùng lộ trình.",
             "en": "The segment that is almost invisible to the bank (Exhibit 1). This is the segment's most common pathway (51% of its rows have no observable transactions). The record is the closest to the segment median among clean rows with this pathway."},
     "replaces": {"candidate": "HLB_POP_100008470",
                  "why": {"vi": "Ứng viên ban đầu có cùng lộ trình nhưng cột kết quả mâu thuẫn: is_default_90d = 1 trong khi khoản vay chưa từng được duyệt (approved_loan_vnd = 0), là 1 trong 436 dòng như vậy.",
                          "en": "The shortlisted record has the same pathway but contradictory outcome columns: is_default_90d = 1 on a loan that was never approved (approved_loan_vnd = 0), one of 436 such rows."}},
     "expected": {"band": "AMBER", "reason_code": "R010-EVD", "checkout_reason_code": "R010-PENDING", "next_action": "SHARE_DATA",
                  "after_share": {"band": "GREEN", "reason_code": "R001-L2"}, "after_decline": {"band": "RED", "reason_code": "R020-EVD"}},
     "segment_share": {"pct": 51.2, "label": {"vi": "nhóm này được đề nghị chia sẻ thêm dữ liệu (R010-EVD)", "en": "of this segment is asked to share more data (R010-EVD)"}},
     "data_notes": [{"vi": "Bộ dữ liệu vẫn ghi thu nhập 10.331.332 ₫ dù không có giao dịch quan sát được (1 trong 1.943 dòng như vậy). Hệ thống không coi đó là thu nhập HLB đã xác minh; trong mô phỏng, con số này chỉ trở thành bản ghi Open Banking giả lập nếu khách đồng ý chia sẻ.",
                     "en": "The dataset still records an inflow of VND 10,331,332 although no transactions are observable (one of 1,943 such rows). The engine does not treat it as verified HLB income; in the simulation it becomes a simulated Open Banking record only if the customer agrees to share."}]},
    {"id": "P4", "applicant_id": "HLB_POP_100004375", "display_name": "Lâm Quốc Bình",
     "name_source": {"kind": "FICTIONAL_ALIAS"},
     "merchant_id": "M-SHF",
     "scenario": {"vi": "Tự động duyệt · Thanh toán tách phần", "en": "Automatic approval · Split tender"},
     "pathway": {"vi": "Dòng tiền bán hàng đã xác minh → tự duyệt hạn mức; đơn hàng lớn hơn hạn mức → khách được chọn trả sau phần trong hạn mức và trả ngay phần còn lại (thanh toán tách phần).",
                 "en": "Verified merchant inflows → automatic limit; the purchase is larger than the limit → the customer can put the in-limit part on Pay Later and pay the rest now (split tender)."},
     "why": {"vi": "Đại diện lộ trình phổ biến nhất của nhóm MSME (57,3% đơn hàng được tách phần). Doanh thu chưa phân loại chỉ được ghi nhận 30% khi tính bội số thu nhập, nên hạn mức thận trọng hơn doanh thu gộp. Hồ sơ gần trung vị của nhóm nhất trong số các hồ sơ sạch có cùng lộ trình.",
             "en": "Represents the MSME segment's most common pathway (57.3% of purchases split). Unclassified merchant turnover counts at 30% in the income multiple, so the limit is more prudent than gross turnover. The record is the closest to the segment median among clean rows with this pathway."},
     "replaces": {"candidate": "HLB_POP_100003105",
                  "why": {"vi": "Ứng viên ban đầu có cùng lộ trình nhưng cột kết quả mâu thuẫn: hồ sơ bị bỏ dở (is_abandoned = 1) mà vẫn ghi đã duyệt 54.689.276 ₫, là 1 trong 1.161 dòng như vậy.",
                          "en": "The shortlisted record has the same pathway but contradictory outcome columns: the application was abandoned (is_abandoned = 1) yet records an approval of VND 54,689,276, one of 1,161 such rows."}},
     "expected": {"band": "GREEN", "reason_code": "R001", "checkout_reason_code": "R010-SPLIT", "next_action": "SPLIT_TENDER"},
     "segment_share": {"pct": 57.3, "label": {"vi": "đơn hàng của nhóm này được tách phần", "en": "of this segment's purchases are split"}},
     "data_notes": []},
    {"id": "P5", "applicant_id": "HLB_POP_100000020", "display_name": "Huỳnh Gia Bảo",
     "name_source": {"kind": "RETAINED_FIXTURE_NAME", "customer_id": "CUS-002"},
     "merchant_id": "M-ABC",
     "scenario": {"vi": "Chuyển chuyên viên thẩm định", "en": "Referred to a credit officer"},
     "pathway": {"vi": "Hai dấu hiệu rủi ro (điểm 423 < 436; hóa đơn đúng hạn 58,7% < 66,3%) và thu nhập biến động mạnh → không tự từ chối; chuyển chuyên viên với bản tóm tắt hồ sơ. Tại quầy, khách trả ngay và có thể chuyển trả góp trong 7 ngày nếu được duyệt.",
                 "en": "Two risk flags (score 423 < 436; bills on time 58.7% < 66.3%) and volatile income → never auto-declined; referred to a credit officer with a case summary. At the counter the customer pays now and can convert to instalments within 7 days if approved."},
     "why": {"vi": "Đại diện lộ trình phổ biến nhất của nhóm gig (42,6% chuyển chuyên viên) – nhóm có tỷ lệ duyệt thấp nhất hiện nay dù được nghi là có khả năng trả nợ (Exhibit 1). Cho thấy ranh giới giữa tự động và phán đoán của con người.",
             "en": "Represents the gig segment's most common pathway (42.6% referred to an officer) – the segment with the lowest approval rate today despite suspected repayment ability (Exhibit 1). Shows where automation stops and human judgement starts."},
     "expected": {"band": "AMBER", "reason_code": "R010-FLAG", "checkout_reason_code": "R010-PENDING", "next_action": "OFFICER_REVIEW"},
     "segment_share": {"pct": 42.6, "label": {"vi": "nhóm này được chuyển chuyên viên", "en": "of this segment is referred to an officer"}},
     "data_notes": []},
]

# ---------------------------------------------------------------------------------------------------------------- failure scenarios
# Simulator shortcuts. Each starts from a persona (or one dataset row) and applies the stated modifications; the engine then
# decides as usual. `expected` is documentation, checked by tests - never an input.
FAILURE_SCENARIOS: List[Dict[str, Any]] = [
    {"id": "F1", "base": {"persona": "P1"}, "modifications": {"dti_ratio": 1.0},
     "title": {"vi": "Nghĩa vụ nợ đã xác minh vượt khả năng trả", "en": "Verified debt payments exceed capacity"},
     "demonstrates": {"vi": "Nợ hiện có bằng 100% thu nhập: hệ thống từ chối ngay (RED) vì không thể đóng khoảng thiếu, khách được khiếu nại một lần.",
                      "en": "Existing debt payments equal 100% of income: the engine declines at once (RED) because the gap cannot be closed; one appeal."},
     "expected": {"band": "RED", "reason_code": "R020-CAP", "next_action": "DECLINED_APPEAL"}},
    {"id": "F2", "base": {"persona": "P1"}, "modifications": {"dti_ratio": 0.75},
     "title": {"vi": "Gánh nặng nợ cao (75% thu nhập)", "en": "Heavy debt burden (75% of income)"},
     "demonstrates": {"vi": "Khoảng thiếu có thể đóng bằng thu nhập khác → hệ thống đề nghị chia sẻ dữ liệu trước; nếu không đóng được → RED.",
                      "en": "The gap might be closed by other income → the engine asks for more data first; if it stays open → RED."},
     "expected": {"band": "AMBER", "reason_code": "R010-CAPX", "next_action": "SHARE_DATA", "after_decline": {"band": "RED", "reason_code": "R020-CAP"}}},
    {"id": "F3", "base": {"persona": "P2"}, "modifications": {"rapid_drain_flag": 1},
     "title": {"vi": "Rút sạch tiền ngay sau khi nạp", "en": "Funds drained right after top-up"},
     "demonstrates": {"vi": "Một dấu hiệu hành vi bất thường biến hồ sơ GREEN thành ca chuyên viên thẩm định – không tự từ chối.",
                      "en": "One behavioural red flag turns a GREEN profile into an officer case – never an automatic decline."},
     "expected": {"band": "AMBER", "reason_code": "R010-FLAG", "next_action": "OFFICER_REVIEW"}},
    {"id": "F4", "base": {"persona": "P1"}, "modifications": {"purchase_amount_vnd": 1500000},
     "title": {"vi": "Đơn hàng dưới 1,6 triệu", "en": "Purchase below VND 1.6M"},
     "demonstrates": {"vi": "Ngoài phạm vi sản phẩm: Trả sau không áp dụng (N/A), khách trả ngay. Không phải quyết định tín dụng.",
                      "en": "Outside product scope: Pay Later does not apply (N/A) and the customer pays now. Not a credit decision."},
     "expected": {"checkout_reason_code": "R030-AMT", "next_action": "PAY_NOW_ONLY_SCOPE"}},
    {"id": "F5", "base": {"dataset_row": "HLB_POP_100001351"}, "modifications": {"purchase_amount_vnd": 95000000},
     "display_name": "Khách hàng giả định F5", "merchant_id": "M-SHF",
     "title": {"vi": "Đơn hàng vượt trần 90,1 triệu", "en": "Purchase above the VND 90.1M cap"},
     "demonstrates": {"vi": "Thu nhập cao: hạn mức dừng đúng ở 90.100.000 ₫; phần vượt trả ngay, không bao giờ được tài trợ.",
                      "en": "High income: the limit stops at exactly VND 90,100,000; the excess is paid now and never financed."},
     "expected": {"band": "GREEN", "reason_code": "R001", "checkout_reason_code": "R010-SPLIT", "next_action": "SPLIT_TENDER"}},
]

NEXT_ACTIONS = ("PAY_LATER_FULL", "SPLIT_TENDER", "COUNTER_OFFER", "OFFICER_REVIEW", "FRAUD_REVIEW", "SHARE_DATA", "AUTOMATIC_RETRY",
                "DECLINED_APPEAL", "POLICY_STOP", "PAY_NOW_ONLY_SCOPE", "PAY_NOW_FRAUD_HOLD", "PAY_NOW_SYSTEM_DOWN",
                "LINE_APPROVED_CONVERT", "PAY_NOW", "NOT_ASSESSED")


def _engine_inputs(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row[k] for k in ENGINE_INPUT_FIELDS}


def persona_inputs(p: Dict[str, Any]) -> Dict[str, Any]:
    """Simulator inputs for a persona: dataset engine inputs + purchase (= requested_loan_vnd) + merchant + name."""
    row = DATASET_ROWS[p["applicant_id"]]
    return dict(_engine_inputs(row), display_name=p["display_name"], purchase_amount_vnd=row["requested_loan_vnd"],
                merchant_id=p["merchant_id"])


def scenario_base_inputs(s: Dict[str, Any]) -> Dict[str, Any]:
    if "persona" in s["base"]:
        return persona_inputs(next(p for p in PERSONAS if p["id"] == s["base"]["persona"]))
    row = DATASET_ROWS[s["base"]["dataset_row"]]
    return dict(_engine_inputs(row), display_name=s.get("display_name", s["id"]), purchase_amount_vnd=row["requested_loan_vnd"],
                merchant_id=s.get("merchant_id", "M-ABC"))


def scenario_inputs(s: Dict[str, Any]) -> Dict[str, Any]:
    base = scenario_base_inputs(s)
    return dict(base, **s["modifications"])


def compare_expected(expected: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Compare a documented pathway with a live engine result. Returns {matches, differences}."""
    got = {"band": result["assessment"].get("band"), "reason_code": result["assessment"].get("reason_code"),
           "checkout_reason_code": result["checkout"].get("reason_code"), "next_action": result.get("next_action")}
    diffs = [{"field": k, "expected": v, "actual": got.get(k)} for k, v in expected.items()
             if k in got and got.get(k) != v]
    for branch in ("after_share", "after_decline"):
        if branch in expected:
            b = (result.get("branches") or {}).get(branch.split("_")[1]) or {}
            a = b.get("assessment") or {}
            for k, v in expected[branch].items():
                if a.get(k) != v:
                    diffs.append({"field": branch + "." + k, "expected": v, "actual": a.get(k)})
    return {"matches": not diffs, "differences": diffs}


class ScenarioLabMixin:
    # ------------------------------------------------------------------------------------------------ config
    def scenario_lab_config(self) -> Dict[str, Any]:
        merchants = {m["merchant_id"]: m for m in self.fixtures["merchants"]}
        return {
            "fields": FIELDS, "segments": SEGMENTS, "engine_input_fields": ENGINE_INPUT_FIELDS,
            "merchants": [{"merchant_id": i, "name": merchants[i]["name"], "category": merchants[i]["category"]} for i in LIVE_MERCHANTS],
            "product_scope": {"min_vnd": self.policy["affordability"]["min_limit_vnd"],
                              "max_vnd": self.policy["affordability"]["product_max_limit_vnd"]},
            "personas": [{"id": p["id"], "display_name": p["display_name"], "applicant_id": p["applicant_id"],
                          "segment": DATASET_ROWS[p["applicant_id"]]["customer_profile"], "scenario": p["scenario"],
                          "inputs": persona_inputs(p)} for p in PERSONAS],
            "failure_scenarios": [{"id": s["id"], "title": s["title"], "demonstrates": s["demonstrates"], "base": s["base"],
                                   "modifications": s["modifications"], "base_inputs": scenario_base_inputs(s),
                                   "inputs": scenario_inputs(s), "expected": s["expected"]} for s in FAILURE_SCENARIOS],
            "active_switches": [k for k in ASSESSMENT_SWITCHES if self.flag(k)],
            "business_date": self.as_of.isoformat(), "policy_version": self.policy["policy_version"],
        }

    # ------------------------------------------------------------------------------------------------ persona showcase
    def persona_showcase(self) -> Dict[str, Any]:
        """The five reference personas with their dataset facts and a fresh engine run (normal operating conditions: no
        failure switch applied, current simulated business date). Nothing is stored."""
        out = []
        for p in PERSONAS:
            row = DATASET_ROWS[p["applicant_id"]]
            inp = persona_inputs(p)
            res = self._simulate(inp, switches=[])
            out.append({
                "id": p["id"], "applicant_id": p["applicant_id"], "display_name": p["display_name"], "name_source": p["name_source"],
                "segment": row["customer_profile"], "scenario": p["scenario"], "pathway": p["pathway"], "why": p["why"],
                "replaces": p.get("replaces"), "data_notes": p["data_notes"],
                "segment_share": dict(p["segment_share"], source=SEGMENT_REPLAY_SOURCE),
                "dataset": {"engine_inputs": _engine_inputs(row), "application": {k: row[k] for k in APPLICATION_FIELDS},
                            "outcomes_not_used": {k: row[k] for k in OUTCOME_FIELDS}, "file": DATASET_FILE},
                "simulated": {"merchant_id": p["merchant_id"],
                              "merchant_name": next(m["name"] for m in self.fixtures["merchants"] if m["merchant_id"] == p["merchant_id"]),
                              "purchase_amount_vnd": row["requested_loan_vnd"], "purchase_source": "requested_loan_vnd",
                              "business_date": self.as_of.isoformat()},
                "inputs": inp, "expected": p["expected"], "engine": res, "verification": compare_expected(p["expected"], res),
            })
        return {"personas": out, "business_date": self.as_of.isoformat(), "policy_version": self.policy["policy_version"],
                "policy_hash": self.policy_hash, "checked_at": now_iso(),
                "conditions": "Normal operating conditions (no failure switch), current simulated business date."}

    # ------------------------------------------------------------------------------------------------ simulator
    def simulate_scenario(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        raw = body.get("inputs")
        if not isinstance(raw, dict):
            raise ServiceError(422, "INVALID_LIVE_INPUT", "Send the scenario as {\"inputs\": {...}}.", {"fields": {}})
        inp = self._validate_live(raw)  # same validation (and defaults) as the live customer journey
        apply = body.get("apply_failure_switches", True) is not False
        switches = [k for k in ASSESSMENT_SWITCHES if apply and self.flag(k)]
        res = self._simulate(inp, switches)
        res["derived"] = {
            "hlb_transactions": inp["has_observable_tx"] == 1,
            "inflow_role": "VERIFIED_HLB_INFLOW" if inp["has_observable_tx"] == 1 else "SIMULATED_OPEN_BANKING_RECORD",
            "bill_ratio_role": "HLB_RECORD" if inp["has_observable_tx"] == 1 else "SIMULATED_UTILITY_RECORD",
            "casa_defaulted": raw.get("casa_balance_vnd") in (None, ""),
            "ignored_without_hlb_transactions": [] if inp["has_observable_tx"] == 1 else
            ["inflow_cv", "casa_balance_vnd", "regular_weekly_pattern"],
        }
        return res

    def _simulate(self, inp: Dict[str, Any], switches: List[str]) -> Dict[str, Any]:
        started = time.perf_counter()
        first = self._sandbox_run(inp, switches, None)
        if first["data_request"]["requested"]:
            first["branches"] = {"share": self._sandbox_run(inp, switches, "share"),
                                 "decline": self._sandbox_run(inp, switches, "decline")}
        first.update(inputs_used=inp, failure_switches_applied=switches, business_date=self.as_of.isoformat(),
                     evaluated_at=now_iso(), total_ms=round((time.perf_counter() - started) * 1000, 1), sandbox=True,
                     persisted=False)
        return first

    def _sandbox(self, policy: Optional[Dict[str, Any]] = None):
        """A throw-away service on an in-memory database. `policy` (a modified copy) is only ever used by the risk-round
        demonstrations in risk_lab.py; the live service always runs the locked policy file."""
        from .smart_credit import SmartCreditService  # local import: smart_credit composes this mixin
        fixtures = copy.deepcopy(self.fixtures)
        policy = copy.deepcopy(policy if policy is not None else self.policy)
        sb = SmartCreditService(open_database(":memory:", fixtures), policy, fixtures, payment_delay_ms=0)
        sb.db.set_control("business_date", self.as_of.isoformat())
        return sb

    def _sandbox_run(self, inp: Dict[str, Any], switches: List[str], branch: Optional[str],
                     mutate: Optional[Any] = None, answers_override: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        """`mutate(sb, customer_id)` lets the risk round alter the SANDBOX customer record (missing, stale, conflicting
        or invalid data) before the journey starts; the engine then runs unchanged."""
        sb = self._sandbox()
        try:
            for k in switches:
                sb.db.set_control(k, "1")
            consents = {c["id"]: True for c in sb.policy["consent"]["activation_consents"]}
            cid = sb.create_live_customer(inp, "PRESENTER:simulator")["customer_id"]
            if mutate:
                mutate(sb, cid)
            error = None
            granted: List[str] = []
            try:
                qr = sb.list_qr_codes(cid)[0]
                tx_id = sb.resolve_qr(cid, qr["payload"])["view"]["transaction"]["transaction_id"]
                sb.choose_pay_later(tx_id, cid, consents)
                if branch:
                    app = sb.db.one("SELECT * FROM applications WHERE customer_id = ? ORDER BY rowid DESC LIMIT 1", (cid,))
                    offered = [s["id"] for s in (_j(app["info_request_json"], {}) or {}).get("sources", [])]
                    answers = {sid: branch == "share" for sid in offered}
                    if answers_override is not None:
                        answers = {sid: bool(answers_override.get(sid, False)) for sid in offered}
                    granted = [sid for sid in offered if answers[sid]]
                    sb.alternative_data_consent(app["application_id"], cid, answers)
            except ServiceError as e:
                error = {"code": e.code, "message": e.message}
            out = self._extract(sb, cid, error)
            if branch:
                out["branch"] = {"kind": branch, "sources_granted": granted}
            return out
        finally:
            sb.db.close()

    # ------------------------------------------------------------------------------------------------ result (read-only)
    def _extract(self, sb: Any, cid: str, error: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        app = sb.db.one("SELECT * FROM applications WHERE customer_id = ? ORDER BY rowid DESC LIMIT 1", (cid,))
        tx = sb.db.one("SELECT * FROM transactions WHERE customer_id = ? ORDER BY rowid DESC LIMIT 1", (cid,))
        rt = (_j(app["routing_json"], {}) or {}) if app else {}
        cap = (_j(app["capacity_json"], {}) or {}) if app else {}
        rd = (_j(app["readiness_json"], {}) or {}) if app else {}
        req = (_j(app["info_request_json"], {}) or {}) if app else {}
        acq = (_j(app["acquisition_json"], {}) or {}) if app else {}
        dec = (_j(tx["pl_decision_json"], {}) or {}) if tx else {}
        state = app["credit_state"] if app else None
        route = rt.get("route")
        stp, off = cap.get("stp") or {}, cap.get("officer") or {}
        assessment = {
            "application_id": app["application_id"] if app else None, "credit_state": state,
            "band": (app["band"] if app else None) or rt.get("band"), "reason_code": (app["reason_code"] if app else None) or rt.get("reason_code"),
            "secondary_codes": rt.get("secondary_codes") or [], "rule_ids": rt.get("rule_ids") or [],
            "reasons": [{"rule_id": r.get("rule_id"), "message": r.get("message")} for r in rt.get("reasons") or []],
            "route": route, "decided_by": app["decided_by"] if app else None, "customer_reason": rt.get("customer_reason"),
            "readiness_status": app["readiness_status"] if app else None,
            "fields_not_used": sorted((rd.get("fields_not_used") or {}).keys()) if isinstance(rd.get("fields_not_used"), dict)
            else list(rd.get("fields_not_used") or []),
            "layer0": [{"id": c.get("id"), "result": c.get("result")} for c in (rt.get("identity") or {}).get("checks", [])],
            "policy_version": rt.get("policy_version"), "policy_hash": rt.get("policy_hash"), "engine_ms": rt.get("engine_ms"),
        }
        limit = {
            "approved_limit_vnd": (app["approved_limit_vnd"] if app else None) or rt.get("approved_limit_vnd"),
            "automatic_capacity_vnd": cap.get("limit_vnd"), "officer_max_limit_vnd": cap.get("officer_max_limit_vnd"),
            "binding_constraint": cap.get("binding_constraint"), "basis_for_limit": cap.get("basis_for_limit"),
            "computable": cap.get("computable"), "meets_minimum": cap.get("meets_minimum"),
            "recognised_income_vnd": stp.get("income_vnd"), "average_income_vnd": off.get("income_vnd"),
            "income_basis": (cap.get("income") or {}).get("basis"),
            "obligations_vnd": (cap.get("obligations") or {}).get("monthly_vnd"),
            "debt_service_budget_vnd": stp.get("monthly_debt_service_budget_vnd"), "monthly_headroom_vnd": stp.get("monthly_headroom_vnd"),
            "limit_by_capacity_vnd": stp.get("limit_by_capacity_vnd"), "limit_by_income_multiplier_vnd": stp.get("limit_by_income_multiplier_vnd"),
            "indicative_limit_vnd": rt.get("indicative_limit_vnd"),
        }
        checkout = {
            "transaction_id": tx["transaction_id"] if tx else None, "state": tx["state"] if tx else None,
            "amount_vnd": tx["amount_vnd"] if tx else None, "band": dec.get("band"), "reason_code": dec.get("reason_code"),
            "rule_id": dec.get("rule_id"), "message": dec.get("message"), "decision_class": dec.get("decision_class"),
            "credit_outcome": dec.get("credit_outcome"), "payment_options": dec.get("payment_options") or [],
            "split": dec.get("split"), "checks": [{"id": c.get("id"), "label": c.get("label"), "result": c.get("result"),
                                                   "detail": c.get("detail")} for c in dec.get("checks") or []],
        }
        referred = state == "MANUAL_REVIEW" or route in ("HUMAN_REVIEW", "FRAUD_REVIEW")
        data_req = state == "MORE_INFORMATION_REQUIRED" and req.get("route") == "ALTERNATIVE_DATA"
        rounds = acq.get("layer2") or []
        readiness = {
            "status": rd.get("status"),
            "validation_errors": [{"rule_id": e.get("rule_id"), "field": e.get("field"), "message": e.get("message")}
                                  for e in rd.get("validation_errors") or []],
            "verification_warnings": [{"rule_id": e.get("rule_id"), "field": e.get("field"), "message": e.get("message")}
                                      for e in rd.get("verification_warnings") or []],
            "data_quality_events": [{"rule_id": e.get("rule_id"), "field": e.get("field"), "message": e.get("message"),
                                     "action": e.get("action")} for e in rd.get("data_quality_events") or []],
            "missing_fields": list(rd.get("missing_fields") or []),
            "unavailable_sources": list(rd.get("unavailable_sources") or []),
            "evidence_paths_met": list(rd.get("evidence_paths_met") or []),
            "checks_not_passed": [{"check_id": c.get("check_id"), "name": c.get("name"), "result": c.get("result"),
                                   "detail": c.get("detail")} for c in rd.get("checks_performed") or [] if c.get("result") != "PASS"],
        }
        result = {
            "assessment": assessment, "limit": limit, "checkout": checkout, "readiness": readiness,
            "officer_referral": {"referred": referred, "route": route if referred else None,
                                 "reasons": [r["message"] for r in assessment["reasons"]] if referred else []},
            "data_request": {"requested": data_req,
                             "sources": [{"id": s.get("id"), "label": s.get("label")} for s in req.get("sources", [])] if data_req else [],
                             "missing": [m.get("item") for m in req.get("missing", [])] if data_req else []},
            "layer2_results": [{"source": r.get("source") or r.get("id"), "outcome": r.get("outcome")}
                               for r in (rounds[-1].get("results") or [])] if rounds else [],
            "error": error,
        }
        result["next_action"] = self._next_action(result)
        return result

    @staticmethod
    def _next_action(r: Dict[str, Any]) -> str:
        """Plain label for what happens next, read off the engine's own outputs (presentation only - decides nothing)."""
        a, c = r["assessment"], r["checkout"]
        if r.get("error") and not a.get("credit_state"):
            return "NOT_ASSESSED"
        if c.get("reason_code") in ("R030-AMT", "R030-MER"):
            return "PAY_NOW_ONLY_SCOPE"
        if c.get("decision_class") == "FRAUD_HOLD":
            return "PAY_NOW_FRAUD_HOLD"
        st, route = a.get("credit_state"), a.get("route")
        if st == "POLICY_STOP":
            return "POLICY_STOP"
        if st == "DECLINED":
            return "DECLINED_APPEAL"
        if st == "MANUAL_REVIEW":
            return "FRAUD_REVIEW" if route == "FRAUD_REVIEW" else "OFFICER_REVIEW"
        if r["data_request"]["requested"]:
            return "SHARE_DATA"
        if st == "ASSESSMENT_PENDING" and route == "SYSTEM_RETRY":
            return "AUTOMATIC_RETRY"
        if st == "OFFER_PENDING":
            return "COUNTER_OFFER"
        if st == "APPROVED":
            if c.get("reason_code") == "R010-SPLIT":
                return "SPLIT_TENDER"
            if c.get("band") == "GREEN":
                return "PAY_LATER_FULL"
            if c.get("reason_code") == "R010-API":
                return "PAY_NOW_SYSTEM_DOWN"
            if c.get("reason_code") == "R010-PENDING":
                return "LINE_APPROVED_CONVERT"  # line approved after the counter decision (e.g. after Layer 2)
            return "PAY_NOW"
        if c.get("reason_code") == "R010-API":
            return "PAY_NOW_SYSTEM_DOWN"
        return "NOT_ASSESSED" if not st else "PAY_NOW"
