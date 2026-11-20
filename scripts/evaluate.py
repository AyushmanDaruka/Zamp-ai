"""Accuracy harness.

  python -m scripts.evaluate --oracle     # perfect extraction → tests the rules engine alone
  python -m scripts.evaluate              # real Gemini extraction → end-to-end accuracy
  python -m scripts.evaluate --seed       # end-to-end, and save eval runs as dashboard seed history

Each case runs in isolation (no persistence) so cases don't affect each other.
Writes app/data/eval_report.json, which the UI shows on the Evaluation tab.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app import master as M  # noqa: E402
from app.pipeline import run_invoice  # noqa: E402
from scripts.cases import CASES  # noqa: E402
from scripts.gen_invoices import compute, vendor_profile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def oracle_extraction(case):
    v = vendor_profile(case)
    if case.get("bank_override"):
        v["bank"] = case["bank_override"]
    if case.get("gstin_typo"):
        v["gstin"] = v["gstin"][:14] + ("X" if v["gstin"][14] != "X" else "Y")
    t = compute(case, v["gstin"])
    incl = case.get("tax_inclusive")
    return {
        "vendor_name": v["name"], "vendor_gstin": v["gstin"], "buyer_name": M.BUYER["name"], "buyer_gstin": M.BUYER["gstin"],
        "invoice_number": case["invoice_number"], "invoice_date": case["date"], "po_reference": case.get("po"),
        "rates_include_tax": bool(incl), "gst_rate_percent": 18,
        "line_items": [{"description": l["desc"], "hsn_sac": l["hsn"], "quantity": l["qty"], "unit": l["unit"],
                        "unit_price": round(l["rate"] * 1.18, 2) if incl else l["rate"],
                        "amount": round(round(l["rate"] * 1.18, 2) * l["qty"], 2) if incl else l["amount"]} for l in t["lines"]],
        "subtotal": t["taxable"], "cgst": t["cgst"], "sgst": t["sgst"], "igst": t["igst"], "grand_total": t["total"],
        "bank_account_name": v["bank"]["account_name"], "bank_account_number": v["bank"]["account_number"],
        "bank_ifsc": v["bank"]["ifsc"], "bank_name": v["bank"]["bank_name"], "notes": case.get("note"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between cases (free-tier rate limits)")
    a = ap.parse_args()

    truth = {c["id"]: c for c in json.loads((ROOT / "samples" / "cases.json").read_text())}
    rows, seed = [], []
    cases = [c for c in CASES if not a.only or a.only in c["id"]]
    for n, case in enumerate(cases):
        pdf = (ROOT / "samples" / f"{case['id']}.pdf").read_bytes()
        override = oracle_extraction(case) if a.oracle else None
        result = None
        for ev in run_invoice(pdf, f"{case['id']}.pdf", source="seed", persist=False, extraction_override=override):
            if ev["type"] == "result":
                result = ev["run"]
        exp = case["expected"]
        got = result["decision"]
        f = (result.get("result") or {}).get("fields") or {}
        po_got = (result.get("summary") or {}).get("po_number")
        tr = truth[case["id"]]
        field_hits = {
            "invoice_number": (f.get("invoice_number") or None) == tr["invoice_number"],
            "vendor_gstin": f.get("vendor_gstin") == tr["vendor_gstin"],
            "invoice_date": f.get("invoice_date") == tr["date"],
            "grand_total": f.get("grand_total") is not None and abs(f["grand_total"] - tr["total"]) < 0.5,
            "po": po_got == exp["po"],
        }
        ok = got == exp["decision"]
        rows.append({"id": case["id"], "title": case["title"], "kind": case["kind"], "scanned": case["scanned"],
                     "expected": exp["decision"], "got": got, "correct": ok, "fields": field_hits,
                     "reason": (result.get("result") or {}).get("decision", {}).get("primary_reason"),
                     "ms": result["duration_ms"], "model": ((result.get("result") or {}).get("meta") or {}).get("model")})
        print(f"{'✓' if ok else '✗'} {case['id']:<30} expected {exp['decision']:<19} got {got:<19} "
              f"fields {sum(field_hits.values())}/{len(field_hits)}  {result['duration_ms']} ms")
        if not ok:
            print("    ↳", rows[-1]["reason"])
        if a.seed and case["kind"] == "eval" and result["status"] == "done":
            r = dict(result)
            r["id"] = f"seed-{case['id']}"
            r["created_at"] = (datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc) - timedelta(hours=7 * (len(cases) - n))).isoformat()
            r["filename"] = f"{case['id']}.pdf"
            r["events"] = []
            seed.append(r)
        if not a.oracle and n < len(cases) - 1:
            time.sleep(a.pause)

    n = len(rows)
    fld = {k: sum(r["fields"][k] for r in rows) / n for k in rows[0]["fields"]}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "oracle" if a.oracle else "end-to-end",
              "n": n, "decision_accuracy": sum(r["correct"] for r in rows) / n, "field_accuracy": fld,
              "avg_ms": int(sum(r["ms"] for r in rows) / n), "rows": rows}
    answered = [r for r in rows if r["got"] != "ERROR"]
    report["availability"] = len(answered) / n
    report["accuracy_when_answered"] = (sum(r["correct"] for r in answered) / len(answered)) if answered else None
    report["avg_ms"] = int(sum(r["ms"] for r in answered) / len(answered)) if answered else report["avg_ms"]
    print(f"\nDecision accuracy: {report['decision_accuracy'] * 100:.0f}% ({sum(r['correct'] for r in rows)}/{n})")
    print("Field accuracy:", {k: f"{v * 100:.0f}%" for k, v in fld.items()})
    if not a.only:
        out = ROOT / "app" / "data" / ("eval_report_oracle.json" if a.oracle else "eval_report.json")
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
    if a.seed:
        (ROOT / "app" / "data" / "seed_runs.json").write_text(json.dumps(seed, indent=1, default=str))
        print(f"Saved {len(seed)} seed runs")
    sys.exit(0 if report["decision_accuracy"] == 1 else 1)


if __name__ == "__main__":
    main()
