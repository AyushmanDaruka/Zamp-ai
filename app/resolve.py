"""Human-in-the-loop: what a person may do with an exception, and the guardrails even a human can't bypass.

The system's decision is never rewritten — a resolution is recorded next to it (who, when, why), so the audit
trail always shows what the machine decided and what a human changed.
"""
from datetime import date, datetime, timedelta, timezone

from app import master as M
from app import rules as R

# Issues no reviewer can override from the AP desk: they need the vendor to re-issue, or master data to be fixed first.
HARD_STOPS = {
    "DUPLICATE_INVOICE": "Releasing would pay this invoice twice.",
    "DUPLICATE_FILE": "This exact file was already processed — releasing would pay twice.",
    "UNKNOWN_VENDOR": "No verified vendor or bank account to pay — onboard the vendor first.",
    "VENDOR_BLOCKED": "Vendor is blocked in the master — clear KYC and unblock first.",
    "GSTIN_MISMATCH": "Invalid GSTIN means input tax credit is lost — the vendor must re-issue.",
    "WRONG_TAX_TYPE": "Wrong GST type means input tax credit is lost — the vendor must re-issue.",
    "BUYER_GSTIN": "Not addressed to our GSTIN — the vendor must re-issue.",
    "TAX_CALC": "GST is miscalculated — the vendor must re-issue.",
    "MISSING_INVOICE_NUMBER": "Not a valid GST tax invoice without a serial number (CGST Rule 46).",
    "LINE_ARITHMETIC": "The document's own arithmetic is wrong — the vendor must correct it.",
    "SUBTOTAL_MISMATCH": "The document's own arithmetic is wrong — the vendor must correct it.",
    "TOTAL_MISMATCH": "The document's own arithmetic is wrong — the vendor must correct it.",
    "NO_PO": "No PO to book against — link or raise a PO first.",
    "PO_NOT_FOUND": "PO doesn't exist — link the right PO first.",
    "PO_AMBIGUOUS": "PO is ambiguous — link the right PO first.",
    "FUTURE_DATE": "Future-dated invoice — the vendor must re-issue.",
}

LABELS = {
    "release": "Release for payment",
    "accept_partial": "Accept partial, request credit note",
    "send_back": "Send back to vendor",
    "reject": "Reject",
}
FINAL = {"release": "APPROVED", "accept_partial": "PARTIALLY_APPROVED", "send_back": "REJECTED", "reject": "REJECTED"}
NEEDS_NOTE = {"release", "reject"}


def _blocking(run):
    d = ((run.get("result") or {}).get("decision") or {})
    return [i for i in d.get("issues", []) if i.get("outcome") != "WARN"]


def options(run):
    """Actions available to a reviewer on this run, each with allowed / why-not."""
    if run.get("status") != "done" or (run.get("summary") or {}).get("resolution"):
        return []
    dec = run["decision"]
    if dec == "APPROVED":
        return []
    stops = [(i["code"], HARD_STOPS[i["code"]]) for i in _blocking(run) if i["code"] in HARD_STOPS]
    res = run.get("result") or {}
    po = (res.get("po") or {}).get("po")
    vid = (run.get("summary") or {}).get("vendor_id")
    vendor = M.vendor_by_id(vid) if vid else None
    why_not = stops[0][1] if stops else (None if vendor and po else "Needs a known vendor and a PO to pay against.")
    release = {"action": "release", "allowed": why_not is None, "why_not": why_not}
    if dec == "ON_HOLD":
        acts = [release, {"action": "reject", "allowed": True}]
    elif dec == "PARTIALLY_APPROVED":
        acts = [{"action": "accept_partial", "allowed": True}, {"action": "reject", "allowed": True}]
    else:  # REJECTED
        acts = [{"action": "send_back", "allowed": True}, release]
    for a in acts:
        a["label"] = LABELS[a["action"]]
        a["needs_note"] = a["action"] in NEEDS_NOTE
        a["final"] = FINAL[a["action"]]
    return acts


def apply(run, action, note, by):
    """Validate and record a resolution. Returns (summary, approved_amount). Raises ValueError on a refused action."""
    opts = {o["action"]: o for o in options(run)}
    if action not in opts:
        raise ValueError("This action isn't available for this invoice.")
    o = opts[action]
    if not o["allowed"]:
        raise ValueError(o["why_not"])
    note = (note or "").strip()
    by = (by or "").strip()[:60] or "AP reviewer"
    if o["needs_note"] and len(note) < 8:
        raise ValueError("A reason is required (at least a short sentence) — it goes on the audit trail.")

    summary = dict(run.get("summary") or {})
    res = run.get("result") or {}
    approved = run.get("approved_amount") or 0.0
    payment = None
    if action == "release":
        f, po_out = res["fields"], res["po"]
        vendor = M.vendor_by_id(summary["vendor_id"])
        payment = R.payment_proposal(f, vendor, po_out["po"], f.get("subtotal") or 0)
        summary["consumed_lines"] = po_out.get("consumed_lines", [])
        approved = payment["gross"]
    elif action in ("reject", "send_back"):
        summary["consumed_lines"] = []
        approved = 0.0
    now = datetime.now(timezone.utc).isoformat()
    summary["resolution"] = {"action": action, "label": o["label"], "final_decision": o["final"], "note": note[:1000],
                             "by": by, "at": now, "payment": payment}
    summary["audit"] = (summary.get("audit") or []) + [{"at": now, "by": by, "what": o["label"], "note": note[:1000]}]
    return summary, approved


def msme_clock(run):
    """For MSME vendors: the statutory pay-by date (MSMED Act s.15, max 45 days) and days left from the business date."""
    s = run.get("summary") or {}
    v = M.vendor_by_id(s["vendor_id"]) if s.get("vendor_id") else None
    if not v or not v.get("msme"):
        return None
    d = R.parse_date(s.get("invoice_date"))
    if not d:
        return None
    due = d + timedelta(days=min(v["payment_terms_days"], R.P["msme_max_payment_days"]))
    return {"category": v["msme"], "pay_by": due.isoformat(), "days_left": (due - R.TODAY).days}


def inbox_item(run):
    d = ((run.get("result") or {}).get("decision") or {})
    blocking = _blocking(run)
    top = next((i for i in blocking if i["code"] == d.get("primary_code")), blocking[0] if blocking else None)
    ask = (top or {}).get("internal_ask") or (top or {}).get("vendor_ask") or d.get("primary_reason")
    owner = d.get("owner") or "AP team"
    if run["decision"] == "ERROR":
        ask, owner = "AI reader didn't answer. Retry, or key the invoice in manually.", "AP team"
    return {"id": run["id"], "created_at": run["created_at"], "decision": run["decision"], "code": d.get("primary_code"),
            "owner": owner, "ask": ask, "reason": d.get("primary_reason"), "vendor_name": run.get("vendor_name"),
            "invoice_number": run.get("invoice_number"), "total": run.get("total"), "held": (run.get("total") or 0) - (run.get("approved_amount") or 0),
            "msme": msme_clock(run), "filename": run.get("filename"), "source": run.get("source"),
            "resolution": (run.get("summary") or {}).get("resolution")}
