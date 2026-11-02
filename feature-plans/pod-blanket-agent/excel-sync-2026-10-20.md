# 同步回 Excel：参考图 2D 化（S01P 图案提取）、套图、任务命名（2026-10-20）

[Last Updated: 2026-10-20]

## 为什么改

- 用户 2026-10-20 要求：把 Pattern DNA 上传参考图后先做的「2D 化」（把参考图里的印花提取成干净的平面图案）完整移植过来，放在上传之后马上做，图案提取出来后再做方向分析；之后出图按这张干净的图案裂变。
- 选择：提取完自动接着分析，不加人工确认；已有的 S01 只补提取一张，给以后的出图和质检用，已有的拆解和系列规则不重做。
- 提取提示词照抄 Pattern DNA 的 `flatten_textile`（毯子 / 纺织品类目用的那一份），不先调视觉模型。

## 流程与运行设置要改的

| 位置 | 原文 | 改为 |
| --- | --- | --- |
| 流程图 | 上传参考图 → S01 接入 → S02 拆解 | 上传参考图 → S01 接入（P01 看原图：可用性、授权）→ **S01P 图案提取** → S02 拆解 |
| 阶段表（新增一行） | — | **S01P 图案提取**：输入原图（压到最长边 1536 的 JPEG）+ 文末的提取提示词；图生图模型用 `gpt-image-2.5-flare`，失败再换 `gpt-image-2.5-sunburst`（同日按用户要求改，不用 Pattern DNA 的 `gpt-image-2`）；参数同出图的 gpt 设置（4:5 出 1856x2304，quality high）；输出一张平面图案。全部失败停在这一步，再跑接着做。不满意可以「重新提取图案（要求……）」，要求追加在提示词末尾，只换图案、不重做拆解 |
| S02 拆解的输入图 | 参考图原图 | 两张：第一张是提取的平面图案（设计、构图、配色都从这张读），第二张是原图（只看商品形态、尺寸与面料工艺）。没有图案时照旧只发原图 |
| T01 图生图的输入图 | 参考图原图（压到最长边 2048 的 JPEG） | 有图案时用图案（同样压到最长边 2048），没有时用原图 |
| S07 小变化质检对照的参考图 | 参考图原图 | 有图案时用图案，没有时用原图 |
| 给 P04 的授权说明（`rights_scope.reference_image_use`） | 已授权：文生图只用文字指令；图生图把参考图原图作为输入图（授权未确认时后面再加「不得出现可识别的既有角色、品牌标识或商标」） | 有图案时「参考图原图」改成「从参考图提取的平面图案」，其余不变 |
| 运行设置（新增） | — | `pattern_extract.enabled`（默认开；关掉就回到全程用原图）、`pattern_extract.models`（上面三个模型的顺序）、`pattern_extract.input_max_side`（1536） |

## P02 要改的

只改「输入」一节，其余不变：

| 原文 | 改为 |
| --- | --- |
| {{seed_record}}、{{source_image}}、{{field_dictionary}}。必须提供实际图像，不只提供前一步文字描述。 | {{seed_record}}、{{source_image}}、{{field_dictionary}}。必须提供实际图像，不只提供前一步文字描述。source_image 有两张时，第一张是从参考图提取出的平面图案（已去掉房间、褶皱、透视和光影），设计字段都从它读；第二张是原图，只用来判断商品形态、尺寸、面料与工艺。只有一张时就是原图，只分析 seed_record 标出的有效印花区域。 |

## 没改的

- P06、P07 正文里「参考图原图」的字样没改：有图案时，程序发给模型的输入图 / 对照图是图案。Excel 里如要表述准确，可以写成「参考图（有提取的图案时用图案）」。

## 提取提示词全文（S01P，新增）

用户要求重新提取时，在末尾另起一行追加：`Additional requirement from the user (may be written in Chinese): <用户原话>`。

```text
You are a textile surface-design reconstruction specialist. Transform the specified textile product and the designated foreground objects placed on it into one clean, flat textile design artwork. Requirements: - Treat the designated foreground objects as intentional design motifs, not as removable scene objects. - Preserve their count, identity, colors, key features, relative scale, spacing, orientation, overlap, and overall composition. - Preserve any lettering / text that is part of the printed design (words, names, monograms, numbers) as a core design motif — keep its exact wording legible; never erase, blur, redraw into different words, translate or simplify it (especially personalization placeholders such as "NAME" / "YOUR NAME"). Still drop text that is NOT part of the design: photo watermarks, price / URL / brand tags overlaid on the shot. - Flatten all motifs into a front-facing 2D surface-design view. - Remove the room, furniture, product shape, folds, perspective, shadows, lighting effects, and photographic depth. - Reconstruct partially obscured areas conservatively without adding unsupported motifs. - Preserve the textile’s base color and relevant material character, but do not retain large physical folds or uneven lighting. - Do not force a seamless repeat unless explicitly requested. Output one edge-to-edge, high-resolution flat textile design artwork, with no mockup, border, labels, or surrounding scene.
```

