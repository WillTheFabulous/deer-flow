import argparse
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_TRACEMEMO_BOOTSTRAP = Path(r"C:\Users\19166\AppData\Roaming\TraceMemo\cache\bootstrap")
DEFAULT_WECHAT_FILE_DIR = Path(r"D:\微信聊天记录\xwechat_files\wxid_1rjx8bswh70d22_aa18\msg\file\2026-08")
DEFAULT_DOWNLOAD_PACKAGE_DIR = Path(r"C:\Users\19166\Desktop\毛毯图包压缩包")
DEFAULT_CAROUSEL_ROOT = Path(r"C:\Users\19166\Desktop\AA文件\pod毛毯\轮播图")
SKU_ZIP_RE = re.compile(r"^((?:FLR|JUH)\d{4})\.zip$", re.IGNORECASE)
SKU_MEMBER_RE = re.compile(r"(?:^|/)((?:FLR|JUH)\d{4})(?:/|$)", re.IGNORECASE)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def date_window(label):
    today = datetime.now().date()
    if not label or label.lower() in ("today", "今天"):
        start = datetime.combine(today, datetime.min.time())
    elif label.lower() in ("yesterday", "昨天"):
        start = datetime.combine(today - timedelta(days=1), datetime.min.time())
    else:
        start = datetime.strptime(label, "%Y-%m-%d")
    return start, start + timedelta(days=1)


def latest_tracememo_root(base):
    roots = [p for p in base.iterdir() if p.is_dir()]
    if not roots:
        raise FileNotFoundError(f"No TraceMemo bootstrap cache found under {base}")
    return max(roots, key=lambda p: p.stat().st_mtime)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def contact_name(contact):
    return (
        contact.get("remark")
        or contact.get("wechatNickname")
        or contact.get("m_nsNickName")
        or contact.get("m_nsUsrName")
        or ""
    )


def build_contacts(root):
    startup = load_json(root / "startup.json")
    contacts = {}
    for contact in startup.get("contacts", []):
        md5 = contact.get("md5")
        if md5:
            contacts[md5] = {
                "name": contact_name(contact),
                "username": contact.get("m_nsUsrName") or "",
                "type": contact.get("type") or "",
            }
    return contacts


def parse_message_datetime(value):
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            pass
    return None


