---
name: wechat-flr-blanket-pipeline
description: Process FLR and JUH blanket image packs from the local desktop download folder or legacy WeChat/TraceMemo into separate store carousel folders and title workbooks. Use when the user asks to handle blanket image packs, extract carousel images, make titles, or fill either fixed title workbook.
---

# WeChat FLR Blanket Pipeline

Use this skill to run the complete workflow from FLR image pack archives to the finished blanket title workbook.

## Dependencies

This skill coordinates the local FLR file workflow. For title generation and workbook writing, also use:

- `temu-blanket-title`
- `temu-blanket-batch-title`

Read `references/local-paths.md` when resolving folders. Read `references/title-handoff.md` before generating titles.

## Workflow

1. Resolve the request.
   - Default source: local desktop download folder `C:\Users\19166\Desktop\毛毯图包压缩包`.
   - Route SKU prefixes by store: `FLRxxxx` → 1店 (`C:\Users\19166\Desktop\AA文件\pod毛毯\1店轮播图`, `C:\Users\19166\Desktop\毛毯标题表.xlsx`); `JUHxxxx` → 2店 (`C:\Users\19166\Desktop\AA文件\pod毛毯\2店轮播图`, `C:\Users\19166\Desktop\毛毯2店标题表.xlsx`).
   - Use WeChat/TraceMemo only when the user explicitly names a sender, chat/group, or says the packs were sent in WeChat.
   - Default date: today.

2. Locate FLR archives.
   - For the local desktop workflow, run `scripts/wechat_flr.py list-local-bundles --source-dir <download_dir> --date <date> --output <zips.json>`.
   - This scans ordinary export/bundle zip files whose internal folders contain `FLRxxxx` or `JUHxxxx` SKU directories, such as `export_*.zip`.
   - If the user asks to process a specific SKU range, filter the returned rows to that exact range while preserving SKU order.
   - The local desktop folder may contain no subfolders; do not require per-SKU zip files when a bundle zip contains FLR folders.

3. Legacy WeChat/TraceMemo source, only when explicitly requested.
   - Run `scripts/wechat_flr.py query-messages --chat <chat> --sender <sender> --date <date> --files --keyword FLR`.
   - Use this to confirm which FLR zip files were sent when TraceMemo cache is current.
   - Run `scripts/wechat_flr.py list-zips --date <date> --output <zips.json>`.
   - Use the returned `FLRxxxx.zip` rows as the source for extraction.
   - If TraceMemo returns no messages but local zip files are fresh and continuous, proceed with the local zip list and mention the cache lag.

4. Obtain exact filesystem access.
   - Before local archive reading, extraction, or workbook lock checks, request access only to the configured download folder, carousel root, and title workbook when the environment requires permission.
   - Do not treat a sandbox `PermissionError` as a workbook lock.

5. Extract image packs.
   - Split the inventory by prefix before extraction. Run the extractor once for FLR with `--carousel-root C:\Users\19166\Desktop\AA文件\pod毛毯\1店轮播图`, and once for JUH with `--carousel-root C:\Users\19166\Desktop\AA文件\pod毛毯\2店轮播图`.
   - Confirm every extracted SKU has the matching store root `{carousel_root}\{SKU}\1.jpg`.
   - For local bundle zips with `001.png`, `002.png`, etc., extraction normalizes carousel images to `1.jpg`, `2.jpg`, etc.
   - Do not substitute another image for `1.jpg`.

6. Make a contact sheet.
   - Run `scripts/wechat_flr.py contact-sheet --inventory-json <inventory.json> --output <contact.jpg>`.
   - View the contact sheet, then open individual `1.jpg` files when text or theme details are unclear.

7. Generate titles.
   - Use `temu-blanket-title` rules for every SKU.
   - Preserve SKU order from the zip list.
   - Save ordinary products as JSON objects with `sku` and `title`.
   - List custom products separately in chat with SKU and title. Never include custom products in the workbook rows JSON.

8. Write and verify the workbook.
   - Use the dedicated batch helper directly for this fixed two-column workbook; do not load a generic spreadsheet authoring workflow unless the workbook needs structural repair.
   - After exact write permission is granted, run `temu-blanket-batch-title/scripts/blanket_batch.py check-lock`.
   - If the workbook is locked, ask the user once for permission to close WPS/Excel for that lock event. If the user approves, immediately run `check-lock --close-office-on-lock --wait-seconds 10` and continue automatically; do not ask again for the same lock. A later/new lock event requires a fresh confirmation.
   - For FLR, write to `C:\Users\19166\Desktop\毛毯标题表.xlsx`; for JUH, write to `C:\Users\19166\Desktop\毛毯2店标题表.xlsx`.
   - When the user explicitly asks to continue or append, run `blanket_batch.py write --rows-json <rows.json> --mode append --workbook <store_workbook> --output <receipt.json>`.
   - Otherwise use the default clear mode for that store: `blanket_batch.py write --rows-json <rows.json> --mode clear --workbook <store_workbook> --output <receipt.json>`.
   - Run `blanket_batch.py verify --rows-json <rows.json> --workbook <store_workbook> --receipt-json <receipt.json>`.

## Safety

- Only read source archives needed for the requested date/SKU/task.
- Only read WeChat messages/files when the user explicitly asks for the WeChat source.
- Do not delete WeChat zips or original WeChat files unless the user explicitly asks.
- Do not close WPS/Excel until the user explicitly permits it for the current lock event; once permitted, handle the close-and-retry automatically for that lock. Ask again only for a later/new lock event.
- If exact filesystem access is not granted, stop before extraction/write and request it instead of creating an unintended replacement workbook.
- If a workbook remains locked, create a clearly named fallback workbook and report the lock status.

## Command Examples

```powershell
& python scripts/wechat_flr.py list-local-bundles --source-dir C:\Users\19166\Desktop\毛毯图包压缩包 --date today --output zips.json
& python scripts/wechat_flr.py query-messages --chat 跨境项目组 --sender 盘帅 --date today --files --keyword FLR
& python scripts/wechat_flr.py list-zips --date today --output zips.json
& python scripts/wechat_flr.py extract --zips-json zips.json --output inventory.json
& python scripts/wechat_flr.py contact-sheet --inventory-json inventory.json --output contact.jpg
```
