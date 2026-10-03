"""生成应用图标 ``assets/oj_icon.ico``。

Windows 图标不是"一张 PNG 改后缀"：不同位置需要不同尺寸 ——
任务栏用 32/48、资源管理器小图标用 16、Alt+Tab 用 256。只塞一张 256 的图，
在 16×16 下会因为缩放变成一团模糊的糊点。所以这里逐尺寸绘制（小尺寸简化细节）。

用法::

    python packaging/make_icon.py [输出路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    print("需要 Pillow：pip install pillow", file=sys.stderr)
    raise SystemExit(1)

#: Windows 图标需要的尺寸集合
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

BG_TOP = (26, 127, 212)
BG_BOTTOM = (12, 88, 150)
ACCENT = (74, 222, 128)
TEXT = (255, 255, 255)


def _font(size: int):
    """找一个能渲染字母的字体，找不到就用 Pillow 内置位图字体。"""
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _gradient(size: int) -> Image.Image:
    """竖向渐变底色。"""
    image = Image.new("RGBA", (size, size))
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(
            int(BG_TOP[index] + (BG_BOTTOM[index] - BG_TOP[index]) * ratio)
            for index in range(3)
        )
        for x in range(size):
            image.putpixel((x, y), (*color, 255))
    return image


def render(size: int) -> Image.Image:
    """绘制指定尺寸的图标。"""
    # 先以 4 倍分辨率绘制再缩小，得到平滑边缘（等效于超采样抗锯齿）
    scale = 4
    canvas_size = size * scale
    image = _gradient(canvas_size)

    # 圆角遮罩
    mask = Image.new("L", (canvas_size, canvas_size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, canvas_size - 1, canvas_size - 1),
        radius=int(canvas_size * 0.22),
        fill=255,
    )
    image.putalpha(mask)

    draw = ImageDraw.Draw(image)

    if size <= 20:
        # 极小尺寸下文字不可辨，只画一个对勾，保证远看是个清晰的记号
        _draw_check(draw, canvas_size, scale, thickness_scale=0.16)
    else:
        _draw_check(draw, canvas_size, scale, thickness_scale=0.13,
                    offset=(0.0, -0.10), size_scale=0.44)
        font = _font(int(canvas_size * 0.42))
        text = "OJ"
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        position = ((canvas_size - text_width) / 2 - box[0],
                    canvas_size * 0.62 - text_height / 2 - box[1])
        draw.text(position, text, font=font, fill=TEXT)

    return image.resize((size, size), Image.LANCZOS)


def _draw_check(draw: ImageDraw.ImageDraw, canvas: int, scale: int,
                *, thickness_scale: float, offset: tuple[float, float] = (0.0, 0.0),
                size_scale: float = 0.5) -> None:
    """画一个圆角对勾，代表"通过"。"""
    center_x = canvas * (0.5 + offset[0])
    center_y = canvas * (0.34 + offset[1])
    span = canvas * size_scale
    thickness = max(2, int(canvas * thickness_scale))

    points = [
        (center_x - span * 0.5, center_y),
        (center_x - span * 0.1, center_y + span * 0.34),
        (center_x + span * 0.55, center_y - span * 0.38),
    ]
    draw.line(points[:2], fill=ACCENT, width=thickness, joint="curve")
    draw.line(points[1:], fill=ACCENT, width=thickness, joint="curve")
    radius = thickness // 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=ACCENT)


def _dib_bytes(image: "Image.Image") -> bytes:
    """把一帧图像编码成 ICO 内嵌的 DIB（BITMAPINFOHEADER + BGRA + AND 掩码）。

    32 位 BGRA 且带 alpha 时，AND 掩码在 Windows 10/11 上会被忽略，
    但仍必须存在（结构要求），所以填全 0 即可。
    """
    import struct

    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()

    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        width,
        height * 2,    # 高度含 XOR + AND 两张位图
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression = BI_RGB
        width * height * 4,
        0, 0, 0, 0,
    )

    # XOR 位图：自下而上
    body = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            body += bytes((blue, green, red, alpha))

    # AND 掩码：1bpp，每行按 4 字节对齐
    row_bytes = ((width + 31) // 32) * 4
    mask = b"\x00" * (row_bytes * height)

    return header + bytes(body) + mask


def write_ico(target: Path, frames: dict[int, "Image.Image"]) -> None:
    """按尺寸逐帧写入多尺寸 ICO。

    不用 ``PIL.Image.save(..., sizes=...)`` 的原因：它只能从**同一张**源图缩放生成
    各档尺寸，于是 16×16 会变成 256×256 的缩略糊图。这里 16 与 256 是分别绘制的，
    必须逐帧写入才能保住各自的细节。
    """
    import struct

    sizes = sorted(frames)
    entries = []
    offset = 6 + 16 * len(sizes)

    for size in sizes:
        image = frames[size].convert("RGBA")
        # 256 及以上使用 PNG 压缩（体积从 256KB 降到几 KB），小尺寸用 DIB 兼容性最好
        if size >= 128:
            import io

            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            payload = buffer.getvalue()
        else:
            payload = _dib_bytes(image)

        entries.append((size, payload, offset))
        offset += len(payload)

    with open(target, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(sizes)))  # ICONDIR
        for size, payload, offset in entries:
            dimension = 0 if size >= 256 else size
            handle.write(struct.pack(
                "<BBBBHHII",
                dimension, dimension,   # 宽高（256 记作 0）
                0, 0,                  # 调色板数量、保留位
                1, 32,                 # 色彩平面、位深
                len(payload), offset,
            ))
        for _size, payload, _offset in entries:
            handle.write(payload)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "assets" / "oj_icon.ico"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    frames = {size: render(size) for size in SIZES}
    write_ico(target, frames)

    # 顺手导出一张 PNG，方便放到 README 或安装包里
    png_target = target.with_suffix(".png")
    frames[max(SIZES)].save(png_target, format="PNG")

    print(f"已生成图标：{target}（{len(SIZES)} 个尺寸，{target.stat().st_size / 1024:.1f} KB）")
    print(f"预览图：{png_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
