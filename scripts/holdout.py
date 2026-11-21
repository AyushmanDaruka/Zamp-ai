"""Held-out test set — invoices the system was never tuned on.

Differences from the development set (scripts/cases.py):
  * a NEW invoice layout ("ledger") never seen during development, used for most cases;
  * item descriptions are re-worded (abbreviations, different word order) vs the PO text;
  * quantities, prices, dates, invoice numbers, which invoices are scanned — all drawn at random (fixed seed);
  * new unknown vendors.
The label for each case comes from the scenario it was built from (the policy an AP analyst applies),
NOT from running app/rules.py — so the rules engine can't grade its own homework.

Usage:  .venv/bin/python -m scripts.holdout [--seed 2027] [--n 24]
Writes: samples/holdout/<id>.pdf and samples/holdout/cases.json
"""
import argparse
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app import master as M
from app.gst import make_gstin
from scripts import cases as C
from scripts.gen_invoices import TEMPLATES, amount_words, compute, scanify, vendor_profile

OUT = Path(__file__).resolve().parent.parent / "samples" / "holdout"
W, H = A4

# Re-worded descriptions, as different vendors/clerks actually write them.
REWORD = {
    "MS Round Bar 12mm (IS 2062)": ["M.S. Round Bar Dia 12 mm IS:2062", "Round bar MS 12mm", "12mm MS round (IS2062 E250)"],
    "MS Sheet 2mm 1250x2500": ["M.S. Sheet 2.0mm thk 1250 x 2500", "MS sheet 2 mm (1250*2500)"],
    "Corrugated Box 5-ply 400x300x250": ["5 Ply Corrugated Carton 400x300x250mm", "Corr. box 5ply 400*300*250"],
    "EPE Foam Insert 380x280": ["EPE foam insert 380 x 280 mm", "Foam insert (EPE) 380x280"],
    "Managed IT Support - November 2026": ["Managed IT support services Nov-26", "IT Managed Services (Nov 2026)"],
    "Cloud Backup Licence (per user)": ["Cloud backup licence - per user / month", "Backup licence (cloud) per user"],
    "MCB 32A 4-Pole C-Curve": ["MCB 4P 32 Amp C curve", "32A FP MCB C-type"],
    "Copper Cable 4 sq mm FRLS (90m coil)": ["4 sqmm Cu FRLS wire 90 mtr", "FRLS copper cable 4.0 sq.mm coil 90m"],
    "LED Panel Light 40W 2x2": ["LED panel 2'x2' 40 watt", "40W LED 2x2 panel light"],
    "Copper Cable 2.5 sq mm FRLS (90m coil)": ["2.5 sqmm Cu FRLS wire 90 mtr", "FRLS copper cable 2.5 sq.mm coil"],
    "Modular Switch 16A": ["16 Amp modular switch", "Switch modular 16A 1-way"],
    "Carbide End Mill 10mm 4-Flute": ["Solid carbide endmill D10 4F", "End mill carbide 10 mm 4 flute"],
    "Turning Insert CNMG 120408": ["Insert CNMG120408 (turning)", "CNMG 12 04 08 carbide insert"],
    "HSS Drill Bit 8mm": ["Drill HSS 8.0 mm", "8mm HSS twist drill"],
    "Tap Set M10 (3 pc)": ["M10 hand tap set (3 pcs)", "Tap set M10 x 1.5 - 3pc"],
    "Full Truck Load Freight Kochi-Bengaluru": ["FTL freight Cochin to Bangalore", "Truck freight Kochi-BLR (FTL)"],
}

NEW_UNKNOWN = [
    {"name": "Anand Fasteners & Hardware", "state": "29", "pan": "BXKPA5521Q", "email": "anandfasteners@gmail.com",
     "address": "No. 4, KSSIDC Estate, Rajajinagar, Bengaluru 560044",
     "bank": {"account_name": "Anand Fasteners", "account_number": "917020044551", "ifsc": "UTIB0000123", "bank_name": "Axis Bank, Rajajinagar"},
     "lines": [("Hex bolt M12x50 GR 8.8", "7318", 500, "nos", 14.50), ("Spring washer M12", "7318", 500, "nos", 1.20)]},
    {"name": "Lakshmi Industrial Gases", "state": "33", "pan": "AAGFL9912C", "email": "accounts@lakshmigases.in",
     "address": "SIDCO Industrial Estate, Ambattur, Chennai 600098, Tamil Nadu",
     "bank": {"account_name": "Lakshmi Industrial Gases", "account_number": "6120458877", "ifsc": "IDIB000A012", "bank_name": "Indian Bank, Ambattur"},
     "lines": [("Oxygen cylinder refill 7 cum", "2804", 40, "cyl", 310.00), ("DA cylinder refill", "2901", 10, "cyl", 2150.00)]},
]

