"""Test invoices + ground-truth labels.

DEMO cases are the ones shown in the video / live interview.
EVAL cases widen coverage for the accuracy report (and seed the dashboard history).
`expected` is what a careful senior AP analyst would decide — the label we score against.
"""

NIMBUS_NEW_BANK = {"account_name": "Nimbus IT Services", "account_number": "50100498765432",
                   "ifsc": "HDFC0009912", "bank_name": "HDFC Bank, Lower Parel, Mumbai"}

CASES = [
    # ───────────── DEMO ─────────────
    {"id": "D1_happy_path", "kind": "demo", "title": "Happy path — clean steel invoice, 3-way match",
     "vendor": "V001", "template": "classic", "scanned": False,
     "invoice_number": "SGS/26-27/0112", "date": "2026-11-10", "po": "PO-2026-0101",
     "lines": [("MS Round Bar 12mm (IS 2062)", "7214", 2000, "kg", 62.00),
               ("MS Sheet 2mm 1250x2500", "7208", 50, "nos", 2850.00)],
     "expected": {"decision": "APPROVED", "po": "PO-2026-0101", "total": 314470.00}},

    {"id": "D2_split_po_overbilling", "kind": "demo", "title": "Edge: 3rd split invoice overbills the PO",
     "vendor": "V002", "template": "modern", "scanned": False,
     "invoice_number": "VPS/2026/402", "date": "2026-11-12", "po": "PO-2026-0142",
     "lines": [("Corrugated Box 5-ply 400x300x250", "4819", 6000, "nos", 21.50),
               ("EPE Foam Insert 380x280", "3921", 4000, "nos", 7.00)],
     "expected": {"decision": "PARTIALLY_APPROVED", "po": "PO-2026-0142", "total": 185260.00}},

    {"id": "D3_bank_details_changed", "kind": "demo", "title": "Edge: perfect invoice, but bank account changed (BEC fraud pattern)",
     "vendor": "V003", "template": "services", "scanned": False,
     "invoice_number": "NIT/26-27/0458", "date": "2026-11-16", "po": "PO-2026-0157",
     "lines": [("Managed IT Support - November 2026", "998313", 1, "month", 185000.00),
               ("Cloud Backup Licence (per user)", "998315", 25, "users", 1200.00)],
     "bank_override": NIMBUS_NEW_BANK,
     "note": "IMPORTANT: Our banking details have changed w.e.f. 01-Nov-2026. Kindly update your records and remit all payments to the new account below. Old account is no longer operational.",
     "expected": {"decision": "ON_HOLD", "po": "PO-2026-0157", "total": 253700.00}},

    {"id": "D4_scanned_no_po", "kind": "demo", "title": "Edge: scanned invoice, no PO number — PO must be inferred",
     "vendor": "V004", "template": "shop", "scanned": True,
     "invoice_number": "DE/1147", "date": "2026-11-13", "po": None,
     "po_hint": "Ref: Your order dt. 30/10/2026 (Mr. Suresh, Maintenance)",
     "lines": [("MCB 32A 4P C-curve", "8536", 40, "nos", 1450.00),
               ("Cu cable 4 sqmm FRLS - 90m coil", "8544", 10, "coil", 3900.00),
               ("LED panel 40W 2x2", "9405", 60, "nos", 980.00)],
     "expected": {"decision": "APPROVED", "po": "PO-2026-0163", "total": 183844.00}},

    {"id": "D5_disguised_duplicate", "kind": "demo", "title": "Edge: re-sent duplicate — new number format, new date, scanned",
     "vendor": "V001", "template": "classic", "scanned": True,
     "invoice_number": "SGS-2627-87", "date": "2026-11-14", "po": "PO-2026-0088",
     "lines": [("MS Angle 50x50x5mm", "7216", 3000, "kg", 64.00)],
     "expected": {"decision": "REJECTED", "po": "PO-2026-0088", "total": 226560.00}},

    # ───────────── EVAL ─────────────
    {"id": "E01_price_within_tolerance", "kind": "eval", "title": "Unit price +1.7% vs PO (within 2% tolerance)",
     "vendor": "V005", "template": "modern", "scanned": False,
     "invoice_number": "PTW/2627/0219", "date": "2026-11-05", "po": "PO-2026-0171",
     "lines": [("Carbide End Mill 10mm 4-Flute", "8207", 30, "nos", 2390.00)],
     "expected": {"decision": "APPROVED", "po": "PO-2026-0171", "total": 84606.00}},

    {"id": "E02_price_over_tolerance", "kind": "eval", "title": "Unit price +11.2% vs PO",
     "vendor": "V005", "template": "modern", "scanned": False,
     "invoice_number": "PTW/2627/0236", "date": "2026-11-11", "po": "PO-2026-0175",
     "lines": [("HSS Drill Bit 8mm", "8207", 100, "nos", 185.00),
               ("Tap Set M10 (3 pc)", "8207", 20, "set", 1390.00)],
     "expected": {"decision": "ON_HOLD", "po": "PO-2026-0175", "total": 54634.00}},

    {"id": "E03_grn_short_receipt", "kind": "eval", "title": "Billed 200 inserts, only 150 received (3-way mismatch)",
     "vendor": "V005", "template": "modern", "scanned": False,
     "invoice_number": "PTW/2627/0241", "date": "2026-11-14", "po": "PO-2026-0171",
     "lines": [("Turning Insert CNMG 120408", "8209", 200, "nos", 410.00)],
     "expected": {"decision": "PARTIALLY_APPROVED", "po": "PO-2026-0171", "total": 96760.00}},

    {"id": "E04_wrong_gst_type", "kind": "eval", "title": "Inter-state vendor charged CGST+SGST instead of IGST",
     "vendor": "V002", "template": "modern", "scanned": False,
     "invoice_number": "VPS/2026/415", "date": "2026-11-15", "po": "PO-2026-0142",
     "lines": [("EPE Foam Insert 380x280", "3921", 1000, "nos", 7.00)],
     "force_tax_type": "CGST+SGST",
     "expected": {"decision": "REJECTED", "po": "PO-2026-0142", "total": 8260.00}},

    {"id": "E05_invalid_gstin", "kind": "eval", "title": "Vendor GSTIN on invoice fails checksum",
     "vendor": "V004", "template": "shop", "scanned": False,
     "invoice_number": "DE/1152", "date": "2026-11-17", "po": "PO-2026-0163",
     "lines": [("LED panel 40W 2x2", "9405", 60, "nos", 980.00)],
     "gstin_typo": True,
     "expected": {"decision": "REJECTED", "po": "PO-2026-0163", "total": 69384.00}},

    {"id": "E06_unknown_vendor", "kind": "eval", "title": "Vendor not in vendor master, no PO",
     "vendor": "X_SAI", "template": "classic", "scanned": False,
     "invoice_number": "SHP/112/26-27", "date": "2026-11-09", "po": None,
     "lines": [("Hydraulic Hose Assembly 1/2in x 1m", "4009", 12, "nos", 1650.00),
               ("Pneumatic Cylinder 50x100", "8412", 2, "nos", 6200.00)],
     "expected": {"decision": "ON_HOLD", "po": None, "total": 37996.00}},

    {"id": "E07_blocked_vendor", "kind": "eval", "title": "Vendor blocked in master (KYC lapsed)",
     "vendor": "V006", "template": "services", "scanned": False,
     "invoice_number": "CLPL/26/0712", "date": "2026-11-06", "po": "PO-2026-0180",
     "lines": [("Full Truck Load Freight Kochi-Bengaluru", "996511", 2, "trip", 38000.00)],
     "expected": {"decision": "ON_HOLD", "po": "PO-2026-0180", "total": 89680.00}},

    {"id": "E08_tax_inclusive_rates", "kind": "eval", "title": "Rates quoted inclusive of GST (must back-calculate)",
     "vendor": "V004", "template": "shop", "scanned": False,
     "invoice_number": "DE/1139", "date": "2026-11-08", "po": "PO-2026-0119",
     "lines": [("Modular Switch 16A", "8536", 200, "nos", 145.00)],
     "tax_inclusive": True,
     "expected": {"decision": "APPROVED", "po": "PO-2026-0119", "total": 34220.00}},

    {"id": "E09_arithmetic_error", "kind": "eval", "title": "Line amount ≠ qty × rate (vendor typo)",
     "vendor": "V001", "template": "classic", "scanned": False,
     "invoice_number": "SGS/26-27/0109", "date": "2026-11-07", "po": "PO-2026-0101",
     "lines": [("MS Round Bar 12mm (IS 2062)", "7214", 2000, "kg", 62.00)],
     "line_amount_override": {0: 142000.00},
     "expected": {"decision": "REJECTED", "po": "PO-2026-0101", "total": 167560.00}},

    {"id": "E10_missing_invoice_number", "kind": "eval", "title": "No invoice number on document",
     "vendor": "V005", "template": "modern", "scanned": False,
     "invoice_number": None, "date": "2026-11-12", "po": "PO-2026-0175",
     "lines": [("HSS Drill Bit 8mm", "8207", 100, "nos", 185.00)],
     "expected": {"decision": "REJECTED", "po": "PO-2026-0175", "total": 21830.00}},
]

UNKNOWN_VENDORS = {
    "X_SAI": {"name": "Sai Hydraulics & Pneumatics", "state": "29", "pan": "BQRPS4471J",
              "address": "No. 18, 3rd Cross, Yeshwanthpur Industrial Suburb, Bengaluru 560022",
              "bank": {"account_name": "Sai Hydraulics & Pneumatics", "account_number": "1023456789", "ifsc": "CNRB0001023", "bank_name": "Canara Bank, Yeshwanthpur"},
              "email": "saihydraulics.blr@gmail.com"},
}
