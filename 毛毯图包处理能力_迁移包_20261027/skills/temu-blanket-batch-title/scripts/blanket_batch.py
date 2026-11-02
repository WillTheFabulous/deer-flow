import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from openpyxl import load_workbook


DEFAULT_IMAGE_ROOT = Path("C:/Users/19166/Desktop/AA文件/pod毛毯/轮播图")
DEFAULT_WORKBOOK = Path("C:/Users/19166/Desktop/毛毯标题表.xlsx")
SKU_RE = re.compile(r"^(?:FLR|JUH)\d{4}$", re.IGNORECASE)
BED_USE_RE = re.compile(
    r"(?<![A-Za-z])(bed|bedding|bedspread|bedtime|night's sleep|night sleep|sleeping|sleep|bedroom|bed cover)(?![A-Za-z])",
    re.IGNORECASE,
)


class WorkflowError(Exception):
    pass


def parse_skus(values):
    skus = []
    for value in values or []:
        for part in re.split(r"[\s,;，、]+", value.strip()):
            if not part:
                continue
            match = re.match(r"^((?:FLR|JUH)\d{4})-((?:FLR|JUH)\d{4})$", part, re.IGNORECASE)
            if match:
                if match.group(1)[:3].upper() != match.group(2)[:3].upper():
                    raise WorkflowError(f"Range must use one SKU prefix: {part}")
                prefix = match.group(1)[:3].upper()
                start = int(match.group(1)[3:])
                end = int(match.group(2)[3:])
                step = 1 if end >= start else -1
                for number in range(start, end + step, step):
                    skus.append(f"{prefix}{number:04d}")
            else:
                skus.append(part.upper())
    seen = set()
    ordered = []
    for sku in skus:
        if sku not in seen:
            seen.add(sku)
            ordered.append(sku)
    return ordered