NEW_BANK = {"account_name": "{name}", "account_number": "39847120056", "ifsc": "KKBK0008123", "bank_name": "Kotak Mahindra Bank, Whitefield"}
BANK_NOTES = [
    "Please note: we have moved our banking to a new account. All future payments should be made to the account below only.",
    "Due to an internal audit our previous account is frozen. Kindly remit this invoice to our new bank account mentioned below.",
]


def available(po):
    """Received-but-not-yet-billed quantity per PO line, from the master GRN + invoice ledger."""
    rec, billed = {}, {}
    for g in M.GRNS:
        if g["po_number"] == po["po_number"]:
            for l in g["lines"]:
                rec[l["line"]] = rec.get(l["line"], 0) + l["received"]
    for inv in M.INVOICE_LEDGER:
        if inv["po_number"] == po["po_number"]:
            for l in inv["lines"]:
                billed[l["line"]] = billed.get(l["line"], 0) + l["qty"]
    return {ln["line"]: rec.get(ln["line"], 0) - billed.get(ln["line"], 0) for ln in po["lines"]}


def grn_date(po_number):
    ds = [g["date"] for g in M.GRNS if g["po_number"] == po_number]
    return max(date.fromisoformat(d) for d in ds) if ds else None


def inv_no(rnd, vid):
    n = rnd.randint(500, 899)
    return {"V001": f"SGS/26-27/{n:04d}", "V002": f"VPS/2026/{n}", "V003": f"NIT/26-27/{n:04d}", "V004": f"DE/{1200 + n}",
            "V005": f"PTW/2627/{n:04d}", "V006": f"CLPL/26/{n:04d}"}.get(vid, f"INV-{n}/26-27")


def open_pos(vid=None):
    return [p for p in M.PURCHASE_ORDERS if p["status"] == "open" and (vid is None or p["vendor_id"] == vid)
            and M.vendor_by_id(p["vendor_id"])["status"] == "active"]


def pick_lines(rnd, po, frac=(0.3, 1.0), need_avail=True):
    av = available(po)
    cands = [l for l in po["lines"] if av[l["line"]] > 0] if need_avail else list(po["lines"])
    k = rnd.randint(1, len(cands))
    chosen = rnd.sample(cands, k)
    out = []
    for l in sorted(chosen, key=lambda x: x["line"]):
        cap = av[l["line"]] if need_avail else l["qty"]
        q = max(1, int(round(cap * rnd.uniform(*frac))))
        out.append({"po_line": l, "qty": q, "rate": l["unit_price"]})
    return out


def as_case_lines(rnd, sel):
    return [(rnd.choice(REWORD[s["po_line"]["description"]]), s["po_line"]["hsn"], s["qty"], s["po_line"]["unit"], s["rate"]) for s in sel]


def inv_date(rnd, po_number):
    lo = grn_date(po_number) or date(2026, 11, 1)
    lo = max(lo, date(2026, 11, 1))
    return (lo + timedelta(days=rnd.randint(0, max(0, (date(2026, 11, 19) - lo).days)))).isoformat()