## 同日追加：套图（mooya 图案管线），选款完成不再打压缩包

### 为什么改

- 用户 2026-10-20：选款完成后一般不直接下载，把下载这一步删掉；接着出场景图，复用 mooya 的 Pattern Showcase 毛毯，直接调 mooya 的代码——那边的代码和提示词会不断更新，这边不用管就能生效。
- 选择：mooya 加机器调用接口，内部建图案管线（毛毯、跳过 DNA）任务；选款完成后自动出。

### 流程与运行设置要改的

| 位置 | 原文 | 改为 |
| --- | --- | --- |
| 流程图 | … S10 归档 → 人工选款 → 已选款 | … S10 归档 → 人工选款 → 已选款 → **套图**（选中的款交给 mooya 图案管线出白底图 + 场景图） |
| 阶段表（新增一行） | — | **套图**：输入是选中的设计原图；mooya 图案管线按它自己的步骤出（文字审核 → 高清放大 → 白底图 → 白底图门控 → 场景分析 → 场景策划 → 场景出图），现在一套是 1 张白底图 + 约 16 张场景图。提示词和步骤都在 mooya，本方案不收录、不改写。产出拉回存档，每款一个压缩包（设计图 + 白底图 + 场景图），写进选款表。触发：选款完成后自动；之后逐张补选的款手动「出套图」 |
| 人工审核 / 选款 | （如果 Excel 里已写上同日的「候选表验收后打包选中原图、给下载链接」） | 不再打包，选中的款接着出套图；Excel 里没写这句就不用改 |
| 运行设置（新增） | — | `mooya.base_url`、`mooya.preset`（pod_blanket）、`mooya.timeout_seconds`（60）；`scenes.enabled`、`scenes.auto_after_accept`（选款完成后自动出）、`scenes.max_in_flight`（同时 2 款）、`scenes.poll_seconds`（30）、`scenes.sync_max_minutes`（40）、`scenes.max_attempts`（6） |

### 没改的

- G00、P00–P11 都没改：套图不在这边调模型。

## 同日追加：任务命名（S01N，新增提示词 P12）

### 为什么改

- 用户 2026-10-20 晚：图片上传、创建任务之后，在对图片进行分析的同时，给任务做一个短的名字：一个简短的对参考图的描述，加上当天的日期。
- 选择：格式「描述 + 空格 + 月日」，如「森林三伙伴水彩 1020」；显示在任务列表、任务明细、人设汇报、这张图相关的卡片标题和候选表表名，编号（S01）照留。

### 流程与运行设置要改的

| 位置 | 原文 | 改为 |
| --- | --- | --- |
| 流程图 | 上传参考图 → S01 接入 → S01P 图案提取 → … | 上传参考图 → S01 接入 → S01P 图案提取 → …；另有一条与 S01 **同时**跑的支线：**S01N 任务命名** |
| 阶段表（新增一行） | — | **S01N 任务命名**（P12）：输入参考图原图 + 登记信息；模型只写一个 4–8 个汉字的描述（最多 10 个字），程序去掉空格和标点、接上这张图登记那天的月日作为任务名字。和 S01 同时跑，失败不影响拆解（下次拆解再试）；只起一次，重新拆解、重新提取图案都不改名；人可以改描述（日期不变）或让模型重新起名 |
| 提示词清单（新增） | G00、P00–P11 | 加 **P12 任务命名**（全文见下） |
| 运行设置（新增） | — | `task_name.enabled`（默认开；关掉后任务只显示编号） |

### P12 全文（新增）

```text
# P12 任务命名

## 输入

{{source_images}}、{{source_metadata}}。

## 角色与任务

你给一张参考图对应的设计任务起名字。名字用来让人在任务列表、飞书卡片和候选表里一眼认出是哪张图：只写一个简短的画面描述，日期由程序接在后面。

## 执行规则

只看印花画面本身，概括主体和风格（或题材），如「森林三伙伴水彩」「复古玫瑰格纹」「海洋鲸鱼卡通」；4–8 个汉字，最多 10 个字。不写日期、编号、标点、空格和英文；不写尺寸、商品类型（毛毯、盖毯、毯子）、房间和摆拍道具；不写品牌、商标或可识别的既有角色名，画面里有就换成通用说法（如「卡通小熊」）；画面里的文字不照抄。看不清时只写看得出的题材和主色，不编造。

## 输出约定

result={"description":"..."}。只给一个描述，不给备选。

## 结束与回流

不需要补图，看不清也给出看得出的部分。描述不合规时程序会退回重答。
```

### 没改的

- G00、P00–P11 都没改：起名是单独一次调用，P01 接入不管命名。
