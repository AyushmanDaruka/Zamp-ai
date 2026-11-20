"""Stage 1–2: read the document.

Design principle: the LLM is a *reader*, never a *decider*. It turns pixels/text into fields.
Everything it returns is then checked by code that doesn't trust it:
  • text PDFs  → every critical value must literally exist in the PDF text layer (grounding)
  • scans      → an independent second read of the critical fields must agree (dual-read consensus)
  • always     → the invoice's own arithmetic must reconcile (lines → subtotal → tax → total)
"""
import base64
import json
import os
import re
import time

import httpx
import pymupdf

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _models():
    raw = os.environ.get("GEMINI_MODELS") or "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash"
    return [m.strip() for m in raw.split(",") if m.strip()]


# ───────────────────────── text layer ─────────────────────────
def inspect_pdf(pdf_bytes):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    full_text = []
    for p in doc:
        words = [{"text": w[4], "bbox": [w[0] / p.rect.width, w[1] / p.rect.height, w[2] / p.rect.width, w[3] / p.rect.height]}
                 for w in p.get_text("words")]
        txt = p.get_text("text")
        full_text.append(txt)
        pages.append({"width": p.rect.width, "height": p.rect.height, "words": words, "chars": len(txt.strip()),
                      "images": len(p.get_images())})
    text = "\n".join(full_text)
    has_text = sum(pg["chars"] for pg in pages) > 80
    return {"page_count": len(pages), "has_text_layer": has_text, "text": text, "pages": pages,
            "kind": "digital" if has_text else "scanned"}


def render_page(pdf_bytes, page=0, dpi=100):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return doc[min(page, len(doc) - 1)].get_pixmap(dpi=dpi).tobytes("jpg", jpg_quality=82)


# ───────────────────────── LLM schema ─────────────────────────
def _s(t, nullable=True, desc=None):
    d = {"type": t, "nullable": nullable}
    if desc:
        d["description"] = desc
    return d


EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "vendor_name": _s("STRING"),
        "vendor_gstin": _s("STRING", desc="Supplier GSTIN exactly as printed (15 chars)"),
        "vendor_address": _s("STRING"),
        "buyer_name": _s("STRING"),
        "buyer_gstin": _s("STRING", desc="Recipient/customer GSTIN exactly as printed"),
        "invoice_number": _s("STRING", desc="Exactly as printed. null if the document has no invoice/bill number"),
        "invoice_date": _s("STRING", desc="ISO YYYY-MM-DD"),
        "po_reference": _s("STRING", desc="Purchase order number ONLY if explicitly printed, else null. Never guess."),
        "order_reference_text": _s("STRING", desc="Any other free-text reference to an order/contact, verbatim"),
        "place_of_supply": _s("STRING"),
        "currency": _s("STRING"),
        "rates_include_tax": _s("BOOLEAN", desc="true only if the document states unit rates are inclusive of GST"),
        "line_items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "description": _s("STRING", False),
            "hsn_sac": _s("STRING"),
            "quantity": _s("NUMBER"),
            "unit": _s("STRING"),
            "unit_price": _s("NUMBER", desc="Rate exactly as printed"),
            "amount": _s("NUMBER", desc="Line amount exactly as printed"),
        }, "required": ["description"]}},
        "subtotal": _s("NUMBER", desc="Taxable value / subtotal before GST, as printed"),
        "cgst": _s("NUMBER"), "sgst": _s("NUMBER"), "igst": _s("NUMBER"),
        "gst_rate_percent": _s("NUMBER", desc="Total GST rate e.g. 18"),
        "grand_total": _s("NUMBER", desc="Final payable amount as printed"),
        "amount_in_words": _s("STRING"),
        "bank_account_name": _s("STRING"), "bank_account_number": _s("STRING"),
        "bank_ifsc": _s("STRING"), "bank_name": _s("STRING"),
        "notes": _s("STRING", desc="Any notices/remarks printed on the invoice, verbatim (e.g. change of bank details)"),
        "boxes": {"type": "OBJECT", "nullable": True, "description": "Bounding boxes [ymin,xmin,ymax,xmax] normalised 0-1000",
                  "properties": {k: {"type": "ARRAY", "items": {"type": "INTEGER"}, "nullable": True}
                                 for k in ["invoice_number", "invoice_date", "vendor_gstin", "grand_total", "bank_account_number", "po_reference"]}},
    },
    "required": ["line_items"],
}

EXTRACT_PROMPT = """You are an accounts-payable data-entry specialist reading an Indian GST tax invoice.
Extract the fields defined in the schema EXACTLY as printed on the document.
Rules:
- Never invent or infer values. If a field is not printed, return null. In particular, only fill po_reference if a purchase-order number is literally printed.
- Numbers: plain decimals, no commas or currency symbols (e.g. 314470.00).
- Copy identifiers (invoice number, GSTIN, account number, IFSC) character-for-character, including any mistakes.
- If rates are stated to be inclusive of GST, keep the printed rate and set rates_include_tax=true.
- Copy any notice or remark (e.g. about bank details) verbatim into notes.
- For boxes, give the bounding box of where each value appears on the page."""

VERIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "invoice_number": _s("STRING"), "invoice_date": _s("STRING", desc="ISO YYYY-MM-DD"),
        "vendor_gstin": _s("STRING"), "grand_total": _s("NUMBER"),
        "bank_account_number": _s("STRING"), "bank_ifsc": _s("STRING"),
    },
}

