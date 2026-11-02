# 毛毯图包处理能力迁移包

这个包包含当前使用的毛毯图包处理流程：

- 从本地下载目录扫描 `export_*.zip` 图包，并识别内部 SKU 文件夹
- 按 `FLRxxxx` / `JUHxxxx` 分店提取轮播图，统一为 `1.jpg`、`2.jpg` 等
- 生成联系图，检查每个 SKU 是否有 `1.jpg`
- 根据图片生成符合 Temu 规则的英文标题
- 写入并复核固定两列标题表（A=SKU，B=标题）
- 支持 `chao0001` 等非 FLR/JUH 编码追加到标题表

## 包内结构

```text
skills/
  wechat-flr-blanket-pipeline/
  temu-blanket-title/
  temu-blanket-batch-title/
tools/
  append_titles.py
MIGRATION_README.md
```

三个 `skills` 文件夹可以直接复制到新电脑的 `%USERPROFILE%\.codex\skills\` 下。保留文件夹名称不变。

## 新电脑准备

安装 Python 3.10 或更高版本，并安装脚本依赖：

```powershell
python -m pip install pillow openpyxl
```

如果使用 Codex，把 `skills` 下的三个文件夹复制到：

```text
C:\Users\<你的用户名>\.codex\skills\
```

首次使用时，打开对应的 `SKILL.md`，把 `references/local-paths.md` 中的下载目录、轮播图目录和标题表路径改成新电脑的实际路径。

## 标准处理命令

以下命令中的路径都可以替换：

```powershell
python skills\wechat-flr-blanket-pipeline\scripts\wechat_flr.py list-local-bundles `
  --source-dir "C:\Users\<你>\Desktop\毛毯图包压缩包" `
  --date today --output bundles.json
```

按店铺拆分 JSON 后分别提取：

```powershell
python skills\wechat-flr-blanket-pipeline\scripts\wechat_flr.py extract `
  --zips-json flr_bundles.json `
  --carousel-root "C:\Users\<你>\Desktop\AA文件\pod毛毯\1店轮播图" `
  --output flr_inventory.json

python skills\wechat-flr-blanket-pipeline\scripts\wechat_flr.py extract `
  --zips-json juh_bundles.json `
  --carousel-root "C:\Users\<你>\Desktop\AA文件\pod毛毯\2店轮播图" `
  --output juh_inventory.json
```

生成联系图：

```powershell
python skills\wechat-flr-blanket-pipeline\scripts\wechat_flr.py contact-sheet `
  --inventory-json juh_inventory.json --output juh_contact.jpg
```

图片识别和标题生成由模型根据 `temu-blanket-title\SKILL.md` 完成。标题硬规则包括：前 5 个词内出现 `Flannel Blanket`、每条标题恰好一个 `280GSM`、长度小于 250 个字符、避免床品/儿童/品牌/IP 等风险词。

标准 FLR/JUH 标题表写入：

```powershell
python skills\temu-blanket-batch-title\scripts\blanket_batch.py check-lock `
  --workbook "C:\Users\<你>\Desktop\毛毯2店标题表.xlsx"

python skills\temu-blanket-batch-title\scripts\blanket_batch.py write `
  --rows-json rows.json --mode append `
  --workbook "C:\Users\<你>\Desktop\毛毯2店标题表.xlsx" `
  --output receipt.json

python skills\temu-blanket-batch-title\scripts\blanket_batch.py verify `
  --rows-json rows.json `
  --workbook "C:\Users\<你>\Desktop\毛毯2店标题表.xlsx" `
  --receipt-json receipt.json
```

`blanket_batch.py` 的标准校验器只接受 FLR/JUH SKU。对于 `chao0001` 等其他编码，使用：

```powershell
python tools\append_titles.py `
  --rows-json rows_chao.json `
  --workbook "C:\Users\<你>\Desktop\毛毯2店标题表.xlsx" `
  --output receipt_chao.json
```

这个工具会从 A、B 两列最后一个非空行开始追加，检查重复 SKU，并保留原工作簿结构。

## 店铺默认映射

| SKU 前缀 | 轮播图目录 | 标题表 |
|---|---|---|
| FLR | 1店轮播图 | 毛毯标题表.xlsx |
| JUH | 2店轮播图 | 毛毯2店标题表.xlsx |

其他前缀需要先确认店铺，再使用 `append_titles.py` 追加。

## 注意事项

- 原始压缩包只读，不要删除。
- 提取前先按前缀拆分，避免把不同店铺写入同一目录。
- 每个 SKU 必须确认 `1.jpg` 存在；不要用其他图片替代缺失的 `1.jpg`。
- 标题表如果被 WPS/Excel 锁定，先关闭占用程序，再运行 `check-lock`。
- 迁移包包含处理逻辑和规则，不包含本机的图包、标题表、账号信息或历史数据。
