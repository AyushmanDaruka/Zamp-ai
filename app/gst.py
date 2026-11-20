"""India GST helpers: GSTIN checksum validation, state codes, tax-type rules.

All deterministic. These are the checks an AP clerk does by eye (or forgets to do).
"""
import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "32": "Kerala", "33": "Tamil Nadu", "36": "Telangana", "37": "Andhra Pradesh",
}


def gstin_checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        val = _CHARS.index(ch)
        prod = val * (2 if i % 2 else 1)
        total += prod // 36 + prod % 36
    return _CHARS[(36 - total % 36) % 36]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + gstin_checksum_char(base)


def normalize_gstin(g):
    if not g:
        return None
    return re.sub(r"[^0-9A-Z]", "", str(g).upper()) or None


def validate_gstin(g):
    """Returns (ok, reason)."""
    g = normalize_gstin(g)
    if not g:
        return False, "GSTIN missing"
    if len(g) != 15:
        return False, f"GSTIN has {len(g)} characters (must be 15)"
    if not GSTIN_RE.match(g):
        return False, "GSTIN format invalid (expected 2-digit state + PAN + entity + 'Z' + checksum)"
    if g[:2] not in STATE_CODES:
        return False, f"Unknown state code {g[:2]}"
    expected = gstin_checksum_char(g[:14])
    if g[14] != expected:
        return False, f"GSTIN checksum fails (last char {g[14]}, expected {expected}) — likely typo or fabricated"
    return True, f"Valid GSTIN · {STATE_CODES[g[:2]]} · PAN {g[2:12]}"


def state_of(gstin):
    g = normalize_gstin(gstin)
    return g[:2] if g and len(g) >= 2 else None


def expected_tax_type(vendor_gstin, buyer_gstin):
    """Intra-state supply → CGST+SGST. Inter-state → IGST."""
    vs, bs = state_of(vendor_gstin), state_of(buyer_gstin)
    if not vs or not bs:
        return None
    return "CGST+SGST" if vs == bs else "IGST"