VERIFY_PROMPT = """Second-opinion check. Look at this scanned invoice and read ONLY these six values, character by character:
invoice number, invoice date (ISO), supplier GSTIN, final payable total, beneficiary bank account number, IFSC.
Return null for anything not printed. Do not guess."""


class LLMError(Exception):
    pass


def call_gemini(pdf_bytes, prompt, schema, mime="application/pdf"):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")
    body = {
        "contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(pdf_bytes).decode()}},
                                {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    }
    errors = []
    for model in _models():
        for attempt in range(3):
            b = json.loads(json.dumps(body))
            if "2.5" in model:
                b["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0 if "lite" in model else 512}
            t0 = time.time()
            try:
                r = httpx.post(GEMINI_URL.format(model=model), json=b, headers={"x-goog-api-key": key}, timeout=60)
            except httpx.HTTPError as e:
                errors.append(f"{model}: {type(e).__name__}")
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    usage = data.get("usageMetadata", {})
                    return json.loads(text), {"model": model, "latency_ms": int((time.time() - t0) * 1000),
                                              "tokens_in": usage.get("promptTokenCount"), "tokens_out": usage.get("candidatesTokenCount"),
                                              "attempts": attempt + 1, "fallbacks": errors}
                except (KeyError, IndexError, json.JSONDecodeError) as e:
                    errors.append(f"{model}: unparseable response ({e})")
                    continue
            if r.status_code in (429, 500, 503):
                errors.append(f"{model}: HTTP {r.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            errors.append(f"{model}: HTTP {r.status_code} {r.text[:160]}")
            break  # 400/404 → this model is unusable, try the next one
    raise LLMError("All models failed: " + " | ".join(errors[-4:]))


# ───────────────────────── normalisation ─────────────────────────
def num(x):
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^0-9.\-]", "", str(x))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def norm_id(s):
    return re.sub(r"[^0-9A-Z]", "", str(s).upper()) if s else ""


def normalise(raw):
    out = dict(raw)
    for k in ["subtotal", "cgst", "sgst", "igst", "grand_total", "gst_rate_percent"]:
        out[k] = num(raw.get(k))
    items = []
    for li in raw.get("line_items") or []:
        items.append({"description": (li.get("description") or "").strip(), "hsn_sac": li.get("hsn_sac"),
                      "quantity": num(li.get("quantity")), "unit": li.get("unit"),
                      "unit_price": num(li.get("unit_price")), "amount": num(li.get("amount"))})
    out["line_items"] = items
    for k in ["vendor_gstin", "buyer_gstin", "bank_ifsc"]:
        out[k] = norm_id(raw.get(k)) or None
    out["bank_account_number"] = re.sub(r"\s", "", raw.get("bank_account_number") or "") or None
    inv = (raw.get("invoice_number") or "").strip()
    out["invoice_number"] = inv or None
    return out


# ───────────────────────── verification ─────────────────────────
def _money_forms(v):
    v = round(v, 2)
    forms = {f"{v:.2f}", f"{v:,.2f}".replace(",", "")}
    if v == int(v):
        forms.add(str(int(v)))
    return forms


def ground_in_text(fields, layer):
    """For digital PDFs: does each extracted critical value literally exist on the page? Returns per-field status + bbox."""
    words = layer["pages"][0]["words"] if layer["pages"] else []
    text_compact = re.sub(r"[,\s]", "", layer["text"]).upper()
    res = {}

    def find_box(target_norm):
        for w in words:
            if target_norm and norm_id(w["text"]).replace(".", "") == target_norm.replace(".", ""):
                return w["bbox"]
        return None

    def check(field, value, kind):
        if value in (None, ""):
            res[field] = {"status": "absent"}
            return
        if kind == "money":
            forms = _money_forms(value)
            ok = any(f.replace(",", "") in text_compact for f in forms)
            box = None
            for w in words:
                if re.sub(r"[,]", "", w["text"]) in forms:
                    box = w["bbox"]
            res[field] = {"status": "grounded" if ok else "not_found", "bbox": box}
        else:
            t = norm_id(value)
            ok = t in norm_id(layer["text"])
            res[field] = {"status": "grounded" if ok else "not_found", "bbox": find_box(t)}

    check("invoice_number", fields.get("invoice_number"), "id")
    check("vendor_gstin", fields.get("vendor_gstin"), "id")
    check("buyer_gstin", fields.get("buyer_gstin"), "id")
    check("po_reference", fields.get("po_reference"), "id")
    check("bank_account_number", fields.get("bank_account_number"), "id")
    check("bank_ifsc", fields.get("bank_ifsc"), "id")
    check("grand_total", fields.get("grand_total"), "money")
    check("subtotal", fields.get("subtotal"), "money")
    return res


def compare_reads(primary, second):
    """For scans: compare the main extraction with an independent second read."""
    res = {}
    for f in ["invoice_number", "invoice_date", "vendor_gstin", "grand_total", "bank_account_number", "bank_ifsc"]:
        a, b = primary.get(f), second.get(f)
        if f == "grand_total":
            a, b = num(a), num(b)
            agree = (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 0.5)
        else:
            agree = norm_id(a) == norm_id(b)
        if a in (None, "") and b in (None, ""):
            res[f] = {"status": "absent"}
        else:
            res[f] = {"status": "consensus" if agree else "conflict", "read_1": a, "read_2": b}
    return res


def gemini_box_to_bbox(box):
    if not box or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = box
    return [xmin / 1000, ymin / 1000, xmax / 1000, ymax / 1000]
