#!/usr/bin/env python3
"""把一张图片转成程序图标（多尺寸 .ico + 256px .png）。

    python tools/make_icon.py assets/logo-source.jpg
    python tools/make_icon.py 校徽.png --bg keep        # 保留原背景
    python tools/make_icon.py 校徽.png --bg transparent # 强制抠掉背景

只在**改图标时**需要，运行程序本身不需要 Pillow。

关于透明背景
------------
不能简单地把「所有白色像素」变透明 —— 那样会把白色的眼睛、校徽内圈
一起打穿。这里用的是**从四边泛洪填充**：只有和图像边缘连通的浅色区域
才算背景，物体内部的白色原样保留。抠完还会：
  * 按物体实际外接框裁掉多余留白，再补成正方形（带一点边距）
  * 对 alpha 做轻微羽化，去掉 JPEG 压缩留下的锯齿和白色描边
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow： pip install Pillow")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)
CORNER_RATIO = 0.16      # --bg keep 时才用圆角
MARGIN_RATIO = 0.04      # 抠图后四周留一点空
FLOOD_THRESHOLD = 32     # 泛洪容差（0-255），太小会留白边，太大咬到物体


# --------------------------------------------------------------------------
# 背景处理
# --------------------------------------------------------------------------
def _is_light(pixel: tuple[int, int, int], lo: int = 235) -> bool:
    return min(pixel[:3]) >= lo


def _saturation_map(img: Image.Image) -> Image.Image:
    """返回 L 模式图：像素值 = 饱和度(0-250)。中性色（白/灰/黑）接近 0。

    最大值刻意压到 250，好把 255 空出来当泛洪的哨兵值。
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    out = Image.new("L", (w, h), 0)
    src = rgb.load()
    dst = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            mx = max(r, g, b)
            if mx == 0:
                dst[x, y] = 0
            else:
                dst[x, y] = min(250, (mx - min(r, g, b)) * 250 // mx)
    return out


def remove_background(img: Image.Image, sat_threshold: int = 34,
                      seed_sat: int = 28) -> Image.Image:
    """抠掉背景 —— 判据是「中性色」而不是「白色」。

    白底好办，难的是物体底下那层**灰色投影**：它是灰的，用「接近白色」
    去泛洪会停在投影边缘，留下一圈灰边。改成按**饱和度**判断就干净了：
    背景和投影都是中性色（饱和度≈0），而吉祥物是饱和的蓝色，
    泛洪到身体边界自然停住。

    泛洪只从图像边缘出发，所以被物体包住的白色（眼睛、校徽内圈）
    不受影响。
    """
    img = img.convert("RGBA")
    w, h = img.size

    sat = _saturation_map(img)
    work = sat.copy()
    sentinel = 255

    border = ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] +
              [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])
    seeds = [p for p in border if work.getpixel(p) <= seed_sat]
    if not seeds:
        return img  # 四边都不是中性色，说明本来就没有纯色背景

    for seed in seeds:
        if work.getpixel(seed) == sentinel:
            continue
        ImageDraw.floodfill(work, seed, sentinel, thresh=sat_threshold)

    mask = work.point(lambda v: 0 if v == sentinel else 255)
    # 轻微羽化：JPEG 边缘本来就有半像素的过渡，硬切会有锯齿
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))

    out = img.copy()
    out.putalpha(mask)
    return out


def crop_to_content(img: Image.Image, margin_ratio: float = MARGIN_RATIO) -> Image.Image:
    """按不透明区域裁掉多余留白，再补成正方形。"""
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    pad = int(side * margin_ratio)
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(img, (pad + (side - w) // 2, pad + (side - h) // 2), img)
    return canvas


def square_with_padding(img: Image.Image, bg=(255, 255, 255, 255)) -> Image.Image:
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), bg)
    canvas.paste(img, ((side - w) // 2, (side - h) // 2), img if img.mode == "RGBA" else None)
    return canvas


def rounded(img: Image.Image, ratio: float = CORNER_RATIO) -> Image.Image:
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(1, int(size * ratio)), fill=255
    )
    out = img.copy()
    out.putalpha(mask)
    return out


# --------------------------------------------------------------------------
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    src = Path(args[0]) if args else ASSETS / "logo-source.jpg"
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        sys.exit(f"找不到源图: {src}")

    bg = "auto"
    for flag in flags:
        if flag.startswith("--bg="):
            bg = flag.split("=", 1)[1]
    if "--bg" in flags:
        idx = sys.argv.index("--bg")
        if idx + 1 < len(sys.argv):
            bg = sys.argv[idx + 1]
    if bg not in ("auto", "keep", "transparent"):
        sys.exit(f"--bg 只能是 auto / keep / transparent，收到 {bg!r}")

    ASSETS.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGBA")
    print(f"源图      : {src}  {img.size[0]}x{img.size[1]}")

    if bg == "transparent":
        do_cut = True
    elif bg == "keep":
        do_cut = False
    else:  # auto：四角都是浅色就认为白底，抠掉
        do_cut = all(_is_light(img.getpixel(p)) for p in
                     [(0, 0), (img.width - 1, 0), (0, img.height - 1),
                      (img.width - 1, img.height - 1)])
    print(f"背景处理  : {'抠成透明' if do_cut else '保留原背景（圆角）'}")

    if do_cut:
        base = crop_to_content(remove_background(img))
    else:
        base = rounded(square_with_padding(img))

    base = base.resize((256, 256), Image.LANCZOS)

    png_path = ASSETS / "icon.png"
    base.save(png_path, format="PNG")
    print(f"256px PNG : {png_path}")

    ico_path = ASSETS / "icon.ico"
    base.save(ico_path, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"多尺寸 ICO: {ico_path}  {[f'{s}x{s}' for s in SIZES]}")
    print(f"文件大小  : {ico_path.stat().st_size:,} 字节")

    # 预览：同时贴到浅色和深色底上，方便检查有没有白边
    preview = Image.new("RGBA", (256 * 2 + 30, 256 + 20), (0, 0, 0, 0))
    preview.paste(Image.new("RGBA", (256, 256), (255, 255, 255, 255)), (10, 10))
    preview.paste(Image.new("RGBA", (256, 256), (32, 32, 36, 255)), (276, 10))
    preview.alpha_composite(base, (10, 10))
    preview.alpha_composite(base, (276, 10))
    preview_path = ASSETS / "_preview.png"
    preview.save(preview_path)
    print(f"预览      : {preview_path}   （左=浅底 右=深底，检查有没有白边）")

    _hint_refresh_cache()
    return 0


def _hint_refresh_cache() -> None:
    """换了图标以后，Windows 往往还在用旧缓存，桌面看起来「没变」。"""
    if sys.platform != "win32":
        return
    print()
    print("=" * 62)
    print("⚠  图标换了，但 Windows 有图标缓存，桌面可能还显示旧图。")
    print("   刷新办法（任选其一）：")
    print("     1) 双击桌面空白处按 F5；")
    print("     2) 任务管理器里重启「Windows 资源管理器」；")
    print("     3) 运行下面这行（删缓存数据库，Windows 会自动重建）：")
    print()
    print(r'   del /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db"')
    print(r'   del /f /q "%LOCALAPPDATA%\IconCache.db"')
    print(r'   ie4uinit.exe -show')
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
