"""Command line.

    python main.py check                 # download new CDSCO lists and check the inventory
    python main.py recheck               # check the inventory against stored lists only
    python main.py import list.pdf       # read a CDSCO PDF you downloaded
    python main.py inventory stock.csv   # import medicines (name,manufacturer,batch,expiry,quantity,location)
    python main.py alerts                # show open alerts
    python main.py --mermaid             # print the LangGraph flow
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from config import setup_logging


def main() -> int:
    p = argparse.ArgumentParser(description="MedAlert - substandard-medicine alert agent")
    p.add_argument("command", nargs="?", choices=["check", "recheck", "import", "inventory", "alerts"])
    p.add_argument("path", nargs="?")
    p.add_argument("--mermaid", action="store_true")
    args = p.parse_args()
    setup_logging()

    import storage
    from pipeline import build_graph, import_pdf_bytes, run_check

    if args.mermaid:
        print(build_graph().get_graph().draw_mermaid())
        return 0
    if args.command in ("check", "recheck"):
        result = run_check("cdsco" if args.command == "check" else "none")
        print(result["summary"])
        return 0 if result["status"] == "done" else 1
    if args.command == "import":
        print(import_pdf_bytes(Path(args.path).read_bytes(), title=Path(args.path).stem))
        return 0
    if args.command == "inventory":
        from models import InventoryItem
        n = 0
        with open(args.path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
                storage.add_item(InventoryItem(name=row["name"], manufacturer=row.get("manufacturer", ""),
                                               batch=row.get("batch", ""), expiry=row.get("expiry", ""),
                                               quantity=int(float(row.get("quantity") or 0)),
                                               location=row.get("location", "")))
                n += 1
        print(f"added {n} medicines")
        return 0
    if args.command == "alerts":
        for a in storage.get_alerts("new"):
            d = a["draft"] or {}
            print(f"[{d.get('severity', '?'):8}] [{a['level']:6}] {a['inventory']['name']} "
                  f"<-> {a['entry']['drug_name']} batch {a['entry']['batch']}\n    {d.get('summary', a['why'])}")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
