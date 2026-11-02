"""图像工具：读取尺寸、视觉模型输入压缩、缩略图与选款拼图。"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont, ImageOps

FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
FORMAT_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}


def open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def probe(data: bytes) -> tuple[str, int, int]:
    img = open_image(data)
    return (img.format or "PNG"), img.width, img.height


def to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        canvas = Image.new("RGB", img.size, (255, 255, 255))
        canvas.paste(img, mask=img.split()[-1])
        return canvas
    return img.convert("RGB")


def to_vision_jpeg(data: bytes, max_side: int) -> bytes:
    """发给视觉模型的副本：限制最长边、转 JPEG。原图字节与哈希不受影响。"""
    img = to_rgb(open_image(data))
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def thumbnail_jpeg(data: bytes, max_side: int = 512) -> bytes:
    return to_vision_jpeg(data, max_side)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def label_font(path: str | None, size: int = 18) -> ImageFont.FreeTypeFont | None:
    """拼图说明用的中文字体；没配或读不到时返回 None，调用方只写 ASCII。"""
    if not path:
        return None
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return None


def _fit(font: ImageFont.ImageFont, text: str, width: float) -> str:
    """按像素宽度截断，放不下时末尾写 ...。"""
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "...") > width:
        text = text[:-1]
    return text + "..."


def contact_sheet(
    items: list[tuple[bytes, list[str]]],
    *,
    cols: int = 3,
    cell: int = 420,
    font: ImageFont.ImageFont | None = None,
) -> bytes:
    """选款拼图：每格一张缩略图 + 下方两行说明。

    默认字体只有 ASCII（容器里没有中文字体）；说明里有中文时传 label_font 读到的字体。
    """
    if not items:
        raise ValueError("没有可拼的图")
    cols = max(1, min(cols, len(items)))
    rows = (len(items) + cols - 1) // cols
    label_h = 58
    pad = 16
    sheet = Image.new(
        "RGB",
        (cols * (cell + pad) + pad, rows * (cell + label_h + pad) + pad),
        (245, 245, 245),
    )
    draw = ImageDraw.Draw(sheet)
    font = font or _font(18)
    for idx, (data, lines) in enumerate(items):
        r, c = divmod(idx, cols)
        x = pad + c * (cell + pad)
        y = pad + r * (cell + label_h + pad)
        img = to_rgb(open_image(data))
        img.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        sheet.paste(img, (x + (cell - img.width) // 2, y + (cell - img.height) // 2))
        for li, text in enumerate(lines[:2]):
            draw.text(
                (x + 4, y + cell + 6 + li * 24),
                _fit(font, text, cell - 8),
                fill=(30, 30, 30),
                font=font,
            )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
