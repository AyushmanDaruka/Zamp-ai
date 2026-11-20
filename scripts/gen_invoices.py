"""Render the test invoices in scripts/cases.py to PDFs (4 vendor layouts, optional 'scan' degradation).

Usage:  .venv/bin/python -m scripts.gen_invoices
Writes: samples/<case_id>.pdf and samples/cases.json (ground truth)
"""
import io
import json
import random
from pathlib import Path

import pymupdf as fitz
import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app.gst import make_gstin
from app.master import BUYER, vendor_by_id
from scripts.cases import CASES, UNKNOWN_VENDORS

OUT = Path(__file__).resolve().parent.parent / "samples"
W, H = A4


# ───────────────────────── helpers ─────────────────────────
def inr(x):
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
    return ("-" if neg else "") + f"{s}.{int(frac):02d}"


_ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen".split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _words(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " Hundred" + ("" if n % 100 == 0 else " " + _words(n % 100))
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if n >= div:
            return _words(n // div) + " " + name + ("" if n % div == 0 else " " + _words(n % div))
    return ""


def amount_words(x):
    rupees, paise = divmod(round(x * 100), 100)
    s = "Rupees " + _words(int(rupees))
    if paise:
        s += " and " + _words(int(paise)) + " Paise"
    return s + " Only"


def compute(case, vendor_gstin):
    lines = []
    for i, (desc, hsn, qty, unit, rate) in enumerate(case["lines"]):
        amt = round(qty * rate, 2)
        printed = case.get("line_amount_override", {}).get(i, amt)
        lines.append({"desc": desc, "hsn": hsn, "qty": qty, "unit": unit, "rate": rate, "amount": printed})
    taxable = round(sum(l["amount"] for l in lines), 2)
    intra = vendor_gstin[:2] == BUYER["gstin"][:2]
    tax_type = case.get("force_tax_type") or ("CGST+SGST" if intra else "IGST")
    tax = round(taxable * 0.18, 2)
    if tax_type == "CGST+SGST":
        cgst = sgst = round(tax / 2, 2)
        igst = 0.0
    else:
        cgst = sgst = 0.0
        igst = tax
    total = round(taxable + cgst + sgst + igst, 2)
    return {"lines": lines, "taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst,
            "tax_type": tax_type, "total": total}


def vendor_profile(case):
    vid = case["vendor"]
    if vid in UNKNOWN_VENDORS:
        u = UNKNOWN_VENDORS[vid]
        return {"name": u["name"], "gstin": make_gstin(u["state"], u["pan"]), "address": u["address"],
                "bank": u["bank"], "email": u["email"], "phone": "+91 80 2337 1188"}
    v = vendor_by_id(vid)
    addr = {
        "V001": "No. 7, 2nd Main, Peenya 2nd Stage, Bengaluru 560058, Karnataka",
        "V002": "SIPCOT Industrial Complex, Phase II, Hosur 635109, Tamil Nadu",
        "V003": "Unit 504, Kanakia Wall Street, Andheri-Kurla Road, Mumbai 400093, Maharashtra",
        "V004": "Shop 14, Peenya Industrial Area 1st Stage, Bengaluru 560058",
        "V005": "Plot 221, GIDC Lodhika, Metoda, Rajkot 360021, Gujarat",
        "V006": "Door 31/1120, Willingdon Island, Kochi 682003, Kerala",
    }[vid]
    gstin = v["gstin"]
    return {"name": v["name"], "gstin": gstin, "address": addr, "bank": dict(v["bank"]),
            "email": v["contact"]["email"], "phone": v["contact"]["phone"]}


def fmt_date(iso, style):
    y, m, d = iso.split("-")
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    if style == "dmy":
        return f"{d}/{m}/{y}"
    if style == "dmy-":
        return f"{d}-{m}-{y}"
    return f"{int(d)} {months[int(m) - 1]} {y}"


# ───────────────────────── templates ─────────────────────────
def draw_table(c, x, y, cols, rows, widths, font="Helvetica", size=8.5, head_fill=colors.HexColor("#EEEEEE"), aligns=None):
    rh = 6.5 * mm
    c.setFont({"Times-Roman": "Times-Bold"}.get(font, font + "-Bold"), size)
    c.setFillColor(head_fill)
    c.rect(x, y - rh, sum(widths), rh, stroke=1, fill=1)
    c.setFillColor(colors.black)
    cx = x
    for col, w in zip(cols, widths):
        c.drawString(cx + 1.5 * mm, y - rh + 2.2 * mm, col)
        cx += w
    y -= rh
    c.setFont(font, size)
    for row in rows:
        c.rect(x, y - rh, sum(widths), rh, stroke=1, fill=0)
        cx = x
        for i, (val, w) in enumerate(zip(row, widths)):
            a = (aligns or ["l"] * len(row))[i]
            if a == "r":
                c.drawRightString(cx + w - 1.5 * mm, y - rh + 2.2 * mm, str(val))
            else:
                c.drawString(cx + 1.5 * mm, y - rh + 2.2 * mm, str(val))
            cx += w
        y -= rh
    return y


def tpl_classic(c, case, v, t):
    """Traditional Indian trader invoice: centred header, boxed table, tax summary, amount in words."""
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W / 2, H - 20 * mm, v["name"].upper())
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(W / 2, H - 25 * mm, v["address"])
    c.drawCentredString(W / 2, H - 29 * mm, f"GSTIN: {v['gstin']}   |   Ph: {v['phone']}   |   {v['email']}")
    c.setLineWidth(1)
    c.line(15 * mm, H - 32 * mm, W - 15 * mm, H - 32 * mm)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, H - 38 * mm, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    y = H - 46 * mm
    if case["invoice_number"]:
        c.drawString(15 * mm, y, f"Invoice No.: {case['invoice_number']}")
    c.drawString(15 * mm, y - 5 * mm, f"Invoice Date: {fmt_date(case['date'], 'dmy')}")
    if case.get("po"):
        c.drawString(15 * mm, y - 10 * mm, f"Buyer's Order No.: {case['po']}")
    c.drawString(15 * mm, y - 15 * mm, "Place of Supply: Karnataka (29)")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(110 * mm, y, "Billed To:")
    c.setFont("Helvetica", 8.5)
    c.drawString(110 * mm, y - 5 * mm, BUYER["name"])
    c.drawString(110 * mm, y - 9.5 * mm, "Plot 42, KIADB Industrial Area, Peenya")
    c.drawString(110 * mm, y - 14 * mm, "Bengaluru 560058, Karnataka")
    c.drawString(110 * mm, y - 18.5 * mm, f"GSTIN: {BUYER['gstin']}")
    rows = [(i + 1, l["desc"], l["hsn"], f"{l['qty']:,}", l["unit"], inr(l["rate"]), inr(l["amount"])) for i, l in enumerate(t["lines"])]
    y = draw_table(c, 15 * mm, y - 26 * mm, ["#", "Description of Goods", "HSN", "Qty", "Unit", "Rate", "Amount"], rows,
                   [8 * mm, 70 * mm, 17 * mm, 20 * mm, 13 * mm, 23 * mm, 29 * mm], aligns=["l", "l", "l", "r", "l", "r", "r"])
    xr = W - 15 * mm
    y -= 6 * mm
    summary = [("Taxable Value", t["taxable"])]
    if t["tax_type"] == "CGST+SGST":
        summary += [("CGST @ 9%", t["cgst"]), ("SGST @ 9%", t["sgst"])]
    else:
        summary += [("IGST @ 18%", t["igst"])]
    for label, val in summary:
        c.drawString(125 * mm, y, label)
        c.drawRightString(xr, y, inr(val))
        y -= 5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(125 * mm, y - 1 * mm, "Grand Total (Rs.)")
    c.drawRightString(xr, y - 1 * mm, inr(t["total"]))
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(15 * mm, y - 9 * mm, "Amount in words: " + amount_words(t["total"]))
    bank_block(c, 15 * mm, y - 22 * mm, v["bank"], "Bank Details")
    c.setFont("Helvetica", 8)
    c.drawString(15 * mm, 30 * mm, "Terms: Goods once sold will not be taken back. Interest @18% p.a. on overdue bills. Subject to Bengaluru jurisdiction.")
    c.drawRightString(xr, 22 * mm, f"For {v['name']}")
    c.drawRightString(xr, 12 * mm, "Authorised Signatory")


