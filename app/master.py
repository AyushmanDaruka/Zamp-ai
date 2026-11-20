"""Procurement system of record for the fictional buyer, Kaveri Precision Components Pvt Ltd.

This is what the AP clerk has open in another window: vendor master, purchase orders,
goods receipts (GRNs), the invoice ledger, and the company's AP policy.
In production these would be ERP reads (Tally / SAP / Zoho); here they're static fixtures.
"""
from app.gst import make_gstin

BUYER = {
    "name": "Kaveri Precision Components Pvt Ltd",
    "gstin": make_gstin("29", "AAKCK4821L"),
    "address": "Plot 42, KIADB Industrial Area, Peenya, Bengaluru 560058, Karnataka",
    "state": "Karnataka",
}

POLICY = {
    "price_tolerance_pct": 2.0,        # unit price may exceed PO by ≤2% → approve with variance note
    "po_overrun_tolerance_pct": 1.0,   # cumulative billing may exceed PO value by ≤1%
    "rounding_tolerance_inr": 2.0,     # arithmetic slack for paisa rounding
    "po_inference_min_score": 0.75,    # inferred PO auto-accepted only above this score…
    "po_inference_min_margin": 0.20,   # …and if clearly ahead of the runner-up
    "duplicate_amount_pct": 0.5,       # fuzzy duplicate: same vendor, total within 0.5%…
    "duplicate_window_days": 90,       # …within 90 days
    "cfo_approval_above_inr": 1000000, # anything above ₹10L needs CFO sign-off even if clean
    "msme_max_payment_days": 45,       # MSMED Act s.15 / Income-tax s.43B(h)
}

VENDORS = [
    {
        "id": "V001", "name": "Shree Ganesh Steel Traders", "aliases": ["Shree Ganesh Steel", "SGS Traders"],
        "gstin": make_gstin("29", "ABCFS7712K"), "status": "active", "category": "Raw material - steel",
        "msme": "Micro", "payment_terms_days": 30, "tds_section": "194Q", "tds_rate_pct": 0.1,
        "bank": {"account_name": "Shree Ganesh Steel Traders", "account_number": "50200012345678", "ifsc": "HDFC0001234", "bank_name": "HDFC Bank, Peenya"},
        "contact": {"name": "Ramesh Agarwal", "email": "accounts@shreeganeshsteel.in", "phone": "+91 98450 11223"},
    },
    {
        "id": "V002", "name": "Vertex Packaging Solutions Pvt Ltd", "aliases": ["Vertex Packaging"],
        "gstin": make_gstin("33", "AAECV3391P"), "status": "active", "category": "Packaging",
        "msme": "Small", "payment_terms_days": 45, "tds_section": "194Q", "tds_rate_pct": 0.1,
        "bank": {"account_name": "Vertex Packaging Solutions Pvt Ltd", "account_number": "1234567890123", "ifsc": "IOBA0000321", "bank_name": "Indian Overseas Bank, Hosur"},
        "contact": {"name": "Priya Raman", "email": "billing@vertexpack.co.in", "phone": "+91 94430 55678"},
    },
    {
        "id": "V003", "name": "Nimbus IT Services LLP", "aliases": ["Nimbus IT", "Nimbus IT Services"],
        "gstin": make_gstin("27", "AAPFN6620D"), "status": "active", "category": "IT services",
        "msme": None, "payment_terms_days": 30, "tds_section": "194J(a)", "tds_rate_pct": 2.0,
        "bank": {"account_name": "Nimbus IT Services LLP", "account_number": "045205001234", "ifsc": "ICIC0000452", "bank_name": "ICICI Bank, Andheri East"},
        "contact": {"name": "Karan Mehta", "email": "finance@nimbusit.in", "phone": "+91 98200 44321"},
    },
    {
        "id": "V004", "name": "Deccan Electricals", "aliases": ["Deccan Electrical", "Deccan Elec"],
        "gstin": make_gstin("29", "AFZPD1185M"), "status": "active", "category": "Electrical & MRO",
        "msme": "Micro", "payment_terms_days": 30, "tds_section": "194Q", "tds_rate_pct": 0.1,
        "bank": {"account_name": "Deccan Electricals", "account_number": "33410100009876", "ifsc": "BARB0PEENYA", "bank_name": "Bank of Baroda, Peenya"},
        "contact": {"name": "Manjunath Gowda", "email": "deccanelectricals@gmail.com", "phone": "+91 99001 23456"},
    },
    {
        "id": "V005", "name": "Precision Tooling Works", "aliases": ["Precision Tooling", "PTW"],
        "gstin": make_gstin("24", "AAHFP2207C"), "status": "active", "category": "Cutting tools",
        "msme": None, "payment_terms_days": 60, "tds_section": "194Q", "tds_rate_pct": 0.1,
        "bank": {"account_name": "Precision Tooling Works", "account_number": "917020045678123", "ifsc": "UTIB0000123", "bank_name": "Axis Bank, Rajkot"},
        "contact": {"name": "Hitesh Patel", "email": "accounts@precisiontooling.in", "phone": "+91 98250 77889"},
    },
    {
        "id": "V006", "name": "Coastal Logistics Pvt Ltd", "aliases": ["Coastal Logistics"],
        "gstin": make_gstin("32", "AAFCC5540H"), "status": "blocked", "block_reason": "KYC lapsed — PAN-Aadhaar link and bank re-verification pending since Aug 2026",
        "category": "Logistics", "msme": None, "payment_terms_days": 30, "tds_section": "194C", "tds_rate_pct": 2.0,
        "bank": {"account_name": "Coastal Logistics Pvt Ltd", "account_number": "67543210987", "ifsc": "SBIN0070123", "bank_name": "SBI, Kochi"},
        "contact": {"name": "Anil Thomas", "email": "ar@coastallogistics.in", "phone": "+91 94470 33221"},
    },
]


