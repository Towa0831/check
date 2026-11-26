#!/usr/bin/env python3
"""写真を見比べる・位置を読む・加工の結果を確かめるための画像を作る（Read で開いて見る）。

  python tools/photo_sheet.py
      貼られた写真（/tmp/issue-images/list.txt）を、番号・元の名前付きで一覧にする
      → /tmp/issue-images/sheets/sheet-1.jpg …（1枚に12枚まで）
      目を閉じている、ぶれている、奥に人が写っている、などを見比べて選ぶときに使う
  python tools/photo_sheet.py sheet 写真 写真 …
      指定した写真を一覧にする（サイトの写真とトーンを見比べるときなど）
  python tools/photo_sheet.py grid 写真
      目盛り（ピクセルの座標）付きの画像 → /tmp/issue-images/grid/名前.jpg
      ぼかす範囲・切り抜く中心の座標を読むときに使う。座標は prepare_image.py にそのまま渡せる
  python tools/photo_sheet.py zoom 写真 X1,Y1,X2,Y2
      範囲を拡大する → /tmp/issue-images/zoom/名前-X1-Y1.jpg
      ぼかす前の確認（顔や文字がどこまであるか）と、ぼかした後の確認（読めない・分からないか）に使う
  python tools/photo_sheet.py compare 加工前 加工後
      2枚を並べる → /tmp/issue-images/compare/名前.jpg

座標は、写真の向きを直したあとの、元の大きさのピクセル（prepare_image.py と同じ）。
作った画像は確認用で、サイトには保存しない。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

OUT = Path(os.environ.get("IMAGE_DIR", "/tmp/issue-images"))
FONT_FILES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
]


def font(size: int):
    for f in FONT_FILES:
        if Path(f).exists():
            return ImageFont.truetype(f, size), True
    return ImageFont.load_default(size=size), False


def label_text(text: str, cjk: bool) -> str:
    return text if cjk or text.isascii() else text.encode("ascii", "replace").decode().replace("?", "*")


def open_photo(path: str | Path) -> Image.Image:
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def read_list() -> list[tuple[str, str]]:
    """list.txt の (ファイル名, 元の名前)"""
    path = OUT / "list.txt"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        cols = line.split("\t")
        if not cols or not cols[0]:
            continue
        original = cols[1] if len(cols) >= 4 else ""
        rows.append((cols[0], original))
    return rows


def sheet(items: list[tuple[Path, str]]) -> list[Path]:
    f, cjk = font(22)
    small, _ = font(18)
    cell_w, cell_h, pad, head = 420, 315, 16, 58
    per, cols = 12, 4
    out_dir = OUT / "sheets"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for page in range(0, len(items), per):
        chunk = items[page:page + per]
        rows = (len(chunk) + cols - 1) // cols
        canvas = Image.new("RGB", (cols * (cell_w + pad) + pad, rows * (cell_h + head + pad) + pad), "#e6e8e7")
        draw = ImageDraw.Draw(canvas)
        for i, (path, name) in enumerate(chunk):
            x = pad + (i % cols) * (cell_w + pad)
            y = pad + (i // cols) * (cell_h + head + pad)
            try:
                im = open_photo(path)
            except OSError:
                draw.text((x, y), f"{page + i + 1}. 開けません: {path.name}", fill="#b00020", font=small)
                continue
            size = im.size
            im.thumbnail((cell_w, cell_h))
            canvas.paste(im, (x + (cell_w - im.width) // 2, y + head + (cell_h - im.height) // 2))
            title = f"{page + i + 1}. {path.name}" + (f"  {name}" if name else "")
            draw.text((x, y + 2), label_text(title, cjk), fill="#1f2321", font=f)
            draw.text((x, y + 32), f"{size[0]}x{size[1]}", fill="#6b716e", font=small)
        dest = out_dir / f"sheet-{page // per + 1}.jpg"
        canvas.save(dest, quality=85)
        made.append(dest)
    return made


def step_for(length: int) -> int:
    for step in (25, 50, 100, 200, 250, 500, 1000):
        if length / step <= 12:
            return step
    return 2000


def grid_image(im: Image.Image, origin=(0, 0), scale_to: int = 1600, enlarge: bool = False) -> Image.Image:
    """目盛り付きの画像。origin は、im の左上が元の写真のどこにあたるか（拡大の確認用）"""
    w, h = im.size
    step = step_for(max(w, h))
    view = im.copy()
    factor = scale_to / max(w, h) if (enlarge or max(w, h) > scale_to) else 1.0
    if factor != 1.0:
        view = view.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.LANCZOS)
    draw = ImageDraw.Draw(view)
    f, _ = font(max(14, view.width // 70))
    ox, oy = origin
    first_x = (step - ox % step) % step
    first_y = (step - oy % step) % step
    for gx in range(first_x, w + 1, step):
        x = round(gx * factor)
        draw.line([(x, 0), (x, view.height)], fill=(255, 0, 90), width=1)
        draw.text((x + 3, 2), str(ox + gx), fill=(255, 0, 90), font=f, stroke_width=2, stroke_fill="white")
    for gy in range(first_y, h + 1, step):
        y = round(gy * factor)
        draw.line([(0, y), (view.width, y)], fill=(255, 0, 90), width=1)
        draw.text((3, y + 2), str(oy + gy), fill=(255, 0, 90), font=f, stroke_width=2, stroke_fill="white")
    return view


def parse_box(text: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (int(round(float(v))) for v in text.split(","))
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return max(0, x1), max(0, y1), min(size[0], x2), min(size[1], y2)


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd in ("list", "sheet"):
        if cmd == "sheet" and len(args) > 1:
            items = [(Path(a), "") for a in args[1:]]
        else:
            rows = read_list()
            if not rows:
                print(f"{OUT / 'list.txt'} に写真がありません。")
                return 1
            items = [(OUT / name, original) for name, original in rows]
        for dest in sheet(items):
            print(f"一覧を作りました: {dest}")
        for i, (path, name) in enumerate(items, 1):
            print(f"  {i}. {path.name}" + (f"（元の名前: {name}）" if name else ""))
        return 0
    if cmd in ("grid", "zoom", "compare") and len(args) < (3 if cmd != "grid" else 2):
        print(__doc__)
        return 1
    if cmd == "grid":
        src = Path(args[1])
        im = open_photo(src)
        dest = OUT / "grid" / f"{src.stem}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        grid_image(im).save(dest, quality=88)
        print(f"目盛り付きの画像を作りました: {dest}（写真の大きさ {im.size[0]}x{im.size[1]}。目盛りの数字は元の写真のピクセル）")
        return 0
    if cmd == "zoom":
        src = Path(args[1])
        im = open_photo(src)
        try:
            box = parse_box(args[2], im.size)
        except ValueError:
            print("範囲は X1,Y1,X2,Y2 の形で書いてください（例: 1500,250,1800,650）")
            return 1
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            print(f"範囲が写真の外か、小さすぎます（写真の大きさ {im.size[0]}x{im.size[1]}）")
            return 1
        part = im.crop(box)
        dest = OUT / "zoom" / f"{src.stem}-{box[0]}-{box[1]}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        scale = 900 / max(part.size)
        grid_image(part, origin=box[:2], scale_to=900, enlarge=True).save(dest, quality=90)
        print(f"拡大しました: {dest}（{box} を {scale:.1f} 倍。目盛りの数字は元の写真のピクセル）")
        return 0
    if cmd == "compare":
        a, b = Path(args[1]), Path(args[2])
        ims = [open_photo(a), open_photo(b)]
        h = 800
        ims = [im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS) for im in ims]
        f, cjk = font(26)
        canvas = Image.new("RGB", (ims[0].width + ims[1].width + 24, h + 48), "#e6e8e7")
        draw = ImageDraw.Draw(canvas)
        x = 0
        for im, title in zip(ims, (f"前: {a.name}", f"後: {b.name}")):
            canvas.paste(im, (x, 48))
            draw.text((x + 6, 8), label_text(title, cjk), fill="#1f2321", font=f)
            x += im.width + 24
        dest = OUT / "compare" / f"{b.stem}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(dest, quality=88)
        print(f"並べました: {dest}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