def build(rnd, scenario, idx):
    """Return one case dict. `expected` is set by the scenario's policy, independent of the rules engine."""
    s = scenario
    case = {"id": f"H{idx:02d}_{s}", "kind": "holdout", "scanned": rnd.random() < 0.3}
    if s in ("clean", "price_minor", "price_major", "no_po", "tax_inclusive", "bank_change", "arithmetic", "missing_inv_no", "gstin_typo", "wrong_tax_type"):
        pool = open_pos()
        if s == "tax_inclusive":
            pool = [p for p in pool if p["vendor_id"] in ("V001", "V004")]          # small KA traders quote GST-inclusive
        if s == "wrong_tax_type":
            pool = [p for p in pool if M.vendor_by_id(p["vendor_id"])["gstin"][:2] != M.BUYER["gstin"][:2]]
        if s == "bank_change":
            pool = [p for p in pool if p["vendor_id"] != "V003"]                    # dev set already used V003
        po = rnd.choice(pool)
        sel = pick_lines(rnd, po)
        if s == "price_minor":
            sel[0]["rate"] = round(sel[0]["rate"] * (1 + rnd.uniform(0.004, 0.016)), 2)
        if s == "price_major":
            sel[0]["rate"] = round(sel[0]["rate"] * (1 + rnd.uniform(0.06, 0.15)), 2)
        case.update(vendor=po["vendor_id"], po=None if s == "no_po" else po["po_number"], date=inv_date(rnd, po["po_number"]),
                    invoice_number=None if s == "missing_inv_no" else inv_no(rnd, po["vendor_id"]), lines=as_case_lines(rnd, sel))
        if s == "no_po":
            case["po_hint"] = rnd.choice(["Ref: your verbal order", f"Against your order dt. {po['date'][8:]}/{po['date'][5:7]}", "Delivery challan enclosed"])
        if s == "tax_inclusive":
            case["tax_inclusive"] = True
        if s == "bank_change":
            v = M.vendor_by_id(po["vendor_id"])
            case["bank_override"] = {**NEW_BANK, "account_name": v["name"]}
            case["note"] = rnd.choice(BANK_NOTES)
        if s == "arithmetic":
            l0 = case["lines"][0]
            case["line_amount_override"] = {0: round(l0[2] * l0[4] + rnd.choice([-1, 1]) * rnd.choice([900, 1800, 10000]), 2)}
        if s == "gstin_typo":
            case["gstin_typo"] = True
        if s == "wrong_tax_type":
            case["force_tax_type"] = "CGST+SGST"
        exp_po = po["po_number"]
        decision = {"clean": "APPROVED", "price_minor": "APPROVED", "price_major": "ON_HOLD", "no_po": "APPROVED",
                    "tax_inclusive": "APPROVED", "bank_change": "ON_HOLD", "arithmetic": "REJECTED",
                    "missing_inv_no": "REJECTED", "gstin_typo": "REJECTED", "wrong_tax_type": "REJECTED"}[s]
    elif s == "over_receipt":                       # bill more than the warehouse received → pay received part only
        po = M.po_by_number("PO-2026-0171")
        l = po["lines"][1]
        av = available(po)[2]
        case.update(vendor="V005", po=po["po_number"], date=inv_date(rnd, po["po_number"]), invoice_number=inv_no(rnd, "V005"),
                    lines=[(rnd.choice(REWORD[l["description"]]), l["hsn"], av + rnd.randint(10, l["qty"] - av), l["unit"], l["unit_price"])])
        exp_po, decision = po["po_number"], "PARTIALLY_APPROVED"
    elif s == "duplicate":                          # a ledger invoice re-sent, number re-formatted
        led = rnd.choice([x for x in M.INVOICE_LEDGER if x["vendor_id"] in ("V002", "V004")])
        po = M.po_by_number(led["po_number"])
        n = led["invoice_number"]
        n2 = rnd.choice([n.replace("/", "-"), n.replace("/", " / "), n])
        lines = [(rnd.choice(REWORD[po["lines"][x["line"] - 1]["description"]]), po["lines"][x["line"] - 1]["hsn"], x["qty"],
                  po["lines"][x["line"] - 1]["unit"], po["lines"][x["line"] - 1]["unit_price"]) for x in led["lines"]]
        case.update(vendor=led["vendor_id"], po=po["po_number"], invoice_number=n2,
                    date=(date.fromisoformat(led["invoice_date"]) + timedelta(days=rnd.randint(0, 30))).isoformat(), lines=lines)
        exp_po, decision = po["po_number"], "REJECTED"
    elif s == "blocked":
        po = M.po_by_number("PO-2026-0180")
        l = po["lines"][0]
        case.update(vendor="V006", po=po["po_number"], date=f"2026-11-{rnd.randint(2, 18):02d}", invoice_number=inv_no(rnd, "V006"),
                    lines=[(rnd.choice(REWORD[l["description"]]), l["hsn"], 1, l["unit"], l["unit_price"])])
        exp_po, decision = po["po_number"], "ON_HOLD"
    elif s == "unknown_vendor":
        u = rnd.choice(NEW_UNKNOWN)
        key = "X_" + u["name"].split()[0].upper()
        C.UNKNOWN_VENDORS[key] = {k: u[k] for k in ("name", "state", "pan", "address", "bank", "email")}
        case.update(vendor=key, po=None, date=f"2026-11-{rnd.randint(2, 18):02d}", invoice_number=f"{u['name'][:2].upper()}/{rnd.randint(100, 999)}/26-27",
                    lines=[(d, h, q, un, r) for d, h, q, un, r in u["lines"]])
        exp_po, decision = None, "ON_HOLD"
    else:
        raise ValueError(s)
    case["scenario"] = s
    case["title"] = s.replace("_", " ")
    case["expected"] = {"decision": decision, "po": exp_po}
    return case


