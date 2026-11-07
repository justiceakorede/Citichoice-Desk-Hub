"""Regenerates app/data/iso4217.json. Needs `pip install iso4217` (build-time only; the hub does not depend on it).
Exponents come from the ISO 4217 list as packaged by `iso4217` (NOT CLDR, whose digit counts differ for several
currencies, e.g. IQD, ALL, LAK). Codes WiseCP knows that are not in the current ISO list are kept, flagged, and
assumed to have 2 decimals; they cannot be used for billing until staff decide to enable them."""
import json
import sys

import iso4217
from iso4217 import Currency

WISECP_ONLY = ["ANG", "BGN", "CUC", "GGP", "HRK", "IMP", "JEP", "LTL", "SLL", "VEF", "YUN", "ZWL"]   # from the 166-row WiseCP table
rows = [{"code": c.code, "numeric": f"{c.number:03d}" if c.number else None, "name": c.currency_name,
         "exponent": c.exponent, "iso_status": "iso"} for c in Currency]
have = {r["code"] for r in rows}
rows += [{"code": c, "numeric": None, "name": c, "exponent": 2, "iso_status": "not_in_current_iso"}
         for c in WISECP_ONLY if c not in have]
rows.sort(key=lambda r: r["code"])
json.dump({"source": f"iso4217 package {iso4217.__version__}; WiseCP-only codes flagged not_in_current_iso with assumed exponent 2",
           "currencies": rows}, open(sys.argv[1] if len(sys.argv) > 1 else "app/data/iso4217.json", "w"), indent=1)
print(len(rows), "currencies written")
