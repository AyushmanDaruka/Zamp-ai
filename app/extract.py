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
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Second, independent provider (free tier) used when Gemini is overloaded. Override with GROQ_MODELS.
DEFAULT_GROQ_MODELS = "meta-llama/llama-4-scout-17b-16e-instruct,meta-llama/llama-4-maverick-17b-128e-instruct"


DEFAULT_MODELS = "gemini-3.1-flash-lite,gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.8-flash,gemini-flash-latest"
_last_good = {"model": None}  # warm instances try the last model that answered first


def _models():
    """Gemini models first; Groq models (if a key is set) slot in as soon as the first Gemini wave fails."""
    raw = os.environ.get("GEMINI_MODELS") or DEFAULT_MODELS
    ms = [m.strip() for m in raw.split(",") if m.strip()] if os.environ.get("GEMINI_API_KEY") else []
    if os.environ.get("GROQ_API_KEY"):
        groq = ["groq:" + m.strip() for m in (os.environ.get("GROQ_MODELS") or DEFAULT_GROQ_MODELS).split(",") if m.strip()]
        ms = ms[:3] + groq + ms[3:]
    if _last_good["model"] in ms:
        ms.remove(_last_good["model"])
        ms.insert(0, _last_good["model"])
    return ms


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


def _schema_hint(schema):
    """Gemini schema → a JSON skeleton for providers without the same structured-output format."""
    t = schema.get("type")
    if t == "OBJECT":
        return {k: _schema_hint(v) for k, v in schema.get("properties", {}).items() if k != "boxes"}
    if t == "ARRAY":
        return [_schema_hint(schema["items"])]
    return {"STRING": "string or null", "NUMBER": "number or null", "INTEGER": "integer or null", "BOOLEAN": "true/false or null"}[t] + \
        (f" ({schema['description']})" if schema.get("description") else "")


def _groq_body(model, pdf_bytes, prompt, schema):
    """OpenAI-style vision request: page image + the PDF's own text layer (if any) + the schema as a JSON skeleton."""
    jpg = render_page(pdf_bytes, dpi=150)
    text = inspect_pdf(pdf_bytes)["text"].strip()
    instr = (prompt + "\n\nReturn ONLY a JSON object with exactly these keys (use null when a value is not printed):\n"
             + json.dumps(_schema_hint(schema), indent=1)
             + ("\n\nThe PDF's embedded text layer, for exact characters (the image shows the layout):\n" + text[:6000] if text else ""))
    return {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instr},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}]}]}


def _groq_call(model, body, timeout_s):
    t0 = time.time()
    try:
        r = httpx.post(GROQ_URL, json=body, headers={"Authorization": "Bearer " + os.environ["GROQ_API_KEY"]},
                       timeout=httpx.Timeout(timeout_s, connect=5.0))
    except httpx.HTTPError as e:
        return model, None, f"{model}: {type(e).__name__}", 0
    if r.status_code == 200:
        try:
            return model, json.loads(r.json()["choices"][0]["message"]["content"]), None, int((time.time() - t0) * 1000)
        except (KeyError, IndexError, ValueError) as e:
            return model, None, f"{model}: unparseable ({type(e).__name__})", 0
    return model, None, f"{model}: HTTP {r.status_code}", r.status_code


def _one_call(model, body, key, timeout_s):
    if model.startswith("groq:"):
        return _groq_call(model, body["groq"][model], timeout_s)
    body = body["gemini"]
    t0 = time.time()
    try:
        r = httpx.post(GEMINI_URL.format(model=model), json=body, headers={"x-goog-api-key": key},
                       timeout=httpx.Timeout(timeout_s, connect=5.0))
    except httpx.HTTPError as e:
        return model, None, f"{model}: {type(e).__name__}", 0
    if r.status_code == 200:
        try:
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = [p["text"] for p in parts if "text" in p and not p.get("thought")][-1]
            return model, json.loads(text), None, int((time.time() - t0) * 1000)
        except (KeyError, IndexError, ValueError) as e:
            return model, None, f"{model}: unparseable ({type(e).__name__})", 0
    status = ""
    try:
        status = r.json().get("error", {}).get("status", "")
    except ValueError:
        pass
    return model, None, f"{model}: HTTP {r.status_code} {status}".strip(), r.status_code


