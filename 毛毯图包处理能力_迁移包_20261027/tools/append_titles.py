import argparse
import json
from pathlib import Path
from openpyxl import load_workbook


def main():
    parser = argparse.ArgumentParser(description="Append arbitrary SKU/title rows to a two-column workbook.")
    parser.add_argument("--rows-json", required=True)
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    rows = json.loads(Path(args.rows_json).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SystemExit("rows JSON must contain a non-empty list")
    wb_path = Path(args.workbook)
    wb = load_workbook(wb_path)
    ws = wb.active
    last = 0
    existing = set()
    for r in range(1, ws.max_row + 1):
        value = ws.cell(r, 1).value
        if value is not None and str(value).strip():
            last = r
            existing.add(str(value).strip().upper())
    duplicates = []
    for row in rows:
        sku = str(row.get("sku") or "").strip()
        title = str(row.get("title") or "").strip()
        if not sku or not title:
            raise SystemExit("each row needs non-empty sku and title")
        if sku.upper() in existing:
            duplicates.append(sku)
    if duplicates:
        raise SystemExit(f"duplicate SKU values already exist: {duplicates}")

    start = last + 1
    for offset, row in enumerate(rows):
        r = start + offset
        ws.cell(r, 1, str(row["sku"]).strip())
        ws.cell(r, 2, str(row["title"]).strip())
        ws.cell(r, 1).number_format = "@"
        ws.cell(r, 2).number_format = "@"
    wb.save(wb_path)
    wb.close()

    receipt = {
        "workbook": str(wb_path),
        "mode": "append",
        "previous_last_row": last,
        "start_row": start,
        "end_row": start + len(rows) - 1,
        "rows_written": len(rows),
    }
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
