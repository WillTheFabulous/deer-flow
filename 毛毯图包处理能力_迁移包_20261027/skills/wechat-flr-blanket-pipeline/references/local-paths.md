# Local Paths

Use these defaults unless the user gives a different path.

- TraceMemo bootstrap cache: `C:\Users\19166\AppData\Roaming\TraceMemo\cache\bootstrap`
- Local downloaded blanket package folder: `C:\Users\19166\Desktop\毛毯图包压缩包`
- WeChat received file folder, legacy only when WeChat is explicitly requested: `D:\微信聊天记录\xwechat_files\wxid_1rjx8bswh70d22_aa18\msg\file\2026-08`
- FLR / 1店 carousel root: `C:\Users\19166\Desktop\AA文件\pod毛毯\1店轮播图`
- JUH / 2店 carousel root: `C:\Users\19166\Desktop\AA文件\pod毛毯\2店轮播图`
- FLR / 1店 title workbook: `C:\Users\19166\Desktop\毛毯标题表.xlsx`
- JUH / 2店 title workbook: `C:\Users\19166\Desktop\毛毯2店标题表.xlsx`
- Batch title helper: `C:\Users\19166\.codex\skills\temu-blanket-batch-title\scripts\blanket_batch.py`

By default, scan the local downloaded blanket package folder for export/bundle `.zip` files, then find internal `FLRxxxx` and `JUHxxxx` folders. Route FLR to 1店 and JUH to 2店. The folder may contain one large archive instead of per-SKU zip files or subfolders.

TraceMemo can lag behind WeChat file downloads. If `query-messages` finds no FLR file messages but `list-zips --date today` finds a continuous fresh FLR sequence, treat the local zip list as the operational source and mention that TraceMemo had not refreshed.
