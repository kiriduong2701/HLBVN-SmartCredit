"""EMVCo Merchant-Presented QR (MPM) parser and builder, VietQR/NAPAS profile.

Structure follows the public EMVCo MPM specification as used by VietQR:
  00 payload format indicator ("01")    01 point of initiation (11 static, 12 dynamic)
  38 merchant account information (NAPAS): 00 GUID "A000000727",
     01 beneficiary organisation (00 acquirer BIN, 01 merchant account), 02 service code
  52 MCC   53 currency ("704" = VND)   54 amount   58 country ("VN")
  59 merchant name   60 merchant city   62 additional data (01 bill number, 05 reference label)
  63 CRC-16/CCITT-FALSE over the payload up to and including "6304".

What this proves and what it does not:
  * The CRC detects corruption/edits of the payload; it is NOT a signature.
  * Authenticity of the amount comes from matching the bill number against the
    merchant's order record (simulated merchant-order API, PROPOSED integration).
  * BINs, merchant accounts and orders in the demo are fictional. No NAPAS,
    acquirer or merchant system is contacted.
Pure functions; no I/O.
"""
from typing import Any, Dict, List, Tuple

NAPAS_GUID = "A000000727"


class QRError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return "%04X" % crc


def _tlv(tag: str, value: str) -> str:
    if len(value) > 99:
        raise ValueError("TLV value too long for tag %s" % tag)
    return "%s%02d%s" % (tag, len(value), value)


def parse_tlv(s: str) -> List[Tuple[str, str]]:
    out, i = [], 0
    while i < len(s):
        if i + 4 > len(s):
            raise QRError("QR_MALFORMED", "Truncated QR data.")
        tag, ln = s[i:i + 2], s[i + 2:i + 4]
        if not (tag.isdigit() and ln.isdigit()):
            raise QRError("QR_MALFORMED", "Invalid QR field header.")
        n = int(ln)
        val = s[i + 4:i + 4 + n]
        if len(val) != n:
            raise QRError("QR_MALFORMED", "Truncated QR field %s." % tag)
        out.append((tag, val))
        i += 4 + n
    return out


def build(acquirer_bin: str, merchant_account: str, mcc: str, name: str, city: str,
          amount_vnd: int = None, bill_number: str = None) -> str:
    """Build a VietQR-style payload (dynamic when an amount is given)."""
    acct = _tlv("00", NAPAS_GUID) + _tlv("01", _tlv("00", acquirer_bin) + _tlv("01", merchant_account)) + _tlv("02", "QRPUSH")
    body = _tlv("00", "01") + _tlv("01", "12" if amount_vnd else "11") + _tlv("38", acct) + _tlv("52", mcc) + _tlv("53", "704")
    if amount_vnd:
        body += _tlv("54", str(int(amount_vnd)))
    body += _tlv("58", "VN") + _tlv("59", name[:25]) + _tlv("60", city[:15])
    if bill_number:
        body += _tlv("62", _tlv("01", bill_number))
    body += "6304"
    return body + crc16_ccitt(body)


def parse(payload: str) -> Dict[str, Any]:
    p = (payload or "").strip()
    if len(p) < 20 or not p.startswith("000201"):
        raise QRError("QR_UNREADABLE", "Not an EMVCo merchant QR (payload format indicator missing).")
    if p[-8:-4] != "6304":
        raise QRError("QR_MALFORMED", "QR checksum field missing.")
    if crc16_ccitt(p[:-4]) != p[-4:].upper():
        raise QRError("QR_CRC_MISMATCH", "QR checksum does not match: the code was altered or misread.")
    fields = dict(parse_tlv(p[:-8]))  # everything before the "6304" + CRC tail
    out: Dict[str, Any] = {"format": fields.get("00"), "initiation": "DYNAMIC" if fields.get("01") == "12" else "STATIC",
                           "mcc": fields.get("52"), "currency": fields.get("53"), "country": fields.get("58"),
                           "merchant_name": fields.get("59"), "merchant_city": fields.get("60"), "amount_vnd": None,
                           "bill_number": None, "acquirer_bin": None, "merchant_account": None, "scheme": None}
    acct = fields.get("38")
    if acct:
        sub = dict(parse_tlv(acct))
        out["scheme"] = "NAPAS_VIETQR" if sub.get("00") == NAPAS_GUID else "OTHER"
        if sub.get("01"):
            bo = dict(parse_tlv(sub["01"]))
            out["acquirer_bin"], out["merchant_account"] = bo.get("00"), bo.get("01")
        out["service_code"] = sub.get("02")
    if out["scheme"] != "NAPAS_VIETQR" or not out["acquirer_bin"] or not out["merchant_account"]:
        raise QRError("QR_UNSUPPORTED", "Only VietQR (NAPAS) merchant QR codes are supported.")
    if out["currency"] != "704" or out["country"] != "VN":
        raise QRError("QR_UNSUPPORTED", "Only VND payments in Vietnam are supported.")
    if fields.get("54"):
        if not fields["54"].isdigit() or int(fields["54"]) <= 0:
            raise QRError("QR_MALFORMED", "Invalid amount in QR.")
        out["amount_vnd"] = int(fields["54"])
    if fields.get("62"):
        add = dict(parse_tlv(fields["62"]))
        out["bill_number"] = add.get("01")
        out["reference_label"] = add.get("05")
    return out