# ───────── a layout never used during development ─────────
def tpl_ledger(c, case, v, t):
    """Right-aligned header, 'Bill No.' wording, dd.mm.yyyy dates, amounts without thousands separators, tax per line."""
    xr = W - 14 * mm
    c.setFillColor(colors.HexColor("#1F3A5F"))
    c.rect(0, H - 34 * mm, W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Times-Bold", 17)
    c.drawRightString(xr, H - 15 * mm, v["name"])
    c.setFont("Times-Roman", 8.5)
    c.drawRightString(xr, H - 21 * mm, v["address"])
    c.drawRightString(xr, H - 26 * mm, f"GST Regn No. {v['gstin']}  ·  {v['email']}")
    c.setFont("Times-Bold", 22)
    c.drawString(14 * mm, H - 22 * mm, "BILL")
    c.setFillColor(colors.black)
    y = H - 44 * mm
    c.setFont("Times-Roman", 9.5)
    y0, mth = case["date"].split("-")[0], case["date"].split("-")
    meta = []
    if case["invoice_number"]:
        meta.append(("Bill No.", case["invoice_number"]))
    meta.append(("Dated", f"{mth[2]}.{mth[1]}.{mth[0]}"))
    if case.get("po"):
        meta.append(("P.O. Ref", case["po"]))
    elif case.get("po_hint"):
        meta.append(("Reference", case["po_hint"]))
    for i, (k, val) in enumerate(meta):
        c.drawString(130 * mm, y - i * 5 * mm, f"{k}:")
        c.drawString(152 * mm, y - i * 5 * mm, str(val))
    c.setFont("Times-Bold", 9.5)
    c.drawString(14 * mm, y, "Consignee / Buyer")
    c.setFont("Times-Roman", 9)
    c.drawString(14 * mm, y - 5 * mm, M.BUYER["name"])
    c.drawString(14 * mm, y - 10 * mm, "Plot 42, KIADB Industrial Area, Peenya, Bengaluru 560058")
    c.drawString(14 * mm, y - 15 * mm, f"GST Regn No. {M.BUYER['gstin']}   State: Karnataka, Code 29")
    incl = case.get("tax_inclusive")
    rows = []
    for i, l in enumerate(t["lines"]):
        r = round(l["rate"] * 1.18, 2) if incl else l["rate"]
        amt = round(r * l["qty"], 2) if incl else l["amount"]
        rows.append((l["hsn"], l["desc"], f"{l['qty']} {l['unit']}", f"{r:.2f}", "18%", f"{amt:.2f}"))
    hdr = ["HSN/SAC", "Item", "Quantity", "Rate incl. tax" if incl else "Rate", "GST", "Value"]
    y = _table(c, 14 * mm, y - 26 * mm, hdr, rows, [20 * mm, 78 * mm, 24 * mm, 24 * mm, 12 * mm, 24 * mm])
    y -= 7 * mm
    c.setFont("Times-Roman", 9.5)
    summ = [("Assessable value", t["taxable"])]
    if t["tax_type"] == "IGST":
        summ.append(("Integrated tax (IGST) 18%", t["igst"]))
    else:
        summ += [("Central tax (CGST) 9%", t["cgst"]), ("State tax (SGST) 9%", t["sgst"])]
    if incl:
        c.drawString(14 * mm, y, "Note: rates above include GST; tax shown below is back-calculated.")
    for k, val in summ:
        c.drawString(120 * mm, y, k)
        c.drawRightString(xr, y, f"{val:.2f}")
        y -= 5 * mm
    c.setFont("Times-Bold", 11)
    c.drawString(120 * mm, y - 2 * mm, "Bill Amount (INR)")
    c.drawRightString(xr, y - 2 * mm, f"{t['total']:.2f}")
    c.setFont("Times-Italic", 8.5)
    c.drawString(14 * mm, y - 10 * mm, amount_words(t["total"]))
    y -= 20 * mm
    if case.get("note"):
        c.setFont("Times-Bold", 9)
        words, line, ly = case["note"].split(), "", y
        for w in words:
            if c.stringWidth(line + " " + w, "Times-Bold", 9) > W - 28 * mm:
                c.drawString(14 * mm, ly, line)
                ly -= 4.5 * mm
                line = w
            else:
                line = (line + " " + w).strip()
        c.drawString(14 * mm, ly, line)
        y = ly - 8 * mm
    b = v["bank"]
    c.setFont("Times-Bold", 9)
    c.drawString(14 * mm, y, "Payment to:")
    c.setFont("Times-Roman", 9)
    c.drawString(14 * mm, y - 5 * mm, f"{b['account_name']} · {b['bank_name']}")
    c.drawString(14 * mm, y - 10 * mm, f"Account {b['account_number']} · IFS Code {b['ifsc']}")
    c.setFont("Times-Roman", 8)
    c.drawString(14 * mm, 20 * mm, "This is a computer generated bill.")
    c.drawRightString(xr, 20 * mm, f"for {v['name']}")


def _table(c, x, y, cols, rows, widths):
    rh = 7 * mm
    c.setFont("Times-Bold", 9)
    cx = x
    for col, w in zip(cols, widths):
        c.drawString(cx + 1.5 * mm, y - rh + 2.4 * mm, col)
        cx += w
    c.setLineWidth(0.8)
    c.line(x, y - rh, x + sum(widths), y - rh)
    y -= rh
    c.setFont("Times-Roman", 9)
    for row in rows:
        cx = x
        for i, (val, w) in enumerate(zip(row, widths)):
            if i >= 3:
                c.drawRightString(cx + w - 1.5 * mm, y - rh + 2.4 * mm, str(val))
            else:
                c.drawString(cx + 1.5 * mm, y - rh + 2.4 * mm, str(val))
            cx += w
        y -= rh
    c.setLineWidth(0.4)
    c.line(x, y, x + sum(widths), y)
    return y


def pick_template(rnd, case, v):
    """Mostly the unseen layout; otherwise a development layout that can render this scenario."""
    if rnd.random() < 0.6:
        return "ledger"
    ka = v["gstin"][:2] == "29"
    ok = ["classic"]
    if not case.get("note") and not case.get("po_hint") and not case.get("tax_inclusive"):
        ok.append("modern")
    if ka and case.get("invoice_number") and not case.get("note") and not case.get("force_tax_type"):
        ok.append("shop")
    if not ka and case.get("invoice_number") and not case.get("po_hint") and not case.get("tax_inclusive"):
        ok.append("services")
    if case.get("tax_inclusive") or case.get("po_hint") or case.get("note"):
        ok = [o for o in ok if o in ("shop", "services") and (o != "services" or not case.get("po_hint"))] or ["ledger"]
    return rnd.choice(ok)


SCENARIOS = ["clean", "clean", "clean", "price_minor", "price_major", "over_receipt", "no_po", "no_po", "tax_inclusive",
             "bank_change", "bank_change", "arithmetic", "missing_inv_no", "gstin_typo", "wrong_tax_type", "duplicate",
             "duplicate", "blocked", "unknown_vendor", "unknown_vendor", "clean", "price_major", "arithmetic", "no_po"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=2027)
    ap.add_argument("--n", type=int, default=len(SCENARIOS))
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.pdf"):
        p.unlink()
    scen = SCENARIOS[:a.n]
    rnd.shuffle(scen)
    truth = []
    for i, s in enumerate(scen, 1):
        case = build(rnd, s, i)
        v = vendor_profile(case)
        if case.get("bank_override"):
            v["bank"] = case["bank_override"]
        if case.get("gstin_typo"):
            g = v["gstin"]
            v["gstin"] = g[:14] + ("X" if g[14] != "X" else "Y")
        t = compute(case, v["gstin"])
        case["template"] = pick_template(rnd, case, v)
        buf = io.BytesIO()
        cv = canvas.Canvas(buf, pagesize=A4)
        (tpl_ledger if case["template"] == "ledger" else TEMPLATES[case["template"]])(cv, case, v, t)
        cv.showPage()
        cv.save()
        data = buf.getvalue()
        if case["scanned"]:
            data = scanify(data, seed=a.seed + i)
        (OUT / f"{case['id']}.pdf").write_bytes(data)
        case["expected"]["total"] = t["total"]
        truth.append({**case, "file": f"{case['id']}.pdf", "vendor_gstin": v["gstin"], "taxable": t["taxable"], "total": t["total"],
                      "lines": [list(x) for x in case["lines"]],
                      "line_amount_override": {str(k): x for k, x in case.get("line_amount_override", {}).items()} or None,
                      "unknown_vendor": C.UNKNOWN_VENDORS.get(case["vendor"]) if case["vendor"].startswith("X_") else None})
        print(f"{case['id']:<26} {case['template']:<8} {'scan' if case['scanned'] else 'text'}  {case['vendor']:<10} "
              f"{case['expected']['decision']:<19} ₹{t['total']:,.2f}")
    (OUT / "cases.json").write_text(json.dumps(truth, indent=1))


if __name__ == "__main__":
    main()
