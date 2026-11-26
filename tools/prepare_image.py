#!/usr/bin/env python3
"""画像をホームページ用に整えて保存する。

いつも行うこと:
- スマートフォン写真の向きを正しく直す
- 大きすぎる写真を縮小する（長い辺を最大1600px。--max で変更可）
- 撮影場所（位置情報）などの情報を取り除く
- 保存先の拡張子（.jpg / .png / .webp / .gif）で保存形式を決める

頼まれたときに行うこと（座標は、元の写真の向きを直したあとのピクセル。
python tools/photo_sheet.py grid 元の画像 で、目盛り付きの画像を作って読む）:
- ぼかし   --blur X1,Y1,X2,Y2       四角い範囲を、読めない・分からない程度に強くぼかす（書き込み、名札、画面など）
           --blur-face X1,Y1,X2,Y2  だ円でぼかす（顔）。どちらも何回でも指定できる
           --keep X1,Y1,X2,Y2       ぼかさない範囲（主役の顔など）。ぼかしの範囲やその境目がかかっても、ここはぼかさない
           ぼかしの境目は、まわりへ少しずつなじませる（くっきりした線にしない）。ぼかしたところには、
           まわりと同じくらいの細かいざらつきを足して、そこだけつるっと浮かないようにする
- 切り抜き --crop 3:4               縦横の比率に合わせて切り抜く（写真を入れる枠の比率）
           --focus X,Y              切り抜くときの中心（人物の顔など）。省略すると写真の中央
           --zoom 0.8               比率に合う最大の範囲の何割を使うか（人物を大きく写すとき。0.3〜1）
           --crop-box X1,Y1,X2,Y2   範囲を直接指定して切り抜く（--crop とは一緒に使わない）
- トーン   --tone auto              サイトのほかの写真の明るさ・コントラスト・色の濃さ・色味に近づける（差の半分）
           --tone auto:0.8          近づける強さ（0〜1）
           --brightness 1.1 --contrast 0.95 --saturation 1.1 --warmth 6   手で調整する（倍率。warmth は赤みを足す量、マイナスで青み）

処理の順番は、向きの補正 → ぼかし → 切り抜き → トーン → 縮小。ぼかしは縮小の前に、元の大きさで行う。
ぼかす前の写真は保存しない（保存されるのは、加工が終わった1枚だけ）。

使い方:
  python tools/prepare_image.py 元の画像 保存先 [オプション]
  例: python tools/prepare_image.py /tmp/issue-images/image-1.jpg site/assets/img/news/news_20261201_office.jpg
  例: python tools/prepare_image.py /tmp/issue-images/image-3.jpg site/assets/img/photo/person-goto.jpg \\
        --blur-face 1515,250,1785,630 --blur 85,820,255,1005 --keep 700,180,980,560 --crop 3:4 --focus 815,370 --tone auto
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parent.parent
PHOTO_DIRS = [ROOT / "site" / "assets" / "img" / "photo", ROOT / "site" / "assets" / "img" / "news"]
NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*\.(?:jpg|jpeg|png|webp|gif)$")


def human(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB" if size >= 1024 * 1024 else f"{size / 1024:.0f}KB"


class Problem(Exception):
    pass


def numbers(text: str, count: int, label: str) -> list[int]:
    try:
        values = [int(round(float(v))) for v in text.split(",")]
    except ValueError:
        values = []
    if len(values) != count:
        raise Problem(f"{label} は {'X1,Y1,X2,Y2' if count == 4 else 'X,Y'} の形で書いてください（例: {'100,200,300,400' if count == 4 else '800,400'}）: {text}")
    return values


def box_in(img: Image.Image, text: str, label: str) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = numbers(text, 4, label)
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    w, h = img.size
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise Problem(f"{label} {text} は写真の外か、小さすぎます（写真の大きさ {w}x{h}）")
    return x1, y1, x2, y2


def feather_width(box: tuple[int, int, int, int]) -> int:
    """ぼかしの境目を、なじませる幅（範囲の大きさに合わせる）"""
    w, h = box[2] - box[0], box[3] - box[1]
    return int(max(10, min(90, min(w, h) * 0.35)))


def grain(region: Image.Image) -> Image.Image | None:
    """まわりと同じくらいの細かいざらつき（写真のノイズ）。ぼかしたところだけ、つるっと浮かないようにする"""
    gray = region.convert("L")
    detail = ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(1.2)))
    sigma = min(10.0, ImageStat.Stat(detail).mean[0] * 1.25)
    if sigma < 0.8:
        return None
    noise = Image.effect_noise(region.size, sigma)
    return Image.merge("RGB", (noise, noise, noise)) if region.mode == "RGB" else None


def blur(img: Image.Image, box: tuple[int, int, int, int], ellipse: bool,
         keep: list[tuple[int, int, int, int]] | None = None) -> Image.Image:
    """範囲を、元が分からない程度に強くぼかす。
    - 範囲の中は、確実に全部ぼかす
    - 範囲の外へ、少しずつなじませる（くっきりした境目の線を作らない）
    - ぼかしたところに、まわりと同じくらいのざらつきを足す
    - keep（主役の顔など）には、ぼかしも、なじませる部分もかけない"""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    f = feather_width(box)
    m = f + 4  # 範囲のまわりの、なじませる幅
    ex1, ey1 = max(0, x1 - m), max(0, y1 - m)
    ex2, ey2 = min(img.width, x2 + m), min(img.height, y2 + m)
    region = img.crop((ex1, ey1, ex2, ey2))
    rw, rh = region.size

    # 強くぼかす（小さくしてから戻し、さらにぼかす。モザイクのような四角いむらは作らない）
    # 顔は、誰か分からないように範囲に合わせて強く。文字は、読めなくなる程度にとどめ、
    # 紙や板の形は残す（強すぎると、そこだけ白いもやのように浮いて見えるため）
    s = max(4, min(w, h) // 6) if ellipse else max(4, min(12, min(w, h) // 12))
    soft = region.resize((max(1, rw // s), max(1, rh // s)), Image.BILINEAR).resize((rw, rh), Image.BICUBIC)
    soft = soft.filter(ImageFilter.GaussianBlur(s * 0.6))
    noise = grain(region)
    if noise is not None:
        soft = ImageChops.add(soft, noise, scale=1.0, offset=-128)

    # どこをどれだけぼかすか（白いほど強く）
    inner = [x1 - ex1, y1 - ey1, x2 - ex1 - 1, y2 - ey1 - 1]
    grow = f // 2
    outer = [inner[0] - grow, inner[1] - grow, inner[2] + grow, inner[3] + grow]
    mask = Image.new("L", (rw, rh), 0)
    core = Image.new("L", (rw, rh), 0)
    if ellipse:
        ImageDraw.Draw(mask).ellipse(outer, fill=255)
        ImageDraw.Draw(core).ellipse(inner, fill=255)
    else:
        ImageDraw.Draw(mask).rounded_rectangle(outer, radius=grow + min(w, h) // 6, fill=255)
        ImageDraw.Draw(core).rectangle(inner, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(f / 3))
    mask = ImageChops.lighter(mask, core)  # 範囲の中は、確実に全部

    # ぼかさない範囲（主役の顔など）は、ぼかしを外す（境目は少しだけなじませる）
    for kx1, ky1, kx2, ky2 in keep or []:
        if kx2 <= ex1 or kx1 >= ex2 or ky2 <= ey1 or ky1 >= ey2:
            continue
        hole = Image.new("L", (rw, rh), 0)
        ImageDraw.Draw(hole).ellipse([kx1 - ex1, ky1 - ey1, kx2 - ex1, ky2 - ey1], fill=255)
        hole = hole.filter(ImageFilter.GaussianBlur(max(2, f / 8)))
        mask = ImageChops.subtract(mask, hole)

    out = img.copy()
    out.paste(Image.composite(soft, region, mask), (ex1, ey1))
    return out


def crop_ratio(img: Image.Image, ratio: str, focus: str | None, zoom: float) -> tuple[Image.Image, tuple[int, int, int, int]]:
    try:
        rw, rh = (float(v) for v in ratio.replace("/", ":").split(":"))
        assert rw > 0 and rh > 0
    except (ValueError, AssertionError):
        raise Problem(f"--crop は 3:4 のように「横:縦」で書いてください: {ratio}")
    if not 0.3 <= zoom <= 1:
        raise Problem("--zoom は 0.3〜1 の数にしてください")
    w, h = img.size
    cw, ch = (w, w * rh / rw) if w * rh / rw <= h else (h * rw / rh, h)
    cw, ch = int(cw * zoom), int(ch * zoom)
    fx, fy = numbers(focus, 2, "--focus") if focus else (w // 2, h // 2)
    if not (0 <= fx <= w and 0 <= fy <= h):
        raise Problem(f"--focus {focus} が写真の外です（写真の大きさ {w}x{h}）")
    x1 = min(max(0, fx - cw // 2), w - cw)
    y1 = min(max(0, fy - ch // 2), h - ch)
    box = (int(x1), int(y1), int(x1 + cw), int(y1 + ch))
    return img.crop(box), box


def stats(img: Image.Image) -> tuple[float, float, float, float]:
    """明るさ・コントラスト・色の濃さ・色味（赤み−青み）"""
    small = img.convert("RGB")
    small.thumbnail((400, 400))
    gray = ImageStat.Stat(small.convert("L"))
    sat = ImageStat.Stat(small.convert("HSV").split()[1]).mean[0]
    r, _, b = ImageStat.Stat(small).mean
    return gray.mean[0], gray.stddev[0], sat, r - b


def site_average(exclude: Path) -> tuple[float, float, float, float] | None:
    values = []
    for folder in PHOTO_DIRS:
        for p in sorted(folder.glob("*")):
            if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp") or p.resolve() == exclude.resolve():
                continue
            try:
                with Image.open(p) as im:
                    values.append(stats(ImageOps.exif_transpose(im)))
            except OSError:
                continue
    if not values:
        return None
    return tuple(sum(v[i] for v in values) / len(values) for i in range(4))  # type: ignore[return-value]


def adjust(img: Image.Image, brightness: float, contrast: float, saturation: float, warmth: float) -> Image.Image:
    if brightness != 1:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if saturation != 1:
        img = ImageEnhance.Color(img).enhance(saturation)
    if warmth:
        r, g, b = img.split()
        half = warmth / 2
        r = r.point(lambda v: max(0, min(255, round(v + half))))
        b = b.point(lambda v: max(0, min(255, round(v - half))))
        img = Image.merge("RGB", (r, g, b))
    return img


def fmt(s) -> str:
    return f"明るさ {s[0]:.0f}／コントラスト {s[1]:.0f}／色の濃さ {s[2]:.0f}／色味（赤み） {s[3]:+.0f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="画像をホームページ用に整えて保存します（くわしくは、このファイルの先頭の説明）")
    parser.add_argument("source", help="元の画像")
    parser.add_argument("dest", help="保存先（site/assets/img/ から始まるパス）")
    parser.add_argument("--max", type=int, default=1600, help="長い辺の最大ピクセル数（初期値 1600）")
    parser.add_argument("--blur", action="append", default=[], metavar="X1,Y1,X2,Y2", help="四角くぼかす範囲（何回でも）")
    parser.add_argument("--blur-face", action="append", default=[], metavar="X1,Y1,X2,Y2", help="だ円でぼかす範囲（顔。何回でも）")
    parser.add_argument("--keep", action="append", default=[], metavar="X1,Y1,X2,Y2", help="ぼかさない範囲（主役の顔など。何回でも）")
    parser.add_argument("--crop", metavar="横:縦", help="縦横の比率に合わせて切り抜く（例: 3:4、16:10）")
    parser.add_argument("--focus", metavar="X,Y", help="切り抜くときの中心")
    parser.add_argument("--zoom", type=float, default=1.0, help="比率に合う最大の範囲の何割を使うか（0.3〜1）")
    parser.add_argument("--crop-box", metavar="X1,Y1,X2,Y2", help="範囲を直接指定して切り抜く")
    parser.add_argument("--tone", metavar="auto[:強さ]", help="サイトのほかの写真に近づける（例: auto、auto:0.8）")
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--contrast", type=float, default=1.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--warmth", type=float, default=0.0)
    args = parser.parse_args()

    src = Path(args.source)
    dest = Path(args.dest)
    if not src.is_file():
        print(f"[エラー] 元の画像が見つかりません: {src}")
        return 1
    if dest.is_absolute() or dest.parts[:3] != ("site", "assets", "img"):
        print("[エラー] 保存先は site/assets/img/ から始まるパスにしてください（例: site/assets/img/news/news_20261201_office.jpg）")
        return 1
    if not NAME_RE.match(dest.name):
        print("[エラー] ファイル名は半角の英小文字・数字・ハイフン・アンダーバーにし、拡張子は .jpg .png .webp .gif のどれかにしてください")
        return 1
    if args.crop and args.crop_box:
        print("[エラー] --crop と --crop-box は、どちらか一方だけにしてください")
        return 1
    if args.keep and not (args.blur or args.blur_face):
        print("[エラー] --keep（ぼかさない範囲）は、--blur か --blur-face と一緒に使います")
        return 1
    if args.focus and not args.crop:
        print("[エラー] --focus は --crop と一緒に使います")
        return 1
    manual = (args.brightness, args.contrast, args.saturation, args.warmth) != (1.0, 1.0, 1.0, 0.0)
    if args.tone and manual:
        print("[エラー] --tone と、手で調整する指定（--brightness など）は、どちらか一方だけにしてください")
        return 1
    editing = bool(args.blur or args.blur_face or args.crop or args.crop_box or args.tone or manual)
    out = ROOT / dest
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()
    done: list[str] = []
    blurred: list[tuple[int, int, int, int]] = []  # ぼかした範囲（あとで、保存した写真の中の位置を知らせる）
    kept: list[tuple[int, int, int, int]] = []  # ぼかさない範囲（主役の顔など）
    offset = (0, 0)

    try:
        with Image.open(src) as original:
            source_format = original.format
            if ext == ".gif":
                if source_format != "GIF":
                    print("[エラー] .gif で保存できるのは、元がGIFの画像だけです")
                    return 1
                if editing:
                    print("[エラー] GIF は加工（ぼかし・切り抜き・トーン）できません。.jpg か .png で保存してください")
                    return 1
                shutil.copyfile(src, out)
                print(f"保存しました: {dest}（GIFはそのままコピー、{human(out.stat().st_size)}）")
                return 0

            img = ImageOps.exif_transpose(original)
            before = img.size
            if editing or ext in (".jpg", ".jpeg"):
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1])
                    img = background
                else:
                    img = img.convert("RGB")

            # ぼかし（元の大きさのまま）。ぼかさない範囲（主役の顔など）には、かけない
            keep = kept = [box_in(img, text, "--keep") for text in args.keep]
            for text in args.blur_face:
                box = box_in(img, text, "--blur-face")
                img = blur(img, box, ellipse=True, keep=keep)
                blurred.append(box)
                done.append(f"ぼかし（だ円） {box}")
            for text in args.blur:
                box = box_in(img, text, "--blur")
                img = blur(img, box, ellipse=False, keep=keep)
                blurred.append(box)
                done.append(f"ぼかし（四角） {box}")
            for k in keep:
                if any(k[0] < b[2] and b[0] < k[2] and k[1] < b[3] and b[1] < k[3] for b in blurred):
                    done.append(f"※ ぼかさない範囲 {k} が、ぼかす範囲と重なっています。重なったところはぼかしていません。"
                                "隠したいものが残っていないか、拡大して確かめてください")
                done.append(f"ぼかさない範囲 {k}")

            # 切り抜き
            if args.crop:
                img, box = crop_ratio(img, args.crop, args.focus, args.zoom)
                offset = box[:2]
                done.append(f"切り抜き {args.crop}（元の写真の {box} の範囲、{img.size[0]}x{img.size[1]}）")
            elif args.crop_box:
                box = box_in(img, args.crop_box, "--crop-box")
                img = img.crop(box)
                offset = box[:2]
                done.append(f"切り抜き（範囲を指定） {box}")

            # トーン
            if args.tone:
                mode, _, strength_text = args.tone.partition(":")
                if mode != "auto":
                    raise Problem("--tone は auto（または auto:0.8 のように強さ付き）で指定してください")
                strength = float(strength_text) if strength_text else 0.5
                if not 0 < strength <= 1:
                    raise Problem("--tone の強さは 0〜1 の数にしてください")
                target = site_average(out)
                if target is None:
                    raise Problem("比べるためのサイトの写真が見つかりません")
                cur = stats(img)
                clamp = lambda v: min(1.25, max(0.8, v))  # noqa: E731  やりすぎない
                b = clamp(1 + (target[0] / max(cur[0], 1) - 1) * strength)
                c = clamp(1 + (target[1] / max(cur[1], 1) - 1) * strength)
                s = clamp(1 + (target[2] / max(cur[2], 1) - 1) * strength)
                wm = max(-15.0, min(15.0, (target[3] - cur[3]) * strength))
                img = adjust(img, b, c, s, wm)
                done.append(f"トーン（サイトの写真に近づける、強さ {strength}）\n"
                            f"      サイトの写真の平均: {fmt(target)}\n"
                            f"      この写真（調整前）: {fmt(cur)}\n"
                            f"      この写真（調整後）: {fmt(stats(img))}")
            elif manual:
                cur = stats(img)
                img = adjust(img, args.brightness, args.contrast, args.saturation, args.warmth)
                done.append(f"トーン（手で調整）: {fmt(cur)} → {fmt(stats(img))}")
    except Problem as exc:
        print(f"[エラー] {exc}")
        print("（何も保存していません）")
        return 1

    edited_size = img.size
    if max(img.size) > args.max:
        img.thumbnail((args.max, args.max), Image.LANCZOS)
    scale = img.size[0] / edited_size[0]

    if ext in (".jpg", ".jpeg"):
        img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    elif ext == ".png":
        img.save(out, "PNG", optimize=True)
    elif ext == ".webp":
        img.save(out, "WEBP", quality=82, method=6)

    print(
        f"保存しました: {dest}（{before[0]}x{before[1]} → {img.size[0]}x{img.size[1]}、"
        f"{human(src.stat().st_size)} → {human(out.stat().st_size)}）"
    )
    for line in done:
        print(f"  - {line}")
    def placed(box: tuple[int, int, int, int], grow: int) -> str | None:
        """元の写真での範囲を、保存した写真の中の位置にする（まわりも grow だけ入れる）"""
        x1, y1, x2, y2 = box
        a = [round((x1 - grow - offset[0]) * scale), round((y1 - grow - offset[1]) * scale),
             round((x2 + grow - offset[0]) * scale), round((y2 + grow - offset[1]) * scale)]
        a = [max(0, min(img.size[0], a[0])), max(0, min(img.size[1], a[1])), max(0, min(img.size[0], a[2])), max(0, min(img.size[1], a[3]))]
        return ",".join(map(str, a)) if a[2] - a[0] > 2 and a[3] - a[1] > 2 else None

    if blurred:
        # なじませた境目まで見えるように、まわりも入れる
        inside = [b for b in (placed(box, feather_width(box) + (box[2] - box[0]) // 4) for box in blurred) if b]
        if inside:
            print("  ※ ぼかしが足りているか、境目が目立たないか、拡大して確かめてください（保存した写真の中での位置）:")
            for box in inside:
                print(f"      python tools/photo_sheet.py zoom {dest} {box}")
        else:
            print("  ※ ぼかした範囲は、切り抜きで写真の外に出ました")
        faces = [b for b in (placed(box, (box[2] - box[0]) // 6) for box in kept) if b]
        if faces:
            print("  ※ 主役の顔（ぼかさない範囲）に、ぼかしがかかっていないか、拡大して確かめてください:")
            for box in faces:
                print(f"      python tools/photo_sheet.py zoom {dest} {box}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
