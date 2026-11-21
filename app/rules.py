"""Stages 3–7: deterministic checks + decision. No LLM in here — every outcome is traceable to a rule.

Each check returns {id, label, status: pass|warn|fail|info, detail}.
Each problem additionally raises an Issue that the decision engine ranks:
    REJECT  → the document itself is wrong; vendor must re-issue. We email the vendor.
    HOLD    → something needs an internal human (security, procurement, AP). Routed with a specific question.
    PARTIAL → part of the invoice is supportable; pay that, hold the rest.
    WARN    → noted on the approval, doesn't stop it.
"""
import re
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from app import master as M
from app.extract import norm_id
from app.gst import STATE_CODES, expected_tax_type, normalize_gstin, state_of, validate_gstin

P = M.POLICY
TODAY = date(2026, 11, 20)  # fixed "business date" so demo results are reproducible; swap for date.today() in prod


def chk(cid, label, status, detail, **extra):
    return {"id": cid, "label": label, "status": status, "detail": detail, **extra}


def issue(code, outcome, owner, message, vendor_ask=None, internal_ask=None, exposure=0.0):
    return {"code": code, "outcome": outcome, "owner": owner, "message": message,
            "vendor_ask": vendor_ask, "internal_ask": internal_ask, "exposure_inr": round(exposure or 0, 2)}


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def inr(x):
    if x is None:
        return "—"
    neg = x < 0
    x = abs(round(x, 2))
    whole, frac = divmod(round(x * 100), 100)
    s = str(int(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-₹" if neg else "₹") + s + (f".{int(frac):02d}" if frac else "")


def q(x):
    return f"{x:,.0f}" if x is not None and float(x).is_integer() else f"{x:,}"


def close(a, b, tol=None):
    tol = P["rounding_tolerance_inr"] if tol is None else tol
    return a is not None and b is not None and abs(a - b) <= tol


# ───────────────────────── document integrity ─────────────────────────
def check_document(f):
    """Arithmetic self-consistency + mandatory fields. Catches both vendor typos and extraction errors."""
    checks, issues = [], []
    items = f["line_items"]
    incl = bool(f.get("rates_include_tax"))
    rate = f.get("gst_rate_percent") or 18.0

    if not f.get("invoice_number"):
        checks.append(chk("inv_no", "Invoice number present", "fail", "No invoice/bill number on the document. A GST tax invoice must carry a unique serial number (CGST Rule 46)."))
        issues.append(issue("MISSING_INVOICE_NUMBER", "REJECT", "Vendor",
                            "Invoice has no invoice number — it is not a valid GST tax invoice and cannot be booked or de-duplicated.",
                            vendor_ask="Please re-issue the invoice with a unique invoice number (CGST Rule 46)."))
    else:
        ok_len = len(f["invoice_number"]) <= 16
        checks.append(chk("inv_no", "Invoice number present", "pass" if ok_len else "warn",
                          f"{f['invoice_number']}" + ("" if ok_len else " — longer than the 16 characters GST allows")))

    d = parse_date(f.get("invoice_date"))
    if not d:
        checks.append(chk("inv_date", "Invoice date", "fail", "Invoice date missing or unreadable"))
        issues.append(issue("MISSING_DATE", "HOLD", "AP team", "Invoice date could not be read.", internal_ask="Confirm the invoice date from the original document."))
    elif d > TODAY:
        checks.append(chk("inv_date", "Invoice date", "fail", f"{d.isoformat()} is in the future"))
        issues.append(issue("FUTURE_DATE", "REJECT", "Vendor", f"Invoice is dated {d.isoformat()}, which is in the future.",
                            vendor_ask="Please re-issue the invoice with the correct invoice date."))
    else:
        age = (TODAY - d).days
        checks.append(chk("inv_date", "Invoice date", "pass" if age <= 180 else "warn", f"{d.strftime('%d %b %Y')} · {age} days old"))

    # line arithmetic
    bad_lines = []
    for i, li in enumerate(items):
        q, up, amt = li.get("quantity"), li.get("unit_price"), li.get("amount")
        if q is None or up is None or amt is None:
            continue
        expected = round(q * up, 2)
        if not close(expected, amt, max(1.0, expected * 0.001)):
            bad_lines.append((i, li, expected))
    if bad_lines:
        i, li, exp = bad_lines[0]
        over = li["amount"] - exp
        checks.append(chk("line_math", "Line maths (qty × rate = amount)", "fail",
                          f"Line {i + 1} '{li['description'][:40]}': {li['quantity']:g} × {inr(li['unit_price'])} = {inr(exp)}, but invoice says {inr(li['amount'])} ({'+' if over > 0 else ''}{inr(over)})"))
        issues.append(issue("LINE_ARITHMETIC", "REJECT", "Vendor",
                            f"Line {i + 1} amount does not equal quantity × rate (invoice {'overstated' if over > 0 else 'understated'} by {inr(abs(over))}).",
                            vendor_ask=f"Line {i + 1} ({li['description']}): {li['quantity']:g} × {inr(li['unit_price'])} = {inr(exp)}, but the invoice shows {inr(li['amount'])}. Please issue a corrected invoice.",
                            exposure=max(over, 0) * (1 + rate / 100)))
    elif items:
        checks.append(chk("line_math", "Line maths (qty × rate = amount)", "pass", f"All {len(items)} lines reconcile"))

    lines_sum = round(sum(li["amount"] or 0 for li in items), 2)
    tax = round((f.get("cgst") or 0) + (f.get("sgst") or 0) + (f.get("igst") or 0), 2)
    sub, total = f.get("subtotal"), f.get("grand_total")

    if incl:
        # Tax-inclusive pricing: lines add up to the gross total; the taxable value is backed out.
        backed = round(lines_sum / (1 + rate / 100), 2)
        ok = close(lines_sum, total, 1.0) and (sub is None or close(backed, sub, 2.0))
        checks.append(chk("subtotal", "Lines → total (GST-inclusive rates)", "pass" if ok else "fail",
                          f"Rates include {rate:g}% GST: lines {inr(lines_sum)} → taxable {inr(backed)} + GST {inr(lines_sum - backed)}"))
        if sub is None:
            f["subtotal"] = sub = backed
    elif sub is not None and items:
        ok = close(lines_sum, sub)
        checks.append(chk("subtotal", "Lines add up to subtotal", "pass" if ok else "fail",
                          f"Σ lines {inr(lines_sum)} vs subtotal {inr(sub)}"))
        if not ok and not bad_lines:
            issues.append(issue("SUBTOTAL_MISMATCH", "REJECT", "Vendor", f"Line items add to {inr(lines_sum)} but subtotal shows {inr(sub)}.",
                                vendor_ask=f"Line items total {inr(lines_sum)} but the subtotal printed is {inr(sub)}. Please issue a corrected invoice."))
    elif sub is None:
        f["subtotal"] = sub = lines_sum

    if total is None:
        checks.append(chk("total", "Grand total present", "fail", "No clear total on the document"))
        issues.append(issue("MISSING_TOTAL", "HOLD", "AP team", "No readable grand total.", internal_ask="Confirm the payable amount with the vendor."))
    elif sub is not None:
        ok = close(sub + tax, total)
        checks.append(chk("total", "Subtotal + GST = total", "pass" if ok else "fail",
                          f"{inr(sub)} + {inr(tax)} = {inr(sub + tax)} vs total {inr(total)}"))
        if not ok:
            issues.append(issue("TOTAL_MISMATCH", "REJECT", "Vendor", f"Subtotal + GST ({inr(sub + tax)}) ≠ invoice total ({inr(total)}).",
                                vendor_ask=f"Subtotal {inr(sub)} + GST {inr(tax)} = {inr(sub + tax)}, but the invoice total is {inr(total)}. Please correct.",
                                exposure=max(total - sub - tax, 0)))
    return checks, issues


# ───────────────────────── vendor ─────────────────────────
def identify_vendor(f):
    g = normalize_gstin(f.get("vendor_gstin"))
    for v in M.VENDORS:
        if g and v["gstin"] == g:
            return v, "GSTIN exact match", 1.0
    best, score = None, 0
    name = (f.get("vendor_name") or "").lower()
    for v in M.VENDORS:
        for n in [v["name"]] + v.get("aliases", []):
            s = fuzz.token_set_ratio(name, n.lower())
            if s > score:
                best, score = v, s
    if best and score >= 88:
        return best, f"name match ({score:.0f}%) — GSTIN did not match", score / 100
    return None, "no match in vendor master", 0


def check_vendor(f, vendor, how):
    checks, issues = [], []
    ok, why = validate_gstin(f.get("vendor_gstin"))
    checks.append(chk("gstin_valid", "Vendor GSTIN valid (checksum)", "pass" if ok else "fail", why))

    if not vendor:
        checks.append(chk("vendor_master", "Vendor in vendor master", "fail", f"'{f.get('vendor_name')}' ({f.get('vendor_gstin') or 'no GSTIN'}) is not an onboarded vendor"))
        issues.append(issue("UNKNOWN_VENDOR", "HOLD", "Procurement",
                            f"{f.get('vendor_name')} is not in the vendor master. No payment can be made to an un-onboarded vendor.",
                            internal_ask="Was this purchase authorised? If yes, onboard the vendor (KYC, bank verification) and raise a PO; if not, escalate as maverick spend.",
                            exposure=f.get("grand_total") or 0))
        return checks, issues

    checks.append(chk("vendor_master", "Vendor in vendor master", "pass", f"{vendor['name']} ({vendor['id']}) · {how}"))
    if not ok or normalize_gstin(f.get("vendor_gstin")) != vendor["gstin"]:
        issues.append(issue("GSTIN_MISMATCH", "REJECT", "Vendor",
                            f"GSTIN on invoice ({f.get('vendor_gstin')}) {'is invalid and ' if not ok else ''}does not match the registered GSTIN {vendor['gstin']}. Input tax credit would be lost.",
                            vendor_ask=f"The GSTIN printed on the invoice ({f.get('vendor_gstin')}) does not match your registered GSTIN ({vendor['gstin']}). Please re-issue with the correct GSTIN so we can claim input tax credit."))
        checks.append(chk("gstin_match", "GSTIN matches vendor master", "fail", f"Invoice {f.get('vendor_gstin')} vs master {vendor['gstin']}"))
    else:
        checks.append(chk("gstin_match", "GSTIN matches vendor master", "pass", vendor["gstin"]))

    if vendor["status"] != "active":
        checks.append(chk("vendor_status", "Vendor status", "fail", f"{vendor['status'].upper()}: {vendor.get('block_reason', '')}"))
        issues.append(issue("VENDOR_BLOCKED", "HOLD", "Vendor management",
                            f"{vendor['name']} is {vendor['status']} in the vendor master — {vendor.get('block_reason', '')}.",
                            internal_ask="Complete the pending KYC re-verification before this invoice can be released. Liability is recorded; payment is blocked."))
    else:
        checks.append(chk("vendor_status", "Vendor status", "pass", f"Active · {vendor['category']}" + (f" · MSME ({vendor['msme']})" if vendor.get("msme") else "")))

    # Bank details — the single most expensive thing to get wrong.
    acct, ifsc = f.get("bank_account_number"), f.get("bank_ifsc")
    mb = vendor["bank"]
    notes = (f.get("notes") or "").lower()
    change_language = any(k in notes for k in ["bank", "account", "remit"]) and any(k in notes for k in ["change", "new", "update"])
    if not acct and not ifsc:
        checks.append(chk("bank", "Remit-to bank matches master", "info", "No bank details on invoice — will pay to verified account on file"))
    elif norm_id(acct) == norm_id(mb["account_number"]) and (not ifsc or norm_id(ifsc) == norm_id(mb["ifsc"])):
        checks.append(chk("bank", "Remit-to bank matches master", "pass", f"A/c ••••{mb['account_number'][-4:]} · {mb['ifsc']} (verified)"))
    else:
        detail = f"Invoice asks for A/c {acct} / {ifsc}; verified account on file is ••••{mb['account_number'][-4:]} / {mb['ifsc']}"
        if change_language:
            detail += ". Invoice text announces a bank change — classic business-email-compromise pattern"
        checks.append(chk("bank", "Remit-to bank matches master", "fail", detail))
        issues.append(issue("BANK_MISMATCH", "HOLD", "Treasury / Security",
                            "Bank account on the invoice differs from the verified account in the vendor master." +
                            (" The invoice itself announces the change." if change_language else ""),
                            internal_ask=f"Do NOT update bank details from the invoice or reply to the sending email. Call {vendor['contact']['name']} on the number on file ({vendor['contact']['phone']}) to confirm. Payment stays blocked until a bank-change form + cancelled cheque is verified.",
                            exposure=f.get("grand_total") or 0))
    return checks, issues


# ───────────────────────── duplicates ─────────────────────────
def inv_key(s):
    if not s:
        return ""
    toks = re.findall(r"[A-Z]+|\d+", str(s).upper())
    return "".join(t.lstrip("0") or "0" if t.isdigit() else t for t in toks)


def check_duplicates(f, vendor, file_hash, prior_runs):
    """Against the AP ledger AND everything this system has processed. Exact, normalised and fuzzy."""
    checks, issues = [], []
    total = f.get("grand_total") or 0
    d = parse_date(f.get("invoice_date"))
    key = inv_key(f.get("invoice_number"))

    for r in prior_runs:
        if r.get("file_hash") == file_hash:
            checks.append(chk("dup_file", "Same file seen before", "fail", f"Byte-identical file already processed in run {r['id'][:8]} ({r.get('decision')})"))
            issues.append(issue("DUPLICATE_FILE", "REJECT", "Vendor", f"This exact file was already processed (run {r['id'][:8]}, {r.get('decision')}).",
                                vendor_ask="This invoice has already been received and is being processed. No action needed — please don't resend.", exposure=total))
            return checks, issues
    checks.append(chk("dup_file", "Same file seen before", "pass", "File fingerprint (SHA-256) is new"))

    if not vendor:
        checks.append(chk("dup_inv", "Duplicate of a booked invoice", "info", "Skipped — vendor unknown"))
        return checks, issues

    candidates = []
    for l in M.INVOICE_LEDGER:
        if l["vendor_id"] == vendor["id"]:
            candidates.append({"source": "AP ledger", "invoice_number": l["invoice_number"], "date": l["invoice_date"], "total": l["total"],
                               "po": l["po_number"], "status": l["status"]})
    for r in prior_runs:
        res = r.get("summary") or {}
        if res.get("vendor_id") == vendor["id"] and r.get("decision") not in ("REJECTED", None):
            candidates.append({"source": f"run {r['id'][:8]}", "invoice_number": res.get("invoice_number"), "date": res.get("invoice_date"),
                               "total": res.get("total"), "po": res.get("po_number"), "status": r.get("decision", "").lower()})

    best = None
    for c in candidates:
        signals = []
        ck = inv_key(c["invoice_number"])
        if key and ck == key:
            signals.append("same invoice number (after normalising separators/zeros)" if c["invoice_number"] != f.get("invoice_number") else "identical invoice number")
        elif key and ck and fuzz.ratio(key, ck) >= 85:
            signals.append(f"near-identical number ({c['invoice_number']})")
        if c["total"] and total and abs(c["total"] - total) <= total * P["duplicate_amount_pct"] / 100:
            signals.append(f"same amount {inr(c['total'])}")
        cd = parse_date(c["date"])
        if cd and d and abs((cd - d).days) <= P["duplicate_window_days"]:
            signals.append(f"{abs((cd - d).days)} days apart")
        if c["po"] and f.get("po_reference") and norm_id(c["po"]) == norm_id(f.get("po_reference")):
            signals.append("same PO")
        # Exact (normalised) number = duplicate. Otherwise require the same amount PLUS at least two corroborating
        # signals — consecutive numbers from one vendor (…/0219 vs …/0241) are similar by design and must not trip this alone.
        exact_no = any(s.startswith(("same invoice number", "identical invoice number")) for s in signals)
        same_amt = any("amount" in s for s in signals)
        strong = exact_no or (same_amt and len(signals) >= 3)
        if strong and (best is None or len(signals) > len(best[1])):
            best = (c, signals)

    if best:
        c, signals = best
        checks.append(chk("dup_inv", "Duplicate of a booked invoice", "fail",
                          f"Matches {c['invoice_number']} ({c['source']}, {c['status']}, {c['date']}): " + "; ".join(signals)))
        issues.append(issue("DUPLICATE_INVOICE", "REJECT", "Vendor",
                            f"Probable duplicate of invoice {c['invoice_number']} dated {c['date']} ({c['status']}) — " + ", ".join(signals) + ".",
                            vendor_ask=f"This appears to be a resubmission of invoice {c['invoice_number']} dated {c['date']}, which is already {c['status']} in our books. If this is a genuinely new supply, please share the delivery challan / GRN reference.",
                            exposure=total))
    else:
        checks.append(chk("dup_inv", "Duplicate of a booked invoice", "pass", f"Compared against {len(candidates)} prior invoices from this vendor (exact, normalised & fuzzy)"))
    return checks, issues


# ───────────────────────── PO / GRN matching ─────────────────────────
_ABBR = [(r"\bcu\b", "copper"), (r"sq\s*mm|sqmm", "sq mm"), (r"\b4p\b", "4 pole"), (r"4-pole", "4 pole"), (r"\bpcs?\b", ""),
         (r"[()\-,/]", " ")]


def _clean(s):
    s = (s or "").lower()
    for a, b in _ABBR:
        s = re.sub(a, b, s)
    return re.sub(r"\s+", " ", s).strip()


def consumption(po_number, prior_runs):
    """How much of each PO line has already been billed (ledger + approved runs)."""
    used = {}
    for l in M.INVOICE_LEDGER:
        if l["po_number"] == po_number:
            for ln in l["lines"]:
                used[ln["line"]] = used.get(ln["line"], 0) + ln["qty"]
    for r in prior_runs:
        s = r.get("summary") or {}
        if s.get("po_number") == po_number and r.get("decision") in ("APPROVED", "PARTIALLY_APPROVED"):
            for ln in s.get("consumed_lines", []):
                used[ln["line"]] = used.get(ln["line"], 0) + ln["qty"]
    return used


def received(po_number):
    rec = {}
    for g in M.GRNS:
        if g["po_number"] == po_number:
            for ln in g["lines"]:
                rec[ln["line"]] = rec.get(ln["line"], 0) + ln["received"]
    return rec


def ex_tax_price(li, f):
    up = li.get("unit_price")
    if up is None and li.get("amount") and li.get("quantity"):
        up = li["amount"] / li["quantity"]
    if up is not None and f.get("rates_include_tax"):
        up = up / (1 + (f.get("gst_rate_percent") or 18) / 100)
    return up


def match_lines(f, po):
    """Assign each invoice line to its best PO line. Score = description similarity + HSN + price closeness."""
    pairs = []
    used = set()
    for i, li in enumerate(f["line_items"]):
        best, bs = None, 0.0
        up = ex_tax_price(li, f)
        for pl in po["lines"]:
            if pl["line"] in used:
                continue
            d = fuzz.token_set_ratio(_clean(li["description"]), _clean(pl["description"])) / 100
            h = 1.0 if li.get("hsn_sac") and str(li["hsn_sac"]).strip() == pl["hsn"] else 0.0
            p = 1 - min(1, abs((up or 0) - pl["unit_price"]) / pl["unit_price"]) if up else 0.0
            s = 0.6 * d + 0.25 * h + 0.15 * p
            if s > bs:
                best, bs = pl, s
        if best and bs >= 0.55:
            used.add(best["line"])
        else:
            best = None
        pairs.append((i, li, best, round(bs, 3)))
    return pairs


def infer_po(f, vendor, prior_runs):
    """No PO on the invoice → rank this vendor's open POs by line similarity and remaining-quantity fit."""
    cands = []
    for po in M.PURCHASE_ORDERS:
        if po["vendor_id"] != vendor["id"] or po["status"] != "open":
            continue
        pairs = match_lines(f, po)
        if not pairs:
            continue
        line_score = sum(s for _, _, pl, s in pairs if pl) / len(pairs)
        used, rec = consumption(po["po_number"], prior_runs), received(po["po_number"])
        fit = []
        for _, li, pl, _ in pairs:
            if not pl:
                fit.append(0)
                continue
            avail = rec.get(pl["line"], 0) - used.get(pl["line"], 0)
            fit.append(1.0 if (li.get("quantity") or 0) <= avail else (max(avail, 0) / li["quantity"] if li.get("quantity") else 0))
        qty_fit = sum(fit) / len(fit)
        score = round(0.7 * line_score + 0.3 * qty_fit, 3)
        cands.append({"po_number": po["po_number"], "score": score, "line_similarity": round(line_score, 3), "open_qty_fit": round(qty_fit, 3),
                      "po_date": po["date"], "po_total": po["total"]})
    cands.sort(key=lambda c: -c["score"])
    return cands


def check_po(f, vendor, prior_runs):
    checks, issues, out = [], [], {"po": None, "inferred": False, "candidates": [], "lines": [], "approvable_taxable": None, "consumed_lines": []}
    if not vendor:
        checks.append(chk("po_found", "Purchase order", "info", "Skipped — vendor unknown"))
        if not f.get("po_reference"):
            issues.append(issue("NO_PO", "HOLD", "Procurement", "No purchase order referenced (and vendor not onboarded).",
                                internal_ask="Identify the requester and raise a retrospective PO if the purchase is legitimate."))
        return checks, issues, out

    po = None
    ref = f.get("po_reference")
    if ref:
        po = M.po_by_number(ref)
        if not po:
            checks.append(chk("po_found", "Purchase order", "fail", f"PO '{ref}' printed on invoice does not exist"))
            issues.append(issue("PO_NOT_FOUND", "HOLD", "Procurement", f"Invoice references PO '{ref}', which does not exist.",
                                internal_ask=f"Confirm which PO this {vendor['name']} invoice belongs to."))
            return checks, issues, out
        if po["vendor_id"] != vendor["id"]:
            checks.append(chk("po_found", "Purchase order", "fail", f"{po['po_number']} belongs to a different vendor"))
            issues.append(issue("PO_VENDOR_MISMATCH", "HOLD", "Procurement", f"{po['po_number']} was issued to another vendor.",
                                internal_ask="Confirm the correct PO for this invoice."))
            return checks, issues, out
        checks.append(chk("po_found", "Purchase order", "pass", f"{po['po_number']} (explicit on invoice) · {po['status']} · {inr(po['total'])}"))
    else:
        cands = infer_po(f, vendor, prior_runs)
        out["candidates"] = cands
        if cands:
            top = cands[0]
            margin = top["score"] - (cands[1]["score"] if len(cands) > 1 else 0)
            desc = ", ".join(f"{c['po_number']} {c['score']:.2f}" for c in cands[:3])
            if top["score"] >= P["po_inference_min_score"] and margin >= P["po_inference_min_margin"]:
                po = M.po_by_number(top["po_number"])
                out["inferred"] = True
                checks.append(chk("po_found", "Purchase order", "warn",
                                  f"No PO on invoice → inferred {po['po_number']} with score {top['score']:.2f} (margin {margin:.2f}). Candidates: {desc}"))
                issues.append(issue("PO_INFERRED", "WARN", "AP team", f"PO not printed; matched to {po['po_number']} by line items and open quantities (score {top['score']:.2f})."))
            else:
                checks.append(chk("po_found", "Purchase order", "fail", f"No PO on invoice and no confident match. Candidates: {desc}"))
                issues.append(issue("PO_AMBIGUOUS", "HOLD", "AP team", "No PO on the invoice and the best guess isn't confident enough to auto-match.",
                                    internal_ask=f"Pick the right PO: {desc}."))
                return checks, issues, out
        else:
            checks.append(chk("po_found", "Purchase order", "fail", "No PO on invoice and vendor has no open POs"))
            issues.append(issue("NO_PO", "HOLD", "Procurement", "No purchase order referenced and none open for this vendor.",
                                internal_ask="Raise a PO or confirm this is non-PO spend with budget-owner approval."))
            return checks, issues, out

    out["po"] = po
    pairs = match_lines(f, po)
    used, rec = consumption(po["po_number"], prior_runs), received(po["po_number"])
    approvable = 0.0
    full_taxable = 0.0
    price_issues, qty_issues, unmatched = [], [], []
    for i, li, pl, s in pairs:
        up = ex_tax_price(li, f)
        qty = li.get("quantity") or 0
        full_taxable += (up or 0) * qty
        if not pl:
            unmatched.append(li)
            out["lines"].append({"invoice_line": li["description"], "po_line": None, "score": s, "status": "fail", "note": "Not on PO"})
            continue
        var = ((up - pl["unit_price"]) / pl["unit_price"] * 100) if up else 0
        ordered, already, got = pl["qty"], used.get(pl["line"], 0), rec.get(pl["line"], 0)
        open_rec = max(got - already, 0)
        ok_qty = min(qty, open_rec, max(ordered - already, 0))
        line_status, notes = "pass", []
        if var > P["price_tolerance_pct"]:
            line_status = "fail"
            price_issues.append((li, pl, var, up))
            notes.append(f"price +{var:.1f}%")
        elif var > 0.05:
            line_status = "warn"
            notes.append(f"price +{var:.1f}% (within {P['price_tolerance_pct']:g}%)")
        if qty > ok_qty + 1e-9:
            line_status = "fail" if line_status != "fail" else line_status
            qty_issues.append((li, pl, qty, ok_qty, ordered, already, got))
            notes.append(f"billable {ok_qty:g} of {qty:g}")
        # approvable = billable qty at the *lower* of PO price and invoice price (within tolerance)
        pay_price = min(up, pl["unit_price"] * (1 + P["price_tolerance_pct"] / 100)) if up else pl["unit_price"]
        approvable += ok_qty * pay_price
        if ok_qty > 0:
            out["consumed_lines"].append({"line": pl["line"], "qty": ok_qty})
        out["lines"].append({"invoice_line": li["description"], "po_line": f"L{pl['line']} {pl['description']}", "score": s,
                             "inv_qty": qty, "po_qty": ordered, "prev_billed": already, "received": got, "billable_qty": ok_qty,
                             "inv_price": round(up, 2) if up else None, "po_price": pl["unit_price"], "price_var_pct": round(var, 2),
                             "status": line_status, "note": "; ".join(notes) or "matches PO & GRN"})

    n_ok = sum(1 for l in out["lines"] if l["status"] == "pass")
    checks.append(chk("line_match", "Invoice lines ↔ PO lines", "pass" if not unmatched else "fail",
                      f"{len(pairs) - len(unmatched)}/{len(pairs)} lines matched to PO lines"))
    if unmatched:
        issues.append(issue("LINE_NOT_ON_PO", "HOLD", "Procurement", f"{len(unmatched)} invoice line(s) are not on {po['po_number']}: " + ", ".join(u["description"] for u in unmatched),
                            internal_ask="Approve the extra item(s) via PO amendment, or ask the vendor to remove them."))

    if price_issues:
        li, pl, var, up = max(price_issues, key=lambda x: x[2])
        exp = sum((u - p["unit_price"]) * (l.get("quantity") or 0) for l, p, _, u in price_issues)
        checks.append(chk("price", "Unit price vs PO", "fail",
                          "; ".join(f"{p['description']}: {inr(u)} vs PO {inr(p['unit_price'])} (+{v:.1f}%)" for l, p, v, u in price_issues)))
        issues.append(issue("PRICE_VARIANCE", "HOLD", "Procurement (buyer)",
                            f"Unit price exceeds PO by more than {P['price_tolerance_pct']:g}% — {pl['description']} billed at {inr(up)} vs PO {inr(pl['unit_price'])} (+{var:.1f}%). Over-billing: {inr(exp)} + GST.",
                            internal_ask=f"Was a price revision agreed with the vendor? If yes, amend {po['po_number']}; if not, we'll request a credit note for {inr(exp)} + GST.",
                            exposure=exp * 1.18))
    else:
        warns = [l for l in out["lines"] if l["status"] == "warn"]
        checks.append(chk("price", "Unit price vs PO", "warn" if warns else "pass",
                          "Within tolerance: " + "; ".join(f"{l['po_line']} +{l['price_var_pct']}%" for l in warns) if warns else "All unit prices match PO"))
        if warns:
            issues.append(issue("PRICE_WITHIN_TOLERANCE", "WARN", "AP team", "Minor price variance within policy tolerance: " + "; ".join(f"{l['po_line']} +{l['price_var_pct']}%" for l in warns)))

    if qty_issues:
        parts = []
        for li, pl, qty, okq, ordered, already, got in qty_issues:
            if already + qty > ordered:
                parts.append(f"{pl['description']}: billed {q(qty)}, but {q(already)} of {q(ordered)} ordered were already invoiced → only {q(max(ordered - already, 0))} left on PO")
            else:
                parts.append(f"{pl['description']}: billed {q(qty)}, GRN shows {q(got)} received ({q(already)} already invoiced) → {q(okq)} billable")
        checks.append(chk("grn", "3-way match (PO · GRN · invoice)", "fail", "; ".join(parts)))
    else:
        checks.append(chk("grn", "3-way match (PO · GRN · invoice)", "pass", "Billed quantities ≤ received and not previously invoiced"))

    prev_val = sum(used.get(pl["line"], 0) * pl["unit_price"] for pl in po["lines"])
    this_val = full_taxable
    cap = po["total"] * (1 + P["po_overrun_tolerance_pct"] / 100)
    cum_ok = prev_val + this_val <= cap + 1
    checks.append(chk("po_value", "Cumulative billing vs PO value", "pass" if cum_ok else "fail",
                      f"Previously billed {inr(prev_val)} + this {inr(this_val)} = {inr(prev_val + this_val)} vs PO {inr(po['total'])}" +
                      ("" if cum_ok else f" → over by {inr(prev_val + this_val - po['total'])} ({(prev_val + this_val) / po['total'] * 100 - 100:.1f}%)")))

    out["approvable_taxable"] = round(approvable, 2)
    if qty_issues and not price_issues and not unmatched:
        held = round(this_val - approvable, 2)
        issues.append(issue("QTY_EXCEEDS_RECEIPT", "PARTIAL", "Vendor",
                            f"Only {inr(approvable)} of {inr(this_val)} (taxable) is supported by goods received / open PO quantity. {inr(held)} + GST held." +
                            (" Cumulative billing across split invoices exceeds the PO." if not cum_ok else ""),
                            vendor_ask="; ".join(parts) + f". We are processing {inr(approvable)} + GST now. Please issue a credit note for the balance of {inr(held)} + GST, or share proof of additional delivery.",
                            exposure=held * 1.18))
    if po.get("status") == "closed":
        checks.append(chk("po_status", "PO open", "fail", f"{po['po_number']} is closed — fully billed and paid"))
    return checks, issues, out


# ───────────────────────── GST compliance ─────────────────────────
def check_tax(f, vendor):
    checks, issues = [], []
    bg = normalize_gstin(f.get("buyer_gstin"))
    if bg == M.BUYER["gstin"]:
        checks.append(chk("buyer_gstin", "Billed to our GSTIN", "pass", bg))
    else:
        checks.append(chk("buyer_gstin", "Billed to our GSTIN", "fail", f"Invoice shows {bg or 'no buyer GSTIN'}; ours is {M.BUYER['gstin']}"))
        issues.append(issue("BUYER_GSTIN", "REJECT", "Vendor", "Invoice is not addressed to our GSTIN — input tax credit cannot be claimed.",
                            vendor_ask=f"Please re-issue the invoice to {M.BUYER['name']}, GSTIN {M.BUYER['gstin']}."))

    vg = vendor["gstin"] if vendor else normalize_gstin(f.get("vendor_gstin"))
    exp = expected_tax_type(vg, M.BUYER["gstin"])
    has_cs = (f.get("cgst") or 0) > 0 or (f.get("sgst") or 0) > 0
    has_i = (f.get("igst") or 0) > 0
    actual = "CGST+SGST" if has_cs and not has_i else "IGST" if has_i and not has_cs else ("mixed" if has_cs and has_i else "none")
    if exp and actual != "none":
        vs = STATE_CODES.get(state_of(vg), state_of(vg))
        if actual == exp:
            checks.append(chk("tax_type", "Correct GST type for place of supply", "pass", f"{vs} → Karnataka = {'intra' if exp == 'CGST+SGST' else 'inter'}-state → {exp} ✓"))
        else:
            checks.append(chk("tax_type", "Correct GST type for place of supply", "fail", f"{vs} → Karnataka is {'intra' if exp == 'CGST+SGST' else 'inter'}-state, so {exp} applies — invoice charges {actual}"))
            issues.append(issue("WRONG_TAX_TYPE", "REJECT", "Vendor",
                                f"Wrong GST type: supply from {vs} to Karnataka requires {exp}, but invoice charges {actual}. ITC on wrongly charged tax is not available to us.",
                                vendor_ask=f"Supply from {vs} to Karnataka is an {'intra' if exp == 'CGST+SGST' else 'inter'}-state supply, so {exp} applies, not {actual}. Please cancel this invoice and issue a fresh one with {exp}."))
    sub = f.get("subtotal") or 0
    tax = (f.get("cgst") or 0) + (f.get("sgst") or 0) + (f.get("igst") or 0)
    rate = f.get("gst_rate_percent") or (round(tax / sub * 100) if sub else None)
    if sub and rate:
        ok = close(sub * rate / 100, tax, max(2.0, sub * 0.0005))
        cs_ok = not has_cs or close(f.get("cgst"), f.get("sgst"), 1.0)
        checks.append(chk("tax_calc", "GST amount = taxable × rate", "pass" if ok and cs_ok else "fail",
                          f"{inr(sub)} × {rate:g}% = {inr(sub * rate / 100)} vs charged {inr(tax)}" + ("" if cs_ok else " · CGST ≠ SGST")))
        if not (ok and cs_ok):
            issues.append(issue("TAX_CALC", "REJECT", "Vendor", f"GST charged ({inr(tax)}) ≠ {rate:g}% of taxable value ({inr(sub * rate / 100)}).",
                                vendor_ask=f"GST should be {inr(sub * rate / 100)} ({rate:g}% of {inr(sub)}), invoice shows {inr(tax)}. Please correct."))
    return checks, issues


# ───────────────────────── decision ─────────────────────────
RANK = {"REJECT": 3, "HOLD": 2, "PARTIAL": 1, "WARN": 0}
DECISION_OF = {"REJECT": "REJECTED", "HOLD": "ON_HOLD", "PARTIAL": "PARTIALLY_APPROVED"}


def payment_proposal(f, vendor, po, taxable, partial=False):
    """Payment proposal: TDS on taxable value, MSME-capped due date, paid only to the verified bank account, journal entry."""
    total = f.get("grand_total") or 0
    rate = (f.get("gst_rate_percent") or 18) / 100
    taxable = round(taxable, 2)
    gst = round(taxable * rate, 2)
    tds = round(taxable * vendor["tds_rate_pct"] / 100, 2)
    inv_d = parse_date(f.get("invoice_date")) or TODAY
    terms = vendor["payment_terms_days"]
    if vendor.get("msme"):
        terms = min(terms, P["msme_max_payment_days"])
    due = inv_d + timedelta(days=terms)
    intra = expected_tax_type(vendor["gstin"], M.BUYER["gstin"]) == "CGST+SGST"
    je = [{"account": po["gl_code"], "dr": taxable, "cr": 0, "cost_center": po["cost_center"]}]
    if intra:
        je += [{"account": "1410 · Input CGST", "dr": round(gst / 2, 2), "cr": 0}, {"account": "1411 · Input SGST", "dr": round(gst / 2, 2), "cr": 0}]
    else:
        je += [{"account": "1412 · Input IGST", "dr": gst, "cr": 0}]
    je += [{"account": f"2210 · TDS payable u/s {vendor['tds_section']}", "dr": 0, "cr": tds},
           {"account": f"2100 · Sundry creditors — {vendor['name']}", "dr": 0, "cr": round(taxable + gst - tds, 2)}]
    return {"approved_taxable": taxable, "gst": gst, "gross": round(taxable + gst, 2), "tds_section": vendor["tds_section"],
            "tds_rate_pct": vendor["tds_rate_pct"], "tds": tds, "net_payable": round(taxable + gst - tds, 2),
            "held_amount": round(total - taxable - gst, 2) if partial else 0.0,
            "due_date": due.isoformat(), "terms_days": terms, "msme": vendor.get("msme"),
            "pay_to": f"{vendor['bank']['bank_name']} ••••{vendor['bank']['account_number'][-4:]} (verified, from vendor master)",
            "gl_code": po["gl_code"], "cost_center": po["cost_center"], "journal": je}


def decide(f, vendor, po_out, issues):
    blocking = [i for i in issues if i["outcome"] != "WARN"]
    warns = [i for i in issues if i["outcome"] == "WARN"]
    total = f.get("grand_total") or 0
    rate = (f.get("gst_rate_percent") or 18) / 100

    if blocking:
        top = max(blocking, key=lambda i: RANK[i["outcome"]])
        decision = DECISION_OF[top["outcome"]]
    else:
        top, decision = None, "APPROVED"

    po = po_out.get("po")
    if decision == "APPROVED" and total > P["cfo_approval_above_inr"]:
        decision = "ON_HOLD"
        top = issue("CFO_LIMIT", "HOLD", "CFO", f"Clean match, but {inr(total)} exceeds the ₹10L auto-approval limit.", internal_ask="CFO sign-off required.")
        blocking.append(top)

    payment = None
    if decision in ("APPROVED", "PARTIALLY_APPROVED") and vendor and po:
        taxable = po_out["approvable_taxable"] if decision == "PARTIALLY_APPROVED" else (f.get("subtotal") or 0)
        payment = payment_proposal(f, vendor, po, taxable, partial=decision == "PARTIALLY_APPROVED")

    headline = {
        "APPROVED": "Approved for payment",
        "PARTIALLY_APPROVED": "Partially approved — balance held",
        "ON_HOLD": f"On hold — {top['owner'] if top else ''}",
        "REJECTED": "Rejected — returned to vendor",
    }[decision]

    reasons = [i["message"] for i in sorted(blocking, key=lambda i: -RANK[i["outcome"]])] + [i["message"] for i in warns]
    if decision == "APPROVED":
        reasons.insert(0, "Vendor verified, no duplicate, prices and quantities match PO and goods receipt, GST correct, arithmetic reconciles.")

    vendor_email = None
    # Only the issues that drove the decision go to the vendor; holds are internal and never emailed out.
    v_asks = [i["vendor_ask"] for i in blocking if i.get("vendor_ask") and top and i["outcome"] == top["outcome"]]
    if vendor and v_asks and decision in ("REJECTED", "PARTIALLY_APPROVED") and not any(i["code"] == "BANK_MISMATCH" for i in blocking):
        vendor_email = {
            "to": vendor["contact"]["email"],
            "subject": f"Invoice {f.get('invoice_number') or '(no number)'} — {'action needed' if decision != 'PARTIALLY_APPROVED' else 'partially processed'}",
            "body": f"Dear {vendor['contact']['name']},\n\nThank you for invoice {f.get('invoice_number') or '(no number)'} dated {f.get('invoice_date') or '—'} for {inr(total)}.\n\n" +
                    ("We are unable to process it in its current form:\n" if decision == "REJECTED" else "We have reviewed it against our purchase order and goods receipts:\n") +
                    "\n".join(f"  • {a}" for a in v_asks) +
                    (f"\n\nThe approved portion ({inr(payment['gross'])} incl. GST, less TDS) is scheduled for payment on {payment['due_date']}." if payment else "") +
                    f"\n\nRegards,\nAccounts Payable\n{M.BUYER['name']}",
        }
    internal = [{"owner": i["owner"], "ask": i["internal_ask"]} for i in blocking if i.get("internal_ask")]
    exposure = round(sum(i["exposure_inr"] for i in blocking), 2)
    if decision in ("REJECTED", "ON_HOLD"):
        exposure = min(exposure, total) if exposure else 0
    return {"decision": decision, "headline": headline, "primary_reason": top["message"] if top else reasons[0],
            "reasons": reasons, "owner": top["owner"] if top else None, "primary_code": top["code"] if top else None, "issues": issues, "payment": payment,
            "vendor_email": vendor_email, "internal_tasks": internal, "risk_caught_inr": exposure}