def load_rows(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise WorkflowError("Rows JSON must contain a non-empty list.")

    normalized = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise WorkflowError(f"Row {index} must be an object.")
        sku = str(row.get("sku") or "").upper().strip()
        title = str(row.get("title") or "").strip()
        if not SKU_RE.fullmatch(sku):
            raise WorkflowError(f"Row {index} has an invalid SKU: {sku!r}.")
        if sku in seen:
            raise WorkflowError(f"Rows JSON contains a duplicate SKU: {sku}.")
        if not title:
            raise WorkflowError(f"Row {index} has an empty title.")
        if row.get("is_custom") or row.get("custom") or title.lower().startswith("custom "):
            raise WorkflowError(f"Custom product {sku} must be reported separately and cannot be written to the workbook.")
        seen.add(sku)
        normalized.append({"sku": sku, "title": title})
    return normalized


def inventory(args):
    root = Path(args.image_root)
    if args.all:
        skus = sorted(path.name.upper() for path in root.iterdir() if path.is_dir() and SKU_RE.match(path.name))
    else:
        skus = parse_skus(args.sku)
    rows = []
    for sku in skus:
        image = root / sku / args.reference_image
        rows.append({
            "sku": sku,
            "folder": str(root / sku),
            "image": str(image),
            "exists": image.exists(),
        })
    emit(rows, args.output)
    return 0 if all(row["exists"] for row in rows) else 2


def check_lock(args):
    path = Path(args.workbook)
    writable, error = is_workbook_writable(path)
    if writable:
        print(json.dumps({"workbook": str(path), "writable": True}, ensure_ascii=False))
        return 0

    result = {
        "workbook": str(path),
        "writable": False,
        "error": error,
        "note": "Run this check only after exact filesystem write permission has been granted; otherwise sandbox denial and an Office lock cannot be distinguished.",
    }
    if args.close_office_on_lock:
        result["close_attempts"] = close_office_processes()
        for _ in range(max(args.wait_seconds, 0) * 2):
            time.sleep(0.5)
            writable, error = is_workbook_writable(path)
            if writable:
                result.update({"writable": True, "error": None})
                print(json.dumps(result, ensure_ascii=False))
                return 0
        result["error_after_close"] = error

    print(json.dumps(result, ensure_ascii=False))
    return 1


def is_workbook_writable(path):
    try:
        with open(path, "r+b"):
            pass
        return True, None
    except OSError as exc:
        return False, str(exc)


def close_office_processes():
    attempts = []
    for name in ("wps.exe", "et.exe", "EXCEL.EXE"):
        completed = subprocess.run(
            ["taskkill", "/F", "/IM", name, "/T"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        attempts.append({
            "process": name,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        })
    return attempts


def last_nonempty_ab(sheet):
    for row_index in range(sheet.max_row, 0, -1):
        if sheet.cell(row_index, 1).value is not None or sheet.cell(row_index, 2).value is not None:
            return row_index
    return 0


def existing_skus(sheet, last_row):
    result = {}
    for row_index in range(1, last_row + 1):
        value = sheet.cell(row_index, 1).value
        if value is not None:
            result.setdefault(str(value).upper().strip(), []).append(row_index)
    return result


def write(args):
    rows = load_rows(args.rows_json)
    workbook_path = Path(args.workbook)
    workbook = load_workbook(workbook_path)
    sheet = workbook.active
    mode = "clear" if args.clear else args.mode
    previous_last_row = last_nonempty_ab(sheet)

    if mode == "append":
        present = existing_skus(sheet, previous_last_row)
        duplicates = [{"sku": row["sku"], "rows": present[row["sku"]]} for row in rows if row["sku"] in present]
        if duplicates:
            workbook.close()
            raise WorkflowError(f"Append aborted because SKU values already exist: {json.dumps(duplicates, ensure_ascii=False)}")
        start_row = previous_last_row + 1
    else:
        start_row = 1
        for row_index in range(1, sheet.max_row + 1):
            sheet.cell(row_index, 1).value = None
            sheet.cell(row_index, 2).value = None

    for offset, row in enumerate(rows):
        row_index = start_row + offset
        sheet.cell(row_index, 1, row["sku"])
        sheet.cell(row_index, 2, row["title"])
        sheet.cell(row_index, 1).number_format = "@"
        sheet.cell(row_index, 2).number_format = "@"

    workbook.save(workbook_path)
    workbook.close()
    result = {
        "workbook": str(workbook_path),
        "mode": mode,
        "previous_last_row": previous_last_row,
        "start_row": start_row,
        "end_row": start_row + len(rows) - 1,
        "rows_written": len(rows),
    }
    emit(result, args.output)
    return 0


def find_sequence_start(sheet, expected):
    candidates = []
    max_start = max(0, sheet.max_row - len(expected) + 1)
    expected_skus = [row["sku"] for row in expected]
    for start_row in range(1, max_start + 1):
        actual = [str(sheet.cell(start_row + offset, 1).value or "").upper().strip() for offset in range(len(expected))]
        if actual == expected_skus:
            candidates.append(start_row)
    if len(candidates) != 1:
        raise WorkflowError(f"Expected one contiguous SKU sequence in append mode, found {len(candidates)} at rows {candidates}.")
    return candidates[0]


def verify(args):
    expected = load_rows(args.rows_json)
    workbook = load_workbook(Path(args.workbook), read_only=True, data_only=True)
    sheet = workbook.active

    if args.receipt_json:
        receipt = json.loads(Path(args.receipt_json).read_text(encoding="utf-8"))
        start_row = int(receipt["start_row"])
        mode = receipt["mode"]
    else:
        mode = args.mode
        start_row = 1 if mode == "clear" else find_sequence_start(sheet, expected)

    checks = []
    for offset, expected_row in enumerate(expected):
        row_index = start_row + offset
        sku = sheet.cell(row_index, 1).value
        title = sheet.cell(row_index, 2).value or ""
        first_five = " ".join(str(title).split()[:5])
        checks.append({
            "row": row_index,
            "sku": sku,
            "expected_sku": expected_row["sku"],
            "sku_ok": sku == expected_row["sku"],
            "title_ok": title == expected_row["title"],
            "flannel_blanket_first_five": "Flannel Blanket" in first_five,
            "gsm_once": title.count("280GSM") == 1,
            "under_250": len(title) < 250,
            "no_bed_use": BED_USE_RE.search(title) is None,
        })

    end_row = start_row + len(expected) - 1
    clear_has_no_trailing_values = mode != "clear" or last_nonempty_ab(sheet) == end_row
    workbook.close()
    ok = clear_has_no_trailing_values and all(
        item["sku_ok"]
        and item["title_ok"]
        and item["flannel_blanket_first_five"]
        and item["gsm_once"]
        and item["under_250"]
        and item["no_bed_use"]
        for item in checks
    )
    result = {
        "ok": ok,
        "mode": mode,
        "start_row": start_row,
        "end_row": end_row,
        "rows_checked": len(expected),
        "clear_has_no_trailing_values": clear_has_no_trailing_values,
        "checks": checks,
    }
    emit(result, args.output)
    return 0 if ok else 3


def emit(data, output):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def build_parser():
    parser = argparse.ArgumentParser(description="Batch helpers for Temu blanket title workflows.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--image-root", default=str(DEFAULT_IMAGE_ROOT))
    inventory_parser.add_argument("--reference-image", default="1.jpg")
    inventory_parser.add_argument("--sku", action="append")
    inventory_parser.add_argument("--all", action="store_true")
    inventory_parser.add_argument("--output")
    inventory_parser.set_defaults(func=inventory)

    lock_parser = subparsers.add_parser("check-lock")
    lock_parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    lock_parser.add_argument("--close-office-on-lock", action="store_true")
    lock_parser.add_argument("--wait-seconds", type=int, default=10)
    lock_parser.set_defaults(func=check_lock)

    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    write_parser.add_argument("--rows-json", required=True)
    write_parser.add_argument("--mode", choices=("clear", "append"), default="clear")
    write_parser.add_argument("--clear", action="store_true", help="Deprecated alias for --mode clear.")
    write_parser.add_argument("--output", help="Optional JSON write receipt.")
    write_parser.set_defaults(func=write)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    verify_parser.add_argument("--rows-json", required=True)
    verify_parser.add_argument("--mode", choices=("clear", "append"), default="clear")
    verify_parser.add_argument("--receipt-json")
    verify_parser.add_argument("--output")
    verify_parser.set_defaults(func=verify)

    return parser


def main():
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (WorkflowError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
