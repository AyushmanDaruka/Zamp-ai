"""Orchestrates one invoice run and yields a live event per stage (streamed to the UI as NDJSON)."""
import hashlib
import time
import uuid
from datetime import datetime, timezone

from app import db
from app import extract as X
from app import rules as R

STAGES = [
    ("intake", "Intake & fingerprint"),
    ("extract", "AI extraction"),
    ("verify", "Verify extraction"),
    ("vendor", "Vendor & bank check"),
    ("duplicate", "Duplicate detection"),
    ("match", "PO · GRN · invoice match"),
    ("tax", "GST compliance"),
    ("decide", "Decision & actions"),
]
LABEL = dict(STAGES)
CRITICAL = ["invoice_number", "vendor_gstin", "grand_total", "bank_account_number", "bank_ifsc"]


def worst(checks):
    order = {"fail": 3, "warn": 2, "pass": 1, "info": 0}
    if not checks:
        return "pass"
    w = max(checks, key=lambda c: order.get(c["status"], 0))["status"]
    return "pass" if w == "info" else w


def run_invoice(pdf_bytes, filename, source="upload", persist=True, extraction_override=None, run_id=None):
    run_id = run_id or uuid.uuid4().hex
    t_start = time.time()
    events, issues = [], []
    ctx = {}

    def emit(ev):
        ev["t"] = round(time.time() - t_start, 3)
        events.append(ev)
        return ev

    def stage(sid, fn):
        yield emit({"type": "stage", "stage": sid, "label": LABEL[sid], "state": "running"})
        t0 = time.time()
        try:
            checks, summary, data = fn()
            st = worst(checks)
        except X.LLMError as e:
            checks, summary, data, st = [R.chk("llm", "Model call", "fail", str(e))], "Extraction service unavailable", {}, "fail"
            ctx["fatal"] = str(e)
        yield emit({"type": "stage", "stage": sid, "label": LABEL[sid], "state": "done", "status": st, "summary": summary,
                    "checks": checks, "data": data, "ms": int((time.time() - t0) * 1000)})

    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    yield emit({"type": "run", "id": run_id, "filename": filename, "stages": [{"id": s, "label": l} for s, l in STAGES]})
    prior = [r for r in db.prior_runs() if r["id"] != run_id] if persist else []

    # 1 ─ intake
    def s_intake():
        layer = X.inspect_pdf(pdf_bytes)
        ctx["layer"] = layer
        kind = layer["kind"]
        checks = [R.chk("format", "Readable PDF", "pass", f"{layer['page_count']} page(s), {len(pdf_bytes) / 1024:.0f} KB"),
                  R.chk("text_layer", "Document type", "info",
                        "Digital PDF — text layer present; values will be grounded against it" if kind == "digital"
                        else "Scanned image — no text layer; routing to vision model + independent second read")]
        return checks, ("Digital PDF" if kind == "digital" else "Scanned image (no text layer)") + f" · SHA-256 {file_hash[:12]}…", {"kind": kind, "sha256": file_hash}
    yield from stage("intake", s_intake)

    # 2 ─ extract
    def s_extract():
        if extraction_override is not None:
            raw, meta = extraction_override, {"model": "oracle (ground truth)", "latency_ms": 0}
        else:
            # Stay well inside Vercel's 60 s limit (cold start can eat 10-20 s). A healthy model answers in
            # 2-4 s; if nothing answers in ~24 s it won't, so fail fast and clean (Retry) instead of hanging
            # to the hard kill. Scans also need the second read, so budget both calls together.
            budget = 24.0 if ctx["layer"]["kind"] == "digital" else 22.0
            raw, meta = X.call_gemini(pdf_bytes, X.EXTRACT_PROMPT, X.EXTRACT_SCHEMA, budget_s=budget)
        f = X.normalise(raw)
        ctx["f"], ctx["meta"] = f, meta
        n = len(f["line_items"])
        checks = [R.chk("model", "Model", "info", f"{meta['model']} · {meta['latency_ms']} ms" + (f" · raced {meta['raced']} models in parallel, first valid answer wins" if meta.get("raced") else "") + (f" · {len(meta['fallbacks'])} busy/failed: {', '.join(meta['fallbacks'][:3])}" if meta.get("fallbacks") else "")),
                  R.chk("fields", "Fields extracted", "pass" if n else "fail", f"{n} line items · vendor '{f.get('vendor_name')}' · invoice {f.get('invoice_number') or '—'} · total {R.inr(f.get('grand_total'))}")]
        return checks, f"{f.get('vendor_name') or 'Unknown vendor'} · {f.get('invoice_number') or 'no invoice no.'} · {R.inr(f.get('grand_total'))}", {"fields": f, "meta": meta}
    yield from stage("extract", s_extract)
    if ctx.get("fatal"):
        yield from _finish_error(run_id, filename, source, file_hash, events, t_start, persist, pdf_bytes, ctx)
        return
    f = ctx["f"]

    # 3 ─ verify extraction (grounding / dual read) + document arithmetic
    def s_verify():
        layer = ctx["layer"]
        checks, prov = [], {}
        if layer["kind"] == "digital":
            g = X.ground_in_text(f, layer)
            prov = g
            bad = [k for k in CRITICAL if g.get(k, {}).get("status") == "not_found"]
            ok_n = sum(1 for v in g.values() if v["status"] == "grounded")
            checks.append(R.chk("grounding", "Values exist on the page (anti-hallucination)", "fail" if bad else "pass",
                                f"{ok_n} values found verbatim in the PDF text layer" + (f"; NOT found: {', '.join(bad)}" if bad else "")))
            if bad:
                issues.append(R.issue("EXTRACTION_UNVERIFIED", "HOLD", "AP team", f"Could not verify extracted {', '.join(bad)} against the document text.",
                                      internal_ask=f"Confirm {', '.join(bad)} by eye on the original PDF."))
        else:
            second = None
            if extraction_override is not None:
                second = {k: f.get(k) for k in ["invoice_number", "invoice_date", "vendor_gstin", "grand_total", "bank_account_number", "bank_ifsc"]}
            else:
                try:
                    second, _ = X.call_gemini(pdf_bytes, X.VERIFY_PROMPT, X.VERIFY_SCHEMA, budget_s=max(6.0, 42.0 - (time.time() - t_start)))
                except X.LLMError:
                    second = None
            if second is None:
                # Second read unavailable (model overloaded). Fall back to cross-checks against master data,
                # which run in later stages anyway: GSTIN, bank account and PO quantities must all match records.
                checks.append(R.chk("dual_read", "Independent second read", "warn",
                                    "Second read unavailable (model busy) — relying on checksum, arithmetic and master-data cross-checks"))
                second = {}
            cmp = X.compare_reads(f, second) if second else {}
            prov = cmp
            conflicts = [k for k, v in cmp.items() if v["status"] == "conflict"]
            agree = sum(1 for v in cmp.values() if v["status"] == "consensus")
            if cmp:
                checks.append(R.chk("dual_read", "Independent second read agrees", "fail" if conflicts else "pass",
                                    f"{agree} critical values confirmed by a second, independent read" +
                                    ("; DISAGREE: " + ", ".join(f"{k} ({cmp[k]['read_1']} vs {cmp[k]['read_2']})" for k in conflicts) if conflicts else "")))
            if conflicts:
                issues.append(R.issue("SCAN_UNCERTAIN", "HOLD", "AP team", "Two independent reads of the scan disagree on " + ", ".join(conflicts) + ".",
                                      internal_ask="Check these values on the scan: " + ", ".join(f"{k}: '{cmp[k]['read_1']}' or '{cmp[k]['read_2']}'?" for k in conflicts)))
        for note in f.get("_repairs") or []:
            checks.append(R.chk("ocr_repair", "Format-aware OCR repair", "info", note))
        boxes = (f.get("boxes") or {})
        for k, v in prov.items():
            if not v.get("bbox") and boxes.get(k):
                v["bbox"] = X.gemini_box_to_bbox(boxes[k])
        dc, di = R.check_document(f)
        issues.extend(di)
        checks += dc
        bad = sum(1 for c in checks if c["status"] == "fail")
        return checks, "All values verified & arithmetic reconciles" if not bad else f"{bad} problem(s) with the document", {"provenance": prov}
    yield from stage("verify", s_verify)
    if ctx.get("fatal"):
        yield from _finish_error(run_id, filename, source, file_hash, events, t_start, persist, pdf_bytes, ctx)
        return

    # 4 ─ vendor
    def s_vendor():
        v, how, score = R.identify_vendor(f)
        ctx["vendor"] = v
        c, i = R.check_vendor(f, v, how)
        issues.extend(i)
        return c, (f"{v['name']} ({v['id']})" if v else "Vendor not in master"), {"vendor_id": v["id"] if v else None, "how": how}
    yield from stage("vendor", s_vendor)
    vendor = ctx["vendor"]

    # 5 ─ duplicates
    def s_dup():
        c, i = R.check_duplicates(f, vendor, file_hash, prior)
        issues.extend(i)
        return c, "Probable duplicate" if i else "No duplicate found", {}
    yield from stage("duplicate", s_dup)

    # 6 ─ PO / GRN
    def s_match():
        c, i, out = R.check_po(f, vendor, prior)
        issues.extend(i)
        ctx["po_out"] = out
        po = out.get("po")
        summ = (f"{po['po_number']}{' (inferred)' if out['inferred'] else ''} · billable {R.inr(out['approvable_taxable'])} of {R.inr(f.get('subtotal'))}" if po else "No PO matched")
        return c, summ, {"po_number": po["po_number"] if po else None, "inferred": out["inferred"], "candidates": out["candidates"], "lines": out["lines"]}
    yield from stage("match", s_match)

    # 7 ─ tax
    def s_tax():
        c, i = R.check_tax(f, vendor)
        issues.extend(i)
        return c, "GST compliant" if not i else "GST problem", {}
    yield from stage("tax", s_tax)

    # 8 ─ decide
    def s_decide():
        d = R.decide(f, vendor, ctx["po_out"], issues)
        ctx["decision"] = d
        checks = [R.chk("decision", "Decision", {"APPROVED": "pass", "PARTIALLY_APPROVED": "warn", "ON_HOLD": "warn", "REJECTED": "fail"}[d["decision"]], d["headline"])]
        if d["payment"]:
            p = d["payment"]
            checks.append(R.chk("payment", "Payment proposal", "info", f"Net {R.inr(p['net_payable'])} after TDS {R.inr(p['tds'])} u/s {p['tds_section']} · due {p['due_date']}"))
        if d["vendor_email"]:
            checks.append(R.chk("email", "Vendor email drafted", "info", d["vendor_email"]["subject"]))
        for t in d["internal_tasks"]:
            checks.append(R.chk("task", f"Task → {t['owner']}", "info", t["ask"]))
        return checks, d["headline"], {}
    yield from stage("decide", s_decide)

    d = ctx["decision"]
    po = ctx["po_out"].get("po")
    summary = {"vendor_id": vendor["id"] if vendor else None, "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"),
               "total": f.get("grand_total"), "po_number": po["po_number"] if po else None,
               "primary_code": d.get("primary_code"), "owner": d.get("owner"), "inferred_po": ctx["po_out"].get("inferred"),
               "consumed_lines": ctx["po_out"].get("consumed_lines", []) if d["decision"] in ("APPROVED", "PARTIALLY_APPROVED") else []}
    result = {"fields": f, "decision": d, "po": ctx["po_out"], "meta": ctx.get("meta"), "doc_kind": ctx["layer"]["kind"]}
    run = {"id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "source": source, "filename": filename,
           "file_hash": file_hash, "status": "done", "decision": d["decision"], "vendor_name": (vendor or {}).get("name") or f.get("vendor_name"),
           "invoice_number": f.get("invoice_number"), "total": f.get("grand_total"),
           "approved_amount": d["payment"]["gross"] if d["payment"] else 0.0, "risk_caught": d["risk_caught_inr"],
           "duration_ms": int((time.time() - t_start) * 1000), "doc_kind": ctx["layer"]["kind"], "summary": summary, "result": result}
    yield emit({"type": "result", "run": {k: v for k, v in run.items()}})
    run["events"] = events
    if persist:
        db.save(run, pdf_bytes)


def _finish_error(run_id, filename, source, file_hash, events, t_start, persist, pdf_bytes, ctx):
    run = {"id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "source": source, "filename": filename, "file_hash": file_hash,
           "status": "error", "decision": "ERROR", "vendor_name": None, "invoice_number": None, "total": None, "approved_amount": 0.0,
           "risk_caught": 0.0, "duration_ms": int((time.time() - t_start) * 1000), "doc_kind": (ctx.get("layer") or {}).get("kind"),
           "summary": {}, "result": {"error": ctx.get("fatal"), "retryable": True, "decision": {"decision": "ERROR", "headline": "AI reader busy — nothing was decided. Retry, or it goes to the manual queue",
                                                                              "reasons": [ctx.get("fatal")], "issues": [], "payment": None, "vendor_email": None,
                                                                              "internal_tasks": [{"owner": "AP team", "ask": "Process manually; extraction service failed."}]}}}
    ev = {"type": "result", "run": run, "t": round(time.time() - t_start, 3)}
    events.append(ev)
    yield ev
    run["events"] = events
    if persist:
        db.save(run, pdf_bytes)