def _po(po_number, vendor_id, date, gl, cost_center, lines, status="open"):
    for ln in lines:
        ln["amount"] = round(ln["qty"] * ln["unit_price"], 2)
    return {"po_number": po_number, "vendor_id": vendor_id, "date": date, "status": status,
            "gl_code": gl, "cost_center": cost_center, "lines": lines,
            "total": round(sum(l["amount"] for l in lines), 2)}


PURCHASE_ORDERS = [
    _po("PO-2026-0101", "V001", "2026-10-20", "5010 · Raw Material - Steel", "CC-PROD-01", [
        {"line": 1, "description": "MS Round Bar 12mm (IS 2062)", "hsn": "7214", "qty": 2000, "unit": "kg", "unit_price": 62.00, "gst_rate": 18},
        {"line": 2, "description": "MS Sheet 2mm 1250x2500", "hsn": "7208", "qty": 50, "unit": "nos", "unit_price": 2850.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0088", "V001", "2026-09-15", "5010 · Raw Material - Steel", "CC-PROD-01", [
        {"line": 1, "description": "MS Angle 50x50x5mm", "hsn": "7216", "qty": 3000, "unit": "kg", "unit_price": 64.00, "gst_rate": 18},
    ], status="closed"),
    _po("PO-2026-0142", "V002", "2026-09-01", "5040 · Packing Material", "CC-DISP-02", [
        {"line": 1, "description": "Corrugated Box 5-ply 400x300x250", "hsn": "4819", "qty": 20000, "unit": "nos", "unit_price": 21.50, "gst_rate": 18},
        {"line": 2, "description": "EPE Foam Insert 380x280", "hsn": "3921", "qty": 10000, "unit": "nos", "unit_price": 7.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0157", "V003", "2026-10-28", "6120 · IT Support & Licences", "CC-ADMIN-01", [
        {"line": 1, "description": "Managed IT Support - November 2026", "hsn": "998313", "qty": 1, "unit": "month", "unit_price": 185000.00, "gst_rate": 18},
        {"line": 2, "description": "Cloud Backup Licence (per user)", "hsn": "998315", "qty": 25, "unit": "users", "unit_price": 1200.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0163", "V004", "2026-10-30", "5220 · Electrical Maintenance", "CC-MAINT-01", [
        {"line": 1, "description": "MCB 32A 4-Pole C-Curve", "hsn": "8536", "qty": 40, "unit": "nos", "unit_price": 1450.00, "gst_rate": 18},
        {"line": 2, "description": "Copper Cable 4 sq mm FRLS (90m coil)", "hsn": "8544", "qty": 10, "unit": "coil", "unit_price": 3900.00, "gst_rate": 18},
        {"line": 3, "description": "LED Panel Light 40W 2x2", "hsn": "9405", "qty": 60, "unit": "nos", "unit_price": 980.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0119", "V004", "2026-10-05", "5220 · Electrical Maintenance", "CC-MAINT-01", [
        {"line": 1, "description": "Copper Cable 2.5 sq mm FRLS (90m coil)", "hsn": "8544", "qty": 8, "unit": "coil", "unit_price": 2450.00, "gst_rate": 18},
        {"line": 2, "description": "Modular Switch 16A", "hsn": "8536", "qty": 200, "unit": "nos", "unit_price": 145.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0171", "V005", "2026-10-12", "5110 · Consumable Tools", "CC-PROD-02", [
        {"line": 1, "description": "Carbide End Mill 10mm 4-Flute", "hsn": "8207", "qty": 30, "unit": "nos", "unit_price": 2350.00, "gst_rate": 18},
        {"line": 2, "description": "Turning Insert CNMG 120408", "hsn": "8209", "qty": 200, "unit": "nos", "unit_price": 410.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0175", "V005", "2026-10-22", "5110 · Consumable Tools", "CC-PROD-02", [
        {"line": 1, "description": "HSS Drill Bit 8mm", "hsn": "8207", "qty": 100, "unit": "nos", "unit_price": 185.00, "gst_rate": 18},
        {"line": 2, "description": "Tap Set M10 (3 pc)", "hsn": "8207", "qty": 20, "unit": "set", "unit_price": 1250.00, "gst_rate": 18},
    ]),
    _po("PO-2026-0180", "V006", "2026-08-02", "6310 · Freight Inward", "CC-LOG-01", [
        {"line": 1, "description": "Full Truck Load Freight Kochi-Bengaluru", "hsn": "996511", "qty": 2, "unit": "trip", "unit_price": 38000.00, "gst_rate": 18},
    ]),
]

# Goods receipts / service entry sheets: what physically arrived (the third leg of the 3-way match).
GRNS = [
    {"grn": "GRN-26-1101", "po_number": "PO-2026-0101", "date": "2026-11-08", "lines": [{"line": 1, "received": 2000}, {"line": 2, "received": 50}]},
    {"grn": "GRN-26-0921", "po_number": "PO-2026-0088", "date": "2026-09-28", "lines": [{"line": 1, "received": 3000}]},
    {"grn": "GRN-26-0915", "po_number": "PO-2026-0142", "date": "2026-09-10", "lines": [{"line": 1, "received": 10000}]},
    {"grn": "GRN-26-1004", "po_number": "PO-2026-0142", "date": "2026-10-04", "lines": [{"line": 1, "received": 6000}, {"line": 2, "received": 5000}]},
    {"grn": "GRN-26-1109", "po_number": "PO-2026-0142", "date": "2026-11-09", "lines": [{"line": 1, "received": 4000}, {"line": 2, "received": 5000}]},
    {"grn": "SES-26-1130", "po_number": "PO-2026-0157", "date": "2026-11-15", "lines": [{"line": 1, "received": 1}, {"line": 2, "received": 25}]},
    {"grn": "GRN-26-1112", "po_number": "PO-2026-0163", "date": "2026-11-12", "lines": [{"line": 1, "received": 40}, {"line": 2, "received": 10}, {"line": 3, "received": 60}]},
    {"grn": "GRN-26-1010", "po_number": "PO-2026-0119", "date": "2026-10-10", "lines": [{"line": 1, "received": 8}, {"line": 2, "received": 200}]},
    {"grn": "GRN-26-1020", "po_number": "PO-2026-0171", "date": "2026-10-20", "lines": [{"line": 1, "received": 30}, {"line": 2, "received": 150}]},
    {"grn": "GRN-26-1029", "po_number": "PO-2026-0175", "date": "2026-10-29", "lines": [{"line": 1, "received": 100}, {"line": 2, "received": 20}]},
    {"grn": "GRN-26-0810", "po_number": "PO-2026-0180", "date": "2026-08-10", "lines": [{"line": 1, "received": 2}]},
]

# Invoices already booked in the AP ledger before this system went live.
# `lines` = quantities billed per PO line (used for cumulative PO / GRN consumption).
INVOICE_LEDGER = [
    {"vendor_id": "V001", "invoice_number": "SGS/26-27/0087", "invoice_date": "2026-10-03", "po_number": "PO-2026-0088",
     "taxable": 192000.00, "total": 226560.00, "status": "paid", "paid_on": "2026-10-30", "lines": [{"line": 1, "qty": 3000}]},
    {"vendor_id": "V002", "invoice_number": "VPS/2026/311", "invoice_date": "2026-09-12", "po_number": "PO-2026-0142",
     "taxable": 215000.00, "total": 253700.00, "status": "paid", "paid_on": "2026-10-25", "lines": [{"line": 1, "qty": 10000}]},
    {"vendor_id": "V002", "invoice_number": "VPS/2026/347", "invoice_date": "2026-10-08", "po_number": "PO-2026-0142",
     "taxable": 164000.00, "total": 193520.00, "status": "approved", "paid_on": None, "lines": [{"line": 1, "qty": 6000}, {"line": 2, "qty": 5000}]},
    {"vendor_id": "V004", "invoice_number": "DE/1021", "invoice_date": "2026-10-11", "po_number": "PO-2026-0119",
     "taxable": 19600.00, "total": 23128.00, "status": "paid", "paid_on": "2026-11-05", "lines": [{"line": 1, "qty": 8}]},
]


def vendor_by_id(vid):
    return next((v for v in VENDORS if v["id"] == vid), None)


def po_by_number(num):
    if not num:
        return None
    key = "".join(ch for ch in str(num).upper() if ch.isalnum())
    for po in PURCHASE_ORDERS:
        if "".join(ch for ch in po["po_number"] if ch.isalnum()) == key:
            return po
    # tolerate "PO 163", "2026/0163", "0163"
    digits = "".join(ch for ch in key if ch.isdigit())
    if digits:
        for po in PURCHASE_ORDERS:
            if po["po_number"].endswith(digits[-4:].zfill(4)):
                return po
    return None


def snapshot():
    return {"buyer": BUYER, "policy": POLICY, "vendors": VENDORS, "purchase_orders": PURCHASE_ORDERS,
            "grns": GRNS, "invoice_ledger": INVOICE_LEDGER}
