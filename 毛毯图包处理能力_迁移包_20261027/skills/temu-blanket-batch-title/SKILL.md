---
name: temu-blanket-batch-title
description: Batch-generate Temu POD flannel blanket English titles from local FLR or JUH SKU image folders and fill the corresponding fixed store title workbook. Use when the user asks to process SKU lists/ranges, find blanket images under a store carousel folder, generate titles, write rows, or check missing 1.jpg images.
---

# Temu Blanket Batch Title

Use this skill for repeatable Temu blanket title batches.

## Defaults

- FLR / 1店 image root: `C:\Users\19166\Desktop\AA文件\pod毛毯\1店轮播图`
- JUH / 2店 image root: `C:\Users\19166\Desktop\AA文件\pod毛毯\2店轮播图`
- Reference image: `1.jpg` inside each `FLRxxxx` folder
- FLR / 1店 workbook: `C:\Users\19166\Desktop\毛毯标题表.xlsx`
- JUH / 2店 workbook: `C:\Users\19166\Desktop\毛毯2店标题表.xlsx`
- Output: active sheet, column A = SKU, column B = title
- Header: none
- Write mode: clear columns A:B by default; append only when the user explicitly asks to continue/append

## Required Component

Use `temu-blanket-title` as the title component.

Before generating titles, read:

- `C:\Users\19166\.codex\skills\temu-blanket-title\SKILL.md`
- `C:\Users\19166\.codex\skills\temu-blanket-title\references\compliance.md`
- `C:\Users\19166\.codex\skills\temu-blanket-title\references\holiday-calendar.md`

Apply its hard rules to every generated title:

- One English title per SKU.
- `Flannel Blanket` appears within the first 5 words.
- `280GSM` appears exactly once.
- Title length is under 250 characters.
- Use only `Flannel` as explicit material wording.
- Avoid banned material, IP/brand, minor-related, medical, allergy, platform residue, unsafe pet blanket wording, and bed-use wording.
- Do not use `bed`, `bedding`, `bedspread`, `bedtime`, `night's sleep`, `night sleep`, `sleeping`, `sleep`, `bedroom`, or `bed cover`.
- Use fuller scene phrases when length allows, such as `For Sofa Living Room Office Travel Car` or `For Couch Living Room Office Travel Camping Car`.

## Workflow

1. Resolve SKUs.
   - Accept explicit lists like `FLR0140, FLR0141`.
   - Accept ranges like `FLR0140-FLR0150`.
   - Preserve the user's SKU order.

2. Inventory images.
   - Run `scripts/blanket_batch.py inventory`.
   - Process only folder names matching `FLR` or `JUH` plus four digits.
   - Use only `{image_root}\{SKU}\1.jpg`.
   - Do not silently substitute other images when `1.jpg` is missing.

3. Inspect images and generate titles.
   - View each valid `1.jpg`.
   - Determine custom/text/graphic/mixed lead type, holiday-led status, and gift-led status.
   - If the image clearly shows replaceable name/photo/date/text, start the title with `Custom`.
   - Record ordinary products as JSON objects with `sku` and `title`.
   - Keep custom products in a separate chat/report list. Never include them in the workbook rows JSON. The write helper rejects `Custom` titles and rows marked `custom` or `is_custom`.

4. Obtain exact filesystem access.
   - Before extracting images or checking the workbook lock, request read/write access to only the configured image root and workbook path when the environment requires permission.
   - Do not interpret a sandbox `PermissionError` as an Office lock. Run `check-lock` only after exact workbook write access is granted.

5. Write workbook.
   - Use the dedicated helper directly for this fixed two-column workbook. Do not load a generic spreadsheet authoring workflow unless the workbook itself needs structural or formatting repair.
   - First run `scripts/blanket_batch.py check-lock` after permission is granted.
   - If the workbook is locked, ask the user once for permission to close WPS/Excel for that lock event. If the user approves, immediately run `scripts/blanket_batch.py check-lock --close-office-on-lock --wait-seconds 10` and continue automatically; do not ask again for the same lock. A later/new lock event requires a fresh confirmation.
   - Default to `scripts/blanket_batch.py write --rows-json <rows.json> --mode clear --output <receipt.json>`.
   - Use `--mode append` only when the user explicitly asks to continue or append. Append starts after the last non-empty cell in columns A:B, ignoring styled blank rows.
   - Append mode aborts before saving if any incoming SKU already exists.
   - Select the workbook by prefix: FLR → `毛毯标题表.xlsx`; JUH → `毛毯2店标题表.xlsx`. Directly modify the selected workbook unless the user asks for a copy.
   - Do not write a header row.

6. Handle locks carefully.
   - `check-lock --close-office-on-lock` attempts to close `wps.exe`, `et.exe`, and `EXCEL.EXE`, then repeatedly checks whether the workbook can be opened for write access.
   - If Windows refuses to close a process or the workbook remains locked, report the close attempt results and continue only with a verified writable workbook or a clearly named fallback output.
   - Close WPS/Excel only if the user explicitly permits it for the current lock event; once permitted, handle the close-and-retry automatically for that lock. Ask again only for a later/new lock event.

7. Verify.
   - Run `scripts/blanket_batch.py verify --rows-json <rows.json> --receipt-json <receipt.json>`.
   - Confirm row count, SKU order, exact title match, `Flannel Blanket`, one `280GSM`, length under 250, and no bed-use wording such as `bed`, `bedding`, `bedspread`, `bedtime`, `night's sleep`, `night sleep`, `sleeping`, `sleep`, `bedroom`, or `bed cover`.
   - Summarize missing images and validation failures separately.

## Script Notes

Use the bundled Python runtime when system `python` is unavailable:

```powershell
& 'C:\Users\19166\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'C:\Users\19166\.codex\skills\temu-blanket-batch-title\scripts\blanket_batch.py' inventory --sku FLR0140,FLR0141
```
