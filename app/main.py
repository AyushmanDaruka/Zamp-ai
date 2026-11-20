import json
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

load_dotenv()

from app import db  # noqa: E402
from app import master as M  # noqa: E402
from app.extract import render_page  # noqa: E402
from app.pipeline import STAGES, run_invoice  # noqa: E402
from app.rules import consumption, received  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
DATA = Path(__file__).resolve().parent / "data"
MAX_BYTES = 4 * 1024 * 1024

app = FastAPI(title="Invoice Desk")


def _stream(gen):
    def body():
        for ev in gen:
            yield json.dumps(ev, default=str) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"ok": True, "llm": bool(os.environ.get("GEMINI_API_KEY")), "db": "postgres" if db.IS_PG else "sqlite"}


@app.get("/api/samples")
def samples():
    cases = json.loads((SAMPLES / "cases.json").read_text())
    return [{"id": c["id"], "kind": c["kind"], "title": c["title"], "scanned": c["scanned"], "expected": c["expected"]["decision"]} for c in cases]


@app.get("/api/samples/{sid}.pdf")
def sample_pdf(sid: str):
    p = SAMPLES / f"{sid}.pdf"
    if not p.resolve().parent == SAMPLES.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/samples/{sid}.jpg")
def sample_png(sid: str):
    p = SAMPLES / f"{sid}.pdf"
    if not p.resolve().parent == SAMPLES.resolve() or not p.exists():
        raise HTTPException(404)
    return Response(render_page(p.read_bytes()), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/runs/sample/{sid}")
def run_sample(sid: str):
    p = SAMPLES / f"{sid}.pdf"
    if not p.resolve().parent == SAMPLES.resolve() or not p.exists():
        raise HTTPException(404)
    return _stream(run_invoice(p.read_bytes(), p.name, source="sample"))


@app.post("/api/runs")
async def run_upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 4 MB)")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "Please upload a PDF")
    return _stream(run_invoice(data, file.filename or "upload.pdf", source="upload"))


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{rid}")
def run_detail(rid: str):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404)
    return r


def _run_pdf(rid):
    pdf = db.get_pdf(rid)
    if pdf is None:
        r = db.get_run(rid)
        if r and r.get("filename") and (SAMPLES / r["filename"]).exists():
            pdf = (SAMPLES / r["filename"]).read_bytes()
    if pdf is None:
        raise HTTPException(404)
    return pdf


@app.get("/api/runs/{rid}/page.jpg")
def run_page(rid: str):
    return Response(render_page(_run_pdf(rid)), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/runs/{rid}/file.pdf")
def run_file(rid: str):
    return Response(_run_pdf(rid), media_type="application/pdf")


@app.get("/api/stats")
def stats():
    rs = [r for r in db.list_runs() if r["status"] == "done"]
    n = len(rs)
    dec = Counter(r["decision"] for r in rs)
    reasons = Counter((r.get("summary") or {}).get("primary_code") for r in rs if r["decision"] != "APPROVED")
    reasons.pop(None, None)
    return {
        "runs": n, "by_decision": dec,
        "touchless_rate": dec.get("APPROVED", 0) / n if n else 0,
        "processed_value": sum(r["total"] or 0 for r in rs),
        "approved_value": sum(r["approved_amount"] or 0 for r in rs),
        "risk_caught": sum(r["risk_caught"] or 0 for r in rs),
        "avg_ms": int(sum(r["duration_ms"] or 0 for r in rs) / n) if n else 0,
        "exceptions": reasons.most_common(),
        "owners": Counter((r.get("summary") or {}).get("owner") for r in rs if r["decision"] != "APPROVED" and (r.get("summary") or {}).get("owner")).most_common(),
        "scanned_share": sum(1 for r in rs if r.get("doc_kind") == "scanned") / n if n else 0,
    }


@app.get("/api/master")
def master():
    prior = db.prior_runs()
    pos = []
    for po in M.PURCHASE_ORDERS:
        used, rec = consumption(po["po_number"], prior), received(po["po_number"])
        lines = [{**l, "billed": used.get(l["line"], 0), "received": rec.get(l["line"], 0)} for l in po["lines"]]
        billed_val = sum(l["billed"] * l["unit_price"] for l in lines)
        pos.append({**po, "lines": lines, "billed_value": billed_val, "vendor_name": M.vendor_by_id(po["vendor_id"])["name"]})
    snap = M.snapshot()
    snap["purchase_orders"] = pos
    return snap


@app.get("/api/eval")
def evaluation():
    out = {}
    for k, fn in (("end_to_end", "eval_report.json"), ("oracle", "eval_report_oracle.json")):
        p = DATA / fn
        out[k] = json.loads(p.read_text()) if p.exists() else None
    return out


@app.get("/api/stages")
def stages():
    return [{"id": s, "label": l} for s, l in STAGES]


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True, "remaining": db.count()}


# Local dev: serve the static frontend. On Vercel, /public is served by the CDN.
PUBLIC = ROOT / "public"
if PUBLIC.exists() and not os.environ.get("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="public")


@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=500)
