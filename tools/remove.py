"""ニュース・求人・職種・社員紹介を消す（取り下げる）。写真をページから外す。

項目を JSON から消し、ページのフォルダと、その項目だけで使っていた画像・資料も一緒に消す。
ほかの場所でも使っている画像は残す。消したものは変更の記録に残るので、
tools/history.py で探して、見たり戻したりできる。

使い方:
  python tools/remove.py news 20251210     ニュースの記事を消す
  python tools/remove.py openings          求人の一覧（番号付き）を見る
  python tools/remove.py openings 3        3番の求人を消す
  python tools/remove.py people 04         社員紹介を消す
  python tools/remove.py jobs food         職種を消す（その職種の社員紹介・求人が残っているときは止まる）
  python tools/remove.py photo contact-hero.jpg      写真をページから外す（外せる写真だけ。下を参照）
  python tools/remove.py photo contact-hero.jpg 2    2か所以上で使っているときは、外す場所の番号（2,3 や all も）

写真を外せるのは、外してもページの形が崩れない次の場所だけ:
  - ページの見出しの下の大きな写真（アライアンス・募集一覧・よくある質問など）。写真の枠ごと外す
  - ニュースの記事の本文の写真
  - 社員紹介のインタビューの途中の写真
それ以外（写真の並び・カード・背景・職種や社員紹介の写真・製品の写真など）は外さずに止まり、
理由を表示する（差し替えるか、管理者に相談する）。外したあと、どこでも使わなくなった画像のファイルは消す。

消したあとは python tools/build.py --check を実行してから保存する。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history  # noqa: E402
from history import DATA, KINDS, ROOT, SITE, asset_paths, items_of, label_of, load_json, opening_id, save_json  # noqa: E402

SLUG_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def delete_path(path: Path) -> None:
    """git の記録からも外して消す（保存するときに、消したことも一緒に保存されるように）"""
    rel = path.relative_to(ROOT).as_posix()
    subprocess.run(["git", "rm", "-r", "-q", "--ignore-unmatch", "--", rel], cwd=ROOT, capture_output=True)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def used_elsewhere(asset: str) -> list[str]:
    """その画像・資料を、いまも使っているファイル"""
    needle = asset.lstrip("/")  # JSON では /assets/…、HTML や CSS では ../assets/… のように書かれている
    users = []
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix in (".json", ".html", ".css", ".js") and needle in p.read_text(encoding="utf-8"):
            users.append(p.relative_to(ROOT).as_posix())
    return users


def links_to(page: str) -> list[str]:
    """ほかの場所から、そのページへのリンク"""
    users = []
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix in (".json", ".html"):
            text = p.read_text(encoding="utf-8")
            if f"/{page}" in text or f"/{page.rstrip('/')}\"" in text:
                users.append(p.relative_to(ROOT).as_posix())
    return users


def list_openings() -> None:
    rec = load_json(DATA / "recruit.json")
    print("求人の一覧（消すときは python tools/remove.py openings 番号）")
    for n, o in enumerate(items_of(rec, "openings"), 1):
        status = f"［{o['status']}］" if o.get("status") else ""
        print(f"  {n:>2}. {label_of(o, 'openings')} {o.get('area', '')}{status}")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "photo":
        return remove_photo(args[1:])
    if not args or args[0] not in KINDS:
        print(__doc__)
        return 1
    kind = args[0]
    if kind == "openings" and len(args) == 1:
        list_openings()
        return 0
    if len(args) < 2:
        print(f"使い方: python tools/remove.py {kind} 対象")
        return 1
    target = args[1]
    spec = KINDS[kind]
    path = DATA / spec["file"]
    data = load_json(path)
    items = data.get(spec["list"], [])

    # 消す項目を探す
    if kind == "openings":
        if not target.isdigit() or not 1 <= int(target) <= len(items):
            print(f"求人の番号「{target}」がありません。python tools/remove.py openings で番号を確かめてください。")
            return 1
        index = int(target) - 1
    else:
        if not target or set(target) - SLUG_OK:
            print(f"「{target}」は、フォルダ名（半角の英小文字・数字・ハイフン）で指定してください。")
            return 1
        index = next((i for i, x in enumerate(items) if isinstance(x, dict) and x.get("slug") == target), None)

    folder = SITE / spec["folder"] / target if spec["folder"] else None
    if index is None:
        # 項目はもうないが、フォルダだけ残っているとき（差し戻しのあとなど）は、フォルダと画像を片付ける
        if folder and folder.is_dir():
            past = history.find(kind, target)
            item = past["item"] if past else {}
            delete_path(folder)
            print(f"{KINDS[kind]['label']}「{target}」の項目はすでにないため、残っていたフォルダ {folder.relative_to(ROOT)}/ を消しました。")
            cleanup_assets(item)
            return 0
        print(f"{KINDS[kind]['label']}「{target}」が見つかりません。")
        return 1
    item = items[index]

    # 職種は、その職種の社員紹介・求人が残っていると消せない
    if kind == "jobs":
        rec = data
        people = [p for p in items_of(rec, "people") if p.get("jobSlug") == target]
        openings = [(n, o) for n, o in enumerate(items_of(rec, "openings"), 1) if o.get("job") == target]
        if people or openings:
            print(f"職種「{item.get('name', target)}」には、まだ次のものがあります。先に消すか、ほかの職種に変えてから、もう一度実行してください。")
            for p in people:
                print(f"  社員紹介 {p.get('slug')}: {label_of(p, 'people')}  → python tools/remove.py people {p.get('slug')}")
            for n, o in openings:
                print(f"  求人 {n}番: {label_of(o, 'openings')}  → python tools/remove.py openings {n}")
            print("  （求人を続けて消すときは、番号がずれるので、1件消すたびに python tools/remove.py openings で番号を確かめる）")
            return 1

    # JSON から消す
    del items[index]
    save_json(path, data)
    print(f"{KINDS[kind]['label']}「{label_of(item, kind)}」を site/data/{spec['file']} から消しました。")

    # ページのフォルダ
    if folder and folder.is_dir():
        delete_path(folder)
        print(f"ページのフォルダ {folder.relative_to(ROOT)}/ を消しました。")

    cleanup_assets(item)

    # ほかの場所からのリンク
    if folder:
        page = f"{spec['folder']}/{target}/"
        refs = links_to(page)
        if refs:
            print(f"[注意] ほかの場所に、このページ（/{page}）へのリンクがあります: {'、'.join(refs)}")
            print("       リンクを外すか、別のページに変えるかを担当者に確認してください（このままだと --check がエラーになります）。")
    if kind == "jobs":
        print("[注意] 「4つの職種」のように職種の数を書いた文を直してください（--check が場所を教えてくれます）。")
        print("[注意] エントリーフォームの「ご希望の職種」の選択肢から外す作業は、管理者に頼むよう担当者に伝えてください。")

    key = opening_id(item) if kind == "openings" else target
    print(f"\nあとで戻すとき: python tools/history.py restore {kind} {key}（保存したあとでも戻せます）")
    print("次に python tools/build.py --check を実行してください。")
    return 0


def cleanup_assets(item: dict) -> None:
    for asset in asset_paths(item):
        file = SITE / asset.lstrip("/")
        if not file.is_file():
            continue
        users = used_elsewhere(asset)
        if users:
            print(f"画像・資料 {asset} は、ほかでも使っているので残しました（{'、'.join(users)}）。")
        else:
            delete_path(file)
            print(f"この項目だけで使っていた {asset} を消しました。")


# ---------------------------------------------------------------- 写真を外す

IMAGE_DIR = SITE / "assets" / "img"
# ページの見出しの下の大きな写真。枠（<div>）ごと外すと、ニュース一覧などと同じ「見出しのすぐ下に中身」の形になる
PH_IMG_RE = re.compile(r'<div class="ph-img container"><img\b[^>]*?\bsrc="([^"]+)"[^>]*></div>')
FIXED_REASON = "外すと、その場所に「写真予定」の枠が出ます"
LAYOUT_REASON = "写真の並び・カード・背景など、ページの形の一部です。外すと形が崩れます"
DATA_PLACES = {
    ("site.json", "story"): "アライアンスのページ「私たちの歩み」の写真",
    ("site.json", "cycle"): "「私たちの循環」の写真（トップ・アライアンスのページ）",
    ("site.json", "ceoMessage"): "代表メッセージの写真（トップ・アライアンスのページ）",
    ("recruit.json", "message"): "採用情報トップのメッセージの写真",
    ("recruit.json", "factory"): "採用情報トップの工場紹介（OUR FACTORY）の写真の並び",
}


def find_photo(arg: str) -> list[Path]:
    """写真の名前（contact-hero.jpg。拡張子は省いてもよい）や場所（/assets/img/photo/…）から、画像のファイルを探す"""
    text = arg.strip().split("?")[0].split("#")[0].replace("\\", "/")
    if "/" in text:
        rel = text.lstrip("./")
        rel = rel[len("site/"):] if rel.startswith("site/") else rel
        path = SITE / rel
        return [path] if path.is_file() and IMAGE_DIR in path.resolve().parents else []
    has_ext = bool(Path(text).suffix)
    return sorted(p for p in IMAGE_DIR.rglob("*")
                  if p.is_file() and (p.name == text if has_ext else p.stem == text))


def page_name(page: Path) -> str:
    rel = f"site/{page.relative_to(SITE).as_posix()}"
    m = re.search(r"<title>([^<｜|]*)", page.read_text(encoding="utf-8"))
    title = m.group(1).strip() if m else ""
    return f"{title}のページ（{rel}）" if title else rel


def walk_trail(value, trail: tuple = ()):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk_trail(v, trail + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk_trail(v, trail + (i,))
    else:
        yield trail, value


def json_use(name: str, data, t: tuple) -> dict:
    """JSON の中で写真を使っている場所。外せる場所なら、消す項目（どのリストの何番目か）も入れる"""
    def at(*keys):
        v = data
        for k in keys:
            v = v[k]
        return v if isinstance(v, dict) else {}

    if name == "news.json" and len(t) == 5 and (t[0], t[2], t[4]) == ("items", "blocks", "v") \
            and at("items", t[1], "blocks", t[3]).get("t") == "image":
        item = at("items", t[1])
        return {"kind": "json", "file": name, "removable": True, "list": ("items", t[1], "blocks"), "index": t[3],
                "where": f"ニュース「{item.get('t', '')}」（{item.get('slug', '')}）の本文の写真（本文の {t[3] + 1} 番目）"}
    if name == "recruit.json" and len(t) == 5 and (t[0], t[2], t[4]) == ("people", "qa", "img"):
        person, qa = at("people", t[1]), at("people", t[1], "qa", t[3])
        return {"kind": "json", "file": name, "removable": True, "list": ("people", t[1], "qa"), "index": t[3],
                "where": f"社員紹介 {person.get('slug', '')}（{person.get('name', '')}）の"
                         f"インタビューの途中の写真「{qa.get('cap', '')}」"}

    # ここから下は外せない場所
    head = t[0] if t else ""
    reason = LAYOUT_REASON
    if name == "recruit.json" and head == "people":
        p = at("people", t[1])
        where, reason = f"社員紹介 {p.get('slug', '')}（{p.get('name', '')}）の人物写真", FIXED_REASON
    elif name == "recruit.json" and head == "jobs":
        where, reason = f"職種「{at('jobs', t[1]).get('name', '')}」の写真", FIXED_REASON
    elif name == "products.json":
        where, reason = f"製品「{at(t[0]).get('name', '')}」の写真（アライアンスのページ）", FIXED_REASON
    elif name == "site.json" and head == "companies":
        c = at("companies", t[1])
        where = f"参画企業「{c.get('short') or c.get('name', '')}」のロゴ"
    else:
        where = DATA_PLACES.get((name, head)) or f"site/data/{name} の {'.'.join(map(str, t))}"
    return {"kind": "other", "removable": False, "where": where, "reason": reason}


def photo_uses(photo: Path) -> list[dict]:
    """その写真を使っている場所（外せるかどうか付き）"""
    rel = photo.relative_to(SITE).as_posix()  # assets/img/photo/x.jpg
    web = "/" + rel
    uses: list[dict] = []
    for path in sorted(DATA.glob("*.json")):
        data = load_json(path)
        for trail, value in walk_trail(data):
            if isinstance(value, str) and value.split("?")[0].split("#")[0] == web:
                uses.append(json_use(path.name, data, trail))
    needle = re.compile(re.escape(rel) + r"(?![\w.-])")
    for page in sorted(SITE.rglob("*")):
        if not page.is_file() or page.suffix not in (".html", ".css", ".js"):
            continue
        text = page.read_text(encoding="utf-8")
        total = len(needle.findall(text))
        if not total:
            continue
        label = page_name(page) if page.suffix == ".html" else f"見た目の設定（site/{page.relative_to(SITE).as_posix()}）"
        blocks = [m.group(0) for m in PH_IMG_RE.finditer(text)
                  if (page.parent / m.group(1).split("?")[0]).resolve() == photo.resolve()]
        for block in blocks:
            uses.append({"kind": "html", "page": page, "block": block, "removable": True,
                         "where": f"{label}の、見出しの下の大きな写真"})
        rest = total - len(blocks)
        if rest:
            uses.append({"kind": "other", "removable": False, "reason": LAYOUT_REASON,
                         "where": f"{label}の中" + (f"（{rest}か所）" if rest > 1 else "")})
    return uses


def show_uses(web: str, uses: list[dict]) -> None:
    print(f"写真 {web} を使っている場所:")
    for n, u in enumerate(uses, 1):
        mark = "外せる" if u["removable"] else f"外せない（{u['reason']}）"
        print(f"  {n}. {u['where']} … {mark}")


def remove_photo(args: list[str]) -> int:
    if not args:
        print("使い方: python tools/remove.py photo 写真の名前（例: contact-hero.jpg） [外す場所の番号]")
        return 1
    found = find_photo(args[0])
    if not found:
        print(f"写真「{args[0]}」が site/assets/img/ の中に見つかりません。"
              "ページの HTML や site/data/ の JSON で、その写真の名前を確かめてください。")
        return 1
    if len(found) > 1:
        print(f"「{args[0]}」という名前の写真が複数あります。場所を付けて指定してください: "
              + "、".join("/" + p.relative_to(SITE).as_posix() for p in found))
        return 1
    photo = found[0]
    web = "/" + photo.relative_to(SITE).as_posix()
    uses = photo_uses(photo)
    if not uses:
        print(f"写真 {web} は、サイトのどこにも使われていません（外すものはありません）。")
        return 1

    # 外す場所を決める
    if len(args) > 1:
        sel = args[1].strip()
        if sel.lower() in ("all", "全部"):
            chosen = list(range(len(uses)))
        else:
            try:
                chosen = sorted({int(x) - 1 for x in re.split(r"[,、，\s]+", sel) if x})
            except ValueError:
                chosen = []
            if not chosen or any(not 0 <= i < len(uses) for i in chosen):
                print(f"外す場所の番号「{sel}」が正しくありません。")
                show_uses(web, uses)
                return 1
    elif len(uses) == 1 or not any(u["removable"] for u in uses):
        chosen = list(range(len(uses)))  # 1か所だけのとき。どこからも外せないときは、下で理由を出して止まる
    else:
        show_uses(web, uses)
        print("\nこの写真は2か所以上で使っています。どこから外すかを担当者に確かめ、"
              f"python tools/remove.py photo {args[0]} 番号（複数なら 1,2、全部なら all）で、もう一度実行してください。")
        return 1

    blocked = [uses[i] for i in chosen if not uses[i]["removable"]]
    if blocked:
        print(f"写真 {web} は、次の場所からは外せません。ファイルは何も変えていません。")
        for u in blocked:
            print(f"  - {u['where']}：{u['reason']}")
        print("  → 別の写真に差し替える（edit の手順書の「3」）よう提案してください。"
              "どうしても外したいときは、ページの作りを変える必要があるので、管理者に相談するよう伝えてください。")
        if len(blocked) < len(uses):
            print()
            show_uses(web, uses)
        return 1

    # ページ（HTML）から、写真の枠ごと外す
    for u in (uses[i] for i in chosen if uses[i]["kind"] == "html"):
        page = u["page"]
        page.write_text(page.read_text(encoding="utf-8").replace(u["block"], "", 1), encoding="utf-8")
        print(f"{u['where']}を外しました（写真の枠ごと）。")
    # JSON から、その写真の項目を消す（同じリストの中は、後ろから消す）
    json_uses = sorted((uses[i] for i in chosen if uses[i]["kind"] == "json"),
                       key=lambda u: (u["file"], u["list"], -u["index"]))
    for name in dict.fromkeys(u["file"] for u in json_uses):
        path = DATA / name
        data = load_json(path)
        for u in (x for x in json_uses if x["file"] == name):
            target = data
            for key in u["list"]:
                target = target[key]
            del target[u["index"]]
            print(f"{u['where']}を外しました（site/data/{name}）。")
        save_json(path, data)

    # 画像のファイル
    users = used_elsewhere(web)
    if users:
        print(f"写真 {web} は、ほかの場所でも使っているので、ファイルは残しました（{'、'.join(users)}）。")
    else:
        delete_path(photo)
        print(f"どこでも使わなくなったので、画像のファイル {web} も消しました。")
    print("\nあとで戻すとき: restore の手順書の「外した写真を戻す」（保存したあとでも、変更の記録から戻せます）")
    print("次に python tools/build.py --check を実行してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