def tpl_modern(c, case, v, t):
    """Modern SaaS-style invoice: colour band, per-line tax columns, invoice no on right."""
    band = colors.HexColor("#1F3A5F")
    c.setFillColor(band)
    c.rect(0, H - 34 * mm, W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(15 * mm, H - 16 * mm, v["name"])
    c.setFont("Helvetica", 8)
    c.drawString(15 * mm, H - 21.5 * mm, v["address"])
    c.drawString(15 * mm, H - 26 * mm, f"GSTIN {v['gstin']}  ·  {v['email']}")
    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(W - 15 * mm, H - 18 * mm, "INVOICE")
    c.setFillColor(colors.black)
    y = H - 46 * mm
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(15 * mm, y, "BILL TO")
    c.drawString(120 * mm, y, "INVOICE DETAILS")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15 * mm, y - 5 * mm, BUYER["name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(15 * mm, y - 9.5 * mm, "Plot 42, KIADB Industrial Area, Peenya")
    c.drawString(15 * mm, y - 14 * mm, "Bengaluru 560058, Karnataka")
    c.drawString(15 * mm, y - 18.5 * mm, f"GSTIN {BUYER['gstin']}")
    details = []
    if case["invoice_number"]:
        details.append(("Invoice #", case["invoice_number"]))
    details += [("Issued", fmt_date(case["date"], "long"))]
    if case.get("po"):
        details.append(("PO Ref", case["po"]))
    details.append(("Terms", "Net 45" if case["vendor"] == "V002" else "Net 60"))
    for i, (k, val) in enumerate(details):
        c.drawString(120 * mm, y - (5 + 4.5 * i) * mm, k)
        c.drawRightString(W - 15 * mm, y - (5 + 4.5 * i) * mm, val)
    rate_lbl = "IGST" if t["tax_type"] == "IGST" else "CGST+SGST"
    rows = []
    for l in t["lines"]:
        rows.append((f"{l['desc']} (HSN {l['hsn']})", f"{l['qty']:,} {l['unit']}", inr(l["rate"]), "18%", inr(l["amount"])))
    y = draw_table(c, 15 * mm, y - 32 * mm, ["Item", "Qty", "Unit Price", rate_lbl, "Line Total"], rows,
                   [82 * mm, 24 * mm, 26 * mm, 22 * mm, 26 * mm], head_fill=colors.HexColor("#DCE6F2"),
                   aligns=["l", "r", "r", "r", "r"])
    y -= 7 * mm
    xr = W - 15 * mm
    c.drawString(130 * mm, y, "Subtotal")
    c.drawRightString(xr, y, "INR " + inr(t["taxable"]))
    y -= 5 * mm
    if t["tax_type"] == "IGST":
        c.drawString(130 * mm, y, "IGST 18%")
        c.drawRightString(xr, y, "INR " + inr(t["igst"]))
        y -= 5 * mm
    else:
        c.drawString(130 * mm, y, "CGST 9%")
        c.drawRightString(xr, y, "INR " + inr(t["cgst"]))
        y -= 5 * mm
        c.drawString(130 * mm, y, "SGST 9%")
        c.drawRightString(xr, y, "INR " + inr(t["sgst"]))
        y -= 5 * mm
    c.setFillColor(band)
    c.rect(128 * mm, y - 4 * mm, xr - 128 * mm + 2 * mm, 8 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(130 * mm, y - 1.5 * mm, "Amount Due")
    c.drawRightString(xr, y - 1.5 * mm, "INR " + inr(t["total"]))
    c.setFillColor(colors.black)
    bank_block(c, 15 * mm, y - 18 * mm, v["bank"], "Payment Information")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(15 * mm, 15 * mm, "This is a computer generated invoice. Thank you for your business.")


def tpl_services(c, case, v, t):
    """Service-provider invoice: SAC codes, IGST, prominent 'Remit to', free-text note."""
    c.setFont("Times-Bold", 18)
    c.drawString(15 * mm, H - 20 * mm, v["name"])
    c.setFont("Times-Roman", 9)
    c.drawString(15 * mm, H - 25.5 * mm, v["address"])
    c.drawString(15 * mm, H - 30 * mm, f"GSTIN/UIN: {v['gstin']}    PAN: {v['gstin'][2:12]}")
    c.setFont("Times-Bold", 13)
    c.drawRightString(W - 15 * mm, H - 20 * mm, "TAX INVOICE (Services)")
    c.setFont("Times-Roman", 9.5)
    y = H - 42 * mm
    c.drawString(15 * mm, y, f"Customer: {BUYER['name']}")
    c.drawString(15 * mm, y - 5 * mm, f"Customer GSTIN: {BUYER['gstin']}")
    c.drawString(15 * mm, y - 10 * mm, "Place of supply: 29 - Karnataka")
    c.drawRightString(W - 15 * mm, y, f"Invoice No: {case['invoice_number']}")
    c.drawRightString(W - 15 * mm, y - 5 * mm, f"Date: {fmt_date(case['date'], 'dmy-')}")
    if case.get("po"):
        c.drawRightString(W - 15 * mm, y - 10 * mm, f"Your PO: {case['po']}")
    rows = [(l["desc"], l["hsn"], f"{l['qty']} {l['unit']}", inr(l["rate"]), inr(l["amount"])) for l in t["lines"]]
    y = draw_table(c, 15 * mm, y - 20 * mm, ["Particulars", "SAC", "Qty", "Rate", "Taxable Value"], rows,
                   [84 * mm, 20 * mm, 22 * mm, 26 * mm, 28 * mm], font="Times-Roman", size=9.5,
                   aligns=["l", "l", "r", "r", "r"])
    y -= 7 * mm
    xr = W - 15 * mm
    for label, val in [("Total Taxable Value", t["taxable"]),
                       (("IGST @ 18%" if t["tax_type"] == "IGST" else "CGST+SGST @ 18%"), t["igst"] or (t["cgst"] + t["sgst"]))]:
        c.drawString(120 * mm, y, label)
        c.drawRightString(xr, y, inr(val))
        y -= 5.5 * mm
    c.setFont("Times-Bold", 11)
    c.drawString(120 * mm, y, "Invoice Total (INR)")
    c.drawRightString(xr, y, inr(t["total"]))
    c.setFont("Times-Italic", 9)
    c.drawString(15 * mm, y - 8 * mm, amount_words(t["total"]))
    y -= 18 * mm
    if case.get("note"):
        c.setFillColor(colors.HexColor("#FFF4CC"))
        c.rect(15 * mm, y - 12 * mm, W - 30 * mm, 14 * mm, stroke=0, fill=1)
        c.setFillColor(colors.black)
        c.setFont("Times-Bold", 9)
        words, line, ly = case["note"].split(), "", y - 2 * mm
        for w in words:
            if c.stringWidth(line + " " + w, "Times-Bold", 9) > W - 36 * mm:
                c.drawString(18 * mm, ly, line.strip())
                ly -= 4.5 * mm
                line = ""
            line += " " + w
        c.drawString(18 * mm, ly, line.strip())
        y -= 18 * mm
    bank_block(c, 15 * mm, y, v["bank"], "REMIT TO", font="Times")
    c.setFont("Times-Roman", 8.5)
    c.drawString(15 * mm, 25 * mm, "Payment due within 30 days. TDS, if any, to be deducted under the applicable section and certificate shared.")
    c.drawRightString(W - 15 * mm, 14 * mm, f"for {v['name']} — Authorised Signatory")


def tpl_shop(c, case, v, t):
    """Small-trader bill: plain, cramped, ambiguous reference instead of PO, optional tax-inclusive rates."""
    c.setFont("Courier-Bold", 15)
    c.drawString(15 * mm, H - 18 * mm, v["name"].upper())
    c.setFont("Courier", 8.5)
    c.drawString(15 * mm, H - 23 * mm, v["address"])
    c.drawString(15 * mm, H - 27 * mm, f"GSTIN : {v['gstin']}   Mob: {v['phone']}")
    c.setDash(2, 2)
    c.line(15 * mm, H - 30 * mm, W - 15 * mm, H - 30 * mm)
    c.setDash()
    c.setFont("Courier-Bold", 11)
    c.drawString(15 * mm, H - 37 * mm, "TAX INVOICE / CASH-CREDIT BILL")
    c.setFont("Courier", 9)
    y = H - 45 * mm
    c.drawString(15 * mm, y, f"Bill No : {case['invoice_number']}")
    c.drawString(110 * mm, y, f"Date : {fmt_date(case['date'], 'dmy')}")
    c.drawString(15 * mm, y - 5 * mm, f"M/s {BUYER['name']}, Peenya, Bengaluru")
    c.drawString(15 * mm, y - 10 * mm, f"Party GSTIN : {BUYER['gstin']}")
    if case.get("po"):
        c.drawString(15 * mm, y - 15 * mm, f"Order Ref : {case['po']}")
    elif case.get("po_hint"):
        c.drawString(15 * mm, y - 15 * mm, case["po_hint"])
    incl = case.get("tax_inclusive")
    rows = []
    for i, l in enumerate(t["lines"]):
        if incl:
            r = round(l["rate"] * 1.18, 2)
            rows.append((i + 1, l["desc"], l["hsn"], l["qty"], inr(r), inr(round(r * l["qty"], 2))))
        else:
            rows.append((i + 1, l["desc"], l["hsn"], l["qty"], inr(l["rate"]), inr(l["amount"])))
    hdr = ["Sl", "Particulars", "HSN", "Qty", "Rate(incl GST)" if incl else "Rate", "Amount"]
    y = draw_table(c, 15 * mm, y - 22 * mm, hdr, rows, [9 * mm, 72 * mm, 17 * mm, 15 * mm, 30 * mm, 37 * mm],
                   font="Courier", size=8.5, head_fill=colors.white, aligns=["l", "l", "l", "r", "r", "r"])
    y -= 6 * mm
    xr = W - 15 * mm
    c.setFont("Courier", 9)
    if incl:
        c.drawString(15 * mm, y, "** Rates are inclusive of GST **")
        y -= 6 * mm
        for label, val in [("Taxable Amt", t["taxable"]), ("CGST 9%", t["cgst"]), ("SGST 9%", t["sgst"])]:
            c.drawString(110 * mm, y, label)
            c.drawRightString(xr, y, inr(val))
            y -= 5 * mm
    else:
        for label, val in [("Sub Total", t["taxable"]), ("CGST 9%", t["cgst"]), ("SGST 9%", t["sgst"])]:
            c.drawString(110 * mm, y, label)
            c.drawRightString(xr, y, inr(val))
            y -= 5 * mm
    c.setFont("Courier-Bold", 10.5)
    c.drawString(110 * mm, y - 1 * mm, "NET AMOUNT")
    c.drawRightString(xr, y - 1 * mm, inr(t["total"]))
    c.setFont("Courier", 8.5)
    c.drawString(15 * mm, y - 9 * mm, amount_words(t["total"]))
    b = v["bank"]
    c.drawString(15 * mm, y - 18 * mm, f"Bank: {b['bank_name']}  A/c {b['account_number']}  IFSC {b['ifsc']}")
    c.drawString(15 * mm, 22 * mm, "E.& O.E.        Customer Signature                    For " + v["name"])


def bank_block(c, x, y, bank, title, font="Helvetica"):
    c.setFont(font + "-Bold", 9)
    c.drawString(x, y, title)
    c.setFont(font if font != "Times" else "Times-Roman", 8.5)
    c.drawString(x, y - 4.5 * mm, f"A/c Name: {bank['account_name']}")
    c.drawString(x, y - 9 * mm, f"A/c No: {bank['account_number']}")
    c.drawString(x, y - 13.5 * mm, f"IFSC: {bank['ifsc']}   Bank: {bank['bank_name']}")


TEMPLATES = {"classic": tpl_classic, "modern": tpl_modern, "services": tpl_services, "shop": tpl_shop}


def scanify(pdf_bytes, seed):
    """Rasterise → grey → skew → blur → noise → JPEG. Result has NO text layer, like a phone/office scan."""
    rnd = random.Random(seed)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=130)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = img.rotate(rnd.uniform(-1.6, 1.6), resample=Image.BICUBIC, expand=False, fillcolor=245)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    arr = np.asarray(img).astype(np.float32)
    arr = arr * 0.92 + 12 + np.random.default_rng(seed).normal(0, 9, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    out = fitz.open()
    page = out.new_page(width=W, height=H)
    page.insert_image(page.rect, stream=buf.getvalue())
    return out.tobytes(deflate=True)


def main():
    OUT.mkdir(exist_ok=True)
    truth = []
    for n, case in enumerate(CASES):
        v = vendor_profile(case)
        if case.get("bank_override"):
            v["bank"] = case["bank_override"]
        if case.get("gstin_typo"):
            g = v["gstin"]
            v["gstin"] = g[:14] + ("X" if g[14] != "X" else "Y")
        t = compute(case, v["gstin"])
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.setTitle(f"Invoice {case['invoice_number'] or ''}")
        TEMPLATES[case["template"]](c, case, v, t)
        c.showPage()
        c.save()
        data = buf.getvalue()
        if case["scanned"]:
            data = scanify(data, seed=n + 7)
        (OUT / f"{case['id']}.pdf").write_bytes(data)
        truth.append({"id": case["id"], "kind": case["kind"], "title": case["title"], "file": f"{case['id']}.pdf",
                      "invoice_number": case["invoice_number"], "vendor_gstin": v["gstin"], "date": case["date"],
                      "taxable": t["taxable"], "total": t["total"], "scanned": case["scanned"], **{"expected": case["expected"]}})
        assert abs(t["total"] - case["expected"]["total"]) < 0.01, (case["id"], t["total"])
        print(f"{case['id']:<32} {case['template']:<9} {'scan' if case['scanned'] else 'text'}  total {inr(t['total'])}")
    (OUT / "cases.json").write_text(json.dumps(truth, indent=2))


if __name__ == "__main__":
    main()