def call_gemini(pdf_bytes, prompt, schema, mime="application/pdf", budget_s=32.0, hedge=3):
    """Structured-output call, hedged across models.

    The free tier is shared and frequently overloaded (503) or slow, and which model is healthy changes minute
    to minute. So the same request goes to `hedge` models in parallel and the first valid answer wins
    ("hedged requests", The Tail at Scale). Remaining models are tried in further waves until the time budget
    runs out. Output is identical in shape whichever model answers, and everything is verified downstream anyway."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    key = os.environ.get("GEMINI_API_KEY")
    if not key and not os.environ.get("GROQ_API_KEY"):
        raise LLMError("No AI key set (GEMINI_API_KEY / GROQ_API_KEY)")
    body = {"gemini": {
        "contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(pdf_bytes).decode()}},
                                {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
    }, "groq": {}}
    for m in _models():
        if m.startswith("groq:"):
            body["groq"][m] = _groq_body(m[5:], pdf_bytes, prompt, schema)
    t_start = time.time()
    deadline = t_start + budget_s
    errors, dead = [], set()
    pool = ThreadPoolExecutor(max_workers=len(_models()) + 2)
    try:
        while time.time() < deadline - 3:
            queue = [m for m in _models() if m not in dead]
            if not queue:
                break
            pending = {}
            for m in queue[:hedge]:
                pending[pool.submit(_one_call, m, body, key, min(25.0, deadline - time.time()))] = m
            queue = queue[hedge:]
            while pending:
                done, _ = wait(list(pending), timeout=max(0.1, deadline - time.time()), return_when=FIRST_COMPLETED)
                if not done:
                    break
                for fut in done:
                    pending.pop(fut)
                    model, result, err, code = fut.result()
                    if result is not None:
                        _last_good["model"] = model
                        return result, {"model": model, "latency_ms": int((time.time() - t_start) * 1000),
                                        "raced": hedge, "fallbacks": errors}
                    errors.append(err)
                    if code in (400, 403, 404):
                        dead.add(model)
                    if queue and time.time() < deadline - 3:  # keep `hedge` requests in flight
                        m = queue.pop(0)
                        pending[pool.submit(_one_call, m, body, key, min(25.0, deadline - time.time()))] = m
                if time.time() > deadline:
                    break
            time.sleep(0.8)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise LLMError("No model available within the time budget (" + "; ".join(errors[-5:]) + ")")


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


_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
_TO_ALPHA = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}
_CONFUSABLE = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5", "8": "B", "B": "8", "2": "Z", "Z": "2"}


def repair_ifsc(v):
    """IFSC = 4 letters (bank) + '0' + 6 alphanumerics (branch). The 5th char is ALWAYS zero (RBI format)."""
    if not v or len(v) != 11:
        return v, None
    fixed = "".join(_TO_ALPHA.get(c, c) for c in v[:4]) + ("0" if v[4] in "OQD" else v[4]) + v[5:]
    return fixed, (f"IFSC '{v}' → '{fixed}' (5th character of an IFSC is always 0)" if fixed != v else None)


def repair_gstin(v):
    """GSTIN positions have fixed types and the last char is a mod-36 checksum. If the checksum fails, try only
    OCR-confusable swaps (0/O, 1/I, 5/S, 8/B, 2/Z) and accept a repair ONLY if the checksum then validates."""
    from app.gst import validate_gstin
    if not v or len(v) != 15 or validate_gstin(v)[0]:
        return v, None
    digit_pos, alpha_pos = {0, 1, 7, 8, 9, 10}, {2, 3, 4, 5, 6, 11}
    cand = "".join(_TO_DIGIT.get(c, c) if i in digit_pos else _TO_ALPHA.get(c, c) if i in alpha_pos else ("Z" if i == 13 and c == "2" else c)
                   for i, c in enumerate(v))
    if validate_gstin(cand)[0]:
        return cand, f"GSTIN '{v}' → '{cand}' (position types fixed; checksum now validates)"
    for i in (12, 14):
        if cand[i] in _CONFUSABLE:
            alt = cand[:i] + _CONFUSABLE[cand[i]] + cand[i + 1:]
            if validate_gstin(alt)[0]:
                return alt, f"GSTIN '{v}' → '{alt}' (look-alike character at position {i + 1}; checksum now validates)"
    return v, None


def repair_account(v):
    if not v:
        return v, None
    if re.fullmatch(r"[0-9OQDILSB]+", v) and re.search(r"[OQDILSB]", v) and sum(c.isdigit() for c in v) >= len(v) // 2:
        fixed = "".join(_TO_DIGIT.get(c, c) for c in v)
        return fixed, f"Account no. '{v}' → '{fixed}' (bank account numbers are numeric)"
    return v, None


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
    repairs = []
    for k, fn in (("vendor_gstin", repair_gstin), ("buyer_gstin", repair_gstin), ("bank_ifsc", repair_ifsc), ("bank_account_number", repair_account)):
        fixed, note = fn(out.get(k))
        if note:
            out[k] = fixed
            repairs.append(note)
    out["_repairs"] = repairs
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
    second = dict(second)
    for k, fn in (("vendor_gstin", repair_gstin), ("bank_ifsc", repair_ifsc), ("bank_account_number", repair_account)):
        if second.get(k):
            second[k] = fn(norm_id(second[k]) if k != "bank_account_number" else re.sub(r"\s", "", second[k]))[0]
    for f in ["invoice_number", "invoice_date", "vendor_gstin", "grand_total", "bank_account_number", "bank_ifsc"]:
        a, b = primary.get(f), second.get(f)
        if f == "grand_total":
            a, b = num(a), num(b)
            agree = (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 0.5)
        else:
            agree = norm_id(a) == norm_id(b)
        if a in (None, "") and b in (None, ""):
            res[f] = {"status": "absent"}
        elif a in (None, "") or b in (None, ""):
            res[f] = {"status": "single", "read_1": a, "read_2": b}  # only one read saw it — unconfirmed, not contradicted
        else:
            res[f] = {"status": "consensus" if agree else "conflict", "read_1": a, "read_2": b}
    return res


def gemini_box_to_bbox(box):
    if not box or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = box
    return [xmin / 1000, ymin / 1000, xmax / 1000, ymax / 1000]