def iter_tracememo_messages(base):
    root = latest_tracememo_root(base)
    contacts = build_contacts(root)
    seen = set()
    for path in sorted((root / "messages").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = load_json(path)
        contact_md5 = (data.get("cacheKey") or "").split(":", 1)[0]
        contact = contacts.get(contact_md5, {})
        for item in data.get("items") or []:
            unique = (
                item.get("sessionId"),
                item.get("serverId"),
                item.get("localId"),
                item.get("createTime"),
                item.get("senderId"),
            )
            if unique in seen:
                continue
            seen.add(unique)
            content_data = item.get("contentData")
            file_title = content_data.get("title") if isinstance(content_data, dict) else ""
            yield {
                "chat": contact.get("name") or item.get("sessionId") or contact_md5,
                "chat_id": contact.get("username") or item.get("sessionId") or "",
                "sender": item.get("name") or item.get("senderId") or "",
                "sender_id": item.get("senderId") or "",
                "datetime": item.get("datetime") or "",
                "dt": parse_message_datetime(item.get("datetime") or ""),
                "type": item.get("type") or "",
                "file_title": file_title or "",
                "content": item.get("content") or "",
            }


def query_messages(args):
    start, end = date_window(args.date)
    messages = []
    for msg in iter_tracememo_messages(Path(args.tracememo_bootstrap)):
        haystack = " ".join([msg["chat"], msg["chat_id"], msg["sender"], msg["sender_id"], msg["type"], msg["file_title"], msg["content"]])
        if args.chat and args.chat.lower() not in (msg["chat"] + " " + msg["chat_id"]).lower():
            continue
        if args.sender and args.sender.lower() not in (msg["sender"] + " " + msg["sender_id"]).lower():
            continue
        if args.keyword and args.keyword.lower() not in haystack.lower():
            continue
        if args.files and not (msg["type"] == "文件" or msg["file_title"].lower().endswith((".zip", ".rar", ".7z"))):
            continue
        if not msg["dt"] or not (start <= msg["dt"] < end):
            continue
        messages.append(msg)
    messages.sort(key=lambda msg: msg["dt"] or datetime.min)
    emit(messages, args.output)


def list_flr_zips(args):
    start, end = date_window(args.date)
    root = Path(args.wechat_file_dir)
    rows = []
    for path in sorted(root.glob("FLR*.zip"), key=lambda p: p.name.upper()):
        match = SKU_ZIP_RE.match(path.name)
        if not match:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if start <= mtime < end:
            rows.append({
                "sku": match.group(1).upper(),
                "zip": str(path),
                "size": path.stat().st_size,
                "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            })
    emit(rows, args.output)


def image_sort_key(member):
    stem = Path(member.replace("\\", "/")).stem
    match = re.search(r"\d+", stem)
    if match:
        return (0, int(match.group(0)), stem.lower())
    return (1, stem.lower())


def iter_bundle_skus(zip_path):
    grouped = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            normalized = member.replace("\\", "/").strip("/")
            if not normalized or normalized.endswith("/"):
                continue
            suffix = Path(normalized).suffix.lower()
            if suffix not in IMAGE_SUFFIXES:
                continue
            match = SKU_MEMBER_RE.search(normalized)
            if not match:
                continue
            sku = match.group(1).upper()
            grouped.setdefault(sku, []).append(member)
    return grouped


def list_local_bundles(args):
    start, end = date_window(args.date)
    root = Path(args.source_dir)
    rows = []
    for path in sorted(root.glob("*.zip"), key=lambda p: (p.stat().st_mtime, p.name)):
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if args.date and not (start <= mtime < end):
            continue
        grouped = iter_bundle_skus(path)
        for sku in sorted(grouped):
            members = sorted(grouped[sku], key=image_sort_key)
            rows.append({
                "sku": sku,
                "zip": str(path),
                "source": "local-bundle",
                "image_count": len(members),
                "first_member": members[0],
                "members": members,
                "size": path.stat().st_size,
                "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            })
    emit(rows, args.output)


def write_member_as_jpg(zf, member, output):
    from PIL import Image

    output.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src:
        image = Image.open(src)
        image.load()
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")
    image.save(output, "JPEG", quality=95)


def extract_local_bundle(row, root):
    sku = row["sku"].upper()
    zip_path = Path(row["zip"])
    target = root / sku
    members = row.get("members") or []
    if not members:
        grouped = iter_bundle_skus(zip_path)
        members = sorted(grouped.get(sku, []), key=image_sort_key)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for idx, member in enumerate(members, start=1):
            write_member_as_jpg(zf, member, target / f"{idx}.jpg")
    image = target / "1.jpg"
    return {
        **row,
        "folder": str(target),
        "image": str(image),
        "has_1jpg": image.exists(),
        "extracted_count": len(members),
    }


def extract_flr_zips(args):
    rows = load_json(Path(args.zips_json))
    out = []
    root = Path(args.carousel_root)
    for row in rows:
        if row.get("source") == "local-bundle" or row.get("members"):
            out.append(extract_local_bundle(row, root))
            continue
        sku = row["sku"].upper()
        zip_path = Path(row["zip"])
        target = root / sku
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target)
        image = target / "1.jpg"
        if not image.exists():
            candidates = sorted(
                [p for p in target.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES],
                key=lambda p: image_sort_key(p.name),
            )
            if candidates:
                from PIL import Image

                with Image.open(candidates[0]) as src:
                    src.convert("RGB").save(image, "JPEG", quality=95)
        out.append({
            **row,
            "folder": str(target),
            "image": str(image),
            "has_1jpg": image.exists(),
        })
    emit(out, args.output)


def make_contact_sheet(args):
    from PIL import Image, ImageDraw

    rows = load_json(Path(args.inventory_json))
    thumb_w, thumb_h = args.thumb_size, args.thumb_size
    label_h = 34
    cols = args.columns
    sheet_rows = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, sheet_rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for idx, row in enumerate(rows):
        image_path = Path(row["image"])
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
        x = (idx % cols) * thumb_w + (thumb_w - image.width) // 2
        y = (idx // cols) * (thumb_h + label_h) + (thumb_h - image.height) // 2
        sheet.paste(image, (x, y))
        lx = (idx % cols) * thumb_w
        ly = (idx // cols) * (thumb_h + label_h) + thumb_h
        draw.rectangle([lx, ly, lx + thumb_w, ly + label_h], fill=(245, 245, 245))
        draw.text((lx + 8, ly + 8), row["sku"], fill=(0, 0, 0))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)
    print(output)


def emit(data, output):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def build_parser():
    parser = argparse.ArgumentParser(description="WeChat FLR blanket zip pipeline helpers.")
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query-messages")
    query.add_argument("--tracememo-bootstrap", default=str(DEFAULT_TRACEMEMO_BOOTSTRAP))
    query.add_argument("--chat")
    query.add_argument("--sender")
    query.add_argument("--date", default="today")
    query.add_argument("--keyword", default="FLR")
    query.add_argument("--files", action="store_true")
    query.add_argument("--output")
    query.set_defaults(func=query_messages)

    zips = sub.add_parser("list-zips")
    zips.add_argument("--wechat-file-dir", default=str(DEFAULT_WECHAT_FILE_DIR))
    zips.add_argument("--date", default="today")
    zips.add_argument("--output")
    zips.set_defaults(func=list_flr_zips)

    local = sub.add_parser("list-local-bundles")
    local.add_argument("--source-dir", default=str(DEFAULT_DOWNLOAD_PACKAGE_DIR))
    local.add_argument("--date", default="today")
    local.add_argument("--output")
    local.set_defaults(func=list_local_bundles)

    extract = sub.add_parser("extract")
    extract.add_argument("--zips-json", required=True)
    extract.add_argument("--carousel-root", default=str(DEFAULT_CAROUSEL_ROOT))
    extract.add_argument("--output")
    extract.set_defaults(func=extract_flr_zips)

    sheet = sub.add_parser("contact-sheet")
    sheet.add_argument("--inventory-json", required=True)
    sheet.add_argument("--output", required=True)
    sheet.add_argument("--columns", type=int, default=4)
    sheet.add_argument("--thumb-size", type=int, default=220)
    sheet.set_defaults(func=make_contact_sheet)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
