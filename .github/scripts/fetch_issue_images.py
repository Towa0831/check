#!/usr/bin/env python3
"""Issue（または公開申請）に貼られた画像と、添付された資料をダウンロードする。

GitHub Actions の中で、AI（Claude・ChatGPT・Gemini）が動く前に実行する。
- 対象: Issue本文と、そのIssueのすべてのコメント（Botのコメントは除く）
- 公開申請（プルリクエスト）の場合は、説明文の「Closes #番号」から元のIssueもたどる
- 画像 → /tmp/issue-images/（一覧は list.txt。古い順）
  （貼ったときの元のファイル名も書く。GitHub は画像の説明（alt）に元のファイル名を入れるため）
- 資料（PDF・Word・Excel・PowerPoint・テキスト・CSV など）→ /tmp/issue-files/（一覧は list.txt。古い順）
  中身は tools/read_doc.py で文章にして読む
  取り出せなかった資料は、理由と一緒に failed.txt に書く（AI はこれを見て、担当者にどうすればよいかを伝える）。
  非公開のリポジトリでは、添付された資料はブラウザでログインした人しか取り出せない（GitHub の鍵を使っても取れない）
- 資料置き場（main の inbox/）の資料 → /tmp/issue-files/（list.txt に「資料置き場」として載る）
  このIssueのやり取りでファイル名が書かれているものだけを取り出す。ほかの資料は取り出さない（名前の一覧だけ inbox.txt に書く）
  資料置き場は、AI が作業する場所からは見えないようにしてある（ワークフローで sparse-checkout）

必要な環境変数: GH_TOKEN, REPO, ISSUE_NUMBER
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Issueに貼った画像の元のURL（Markdown形式・imgタグ形式のどちらでも拾える）
ORIGINAL_RE = re.compile(
    r"https://github\.com/user-attachments/assets/[0-9A-Za-z-]+"
    r"|https://(?:private-)?user-images\.githubusercontent\.com/[^\s\"'<>)]+"
)
# 非公開リポジトリで使われる、期限付きのダウンロード用URL
SIGNED_RE = re.compile(r"https://private-user-images\.githubusercontent\.com/[^\s\"'<>]+")
# 画像の説明（alt）。GitHub に画像を貼ると、ここに元のファイル名（拡張子なし）が入る
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?(https://[^\s)>]+)>?")
HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""\b(alt|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# 添付された資料（画像以外のファイル）のURL。Markdown では [元のファイル名](URL) の形で入る
FILE_URL = (r"https://github\.com/(?:user-attachments/files|[\w.-]+/[\w.-]+/files)/\d+/[^\s)\"'<>]+")
FILE_RE = re.compile(FILE_URL)
MD_FILE_RE = re.compile(r"\[([^\]]*)\]\(\s*<?(" + FILE_URL + r")>?")
DOC_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv", ".tsv", ".json", ".log"}
MAX_FILE = 30 * 1024 * 1024
# 資料置き場（担当者が GitHub の画面から資料を入れておくフォルダ。main に置く）
INBOX = "inbox"
INBOX_SKIP = {f"{INBOX}/README.md"}
STEM_MIN = 3        # 拡張子を省いた名前で探すときの、最低の文字数（短い名前は、ほかの言葉と取り違えやすい）
OLD_DAYS = 1        # Issue を作るより、これより前から置いてある資料は、使う前に担当者に確かめてもらう
INFO_MAX = 60       # 置いた日時を調べるファイルの数の上限
LINKED_ISSUE_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
JST = dt.timezone(dt.timedelta(hours=9))


def api(path: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['REPO']}" + (f"/{path}" if path else ""),
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github.full+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "issue-image-fetcher"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def image_names(body: str) -> dict[str, str]:
    """本文の画像の説明（alt）を、画像の識別子（GUID か URL）ごとに返す。元のファイル名にあたる"""
    names: dict[str, str] = {}

    def keep(alt: str, url: str) -> None:
        alt = html.unescape(alt or "").strip()
        if not alt or alt.lower() in ("image", "img", "画像"):
            return
        match = GUID_RE.search(url)
        names.setdefault(match.group(0).lower() if match else url, alt)

    for alt, url in MD_IMG_RE.findall(body or ""):
        keep(alt, url)
    for tag in HTML_IMG_RE.findall(body or ""):
        attrs = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in ATTR_RE.finditer(tag)}
        if attrs.get("src"):
            keep(attrs.get("alt", ""), attrs["src"])
    return names


def find_images(body: str, body_html: str) -> list[tuple[str, list[str]]]:
    """本文から画像URLを探し、(元のURL, ダウンロードを試すURLの候補) の一覧を返す。"""
    signed = SIGNED_RE.findall(html.unescape(body_html or ""))
    results, seen = [], set()
    for url in ORIGINAL_RE.findall(body or ""):
        match = GUID_RE.search(url)
        key = match.group(0).lower() if match else url
        if key in seen:
            continue
        seen.add(key)
        candidates = [s for s in signed if match and key in s.lower()] + [url]
        results.append((url, candidates))
    return results


def download(candidates: list[str], fetcher=None) -> bytes | None:
    fetcher = fetcher or fetch
    for url in candidates:
        try:
            data = fetcher(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  取得に失敗: {url[:70]}... ({exc})")
            continue
        if image_ext(data):
            return data
        print(f"  画像ではないデータでした: {url[:70]}...")
    return None


def find_files(body: str) -> list[tuple[str, str]]:
    """本文から添付された資料を探し、(URL, 元のファイル名) の一覧を返す"""
    names = {url: html.unescape(text).strip() for text, url in MD_FILE_RE.findall(body or "")}
    results, seen = [], set()
    for url in FILE_RE.findall(body or ""):
        if url in seen:
            continue
        seen.add(url)
        name = names.get(url) or urllib.parse.unquote(url.rsplit("/", 1)[-1])
        results.append((url, name))
    return results


def download_file(url: str) -> tuple[bytes | None, str]:
    """資料をダウンロードする（大きすぎるものは取らない）。(中身, 取れなかった理由) を返す"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "issue-file-fetcher"})
        with urllib.request.urlopen(req, timeout=60) as res:
            data = res.read(MAX_FILE + 1)
    except Exception as exc:  # noqa: BLE001
        print(f"  取得に失敗: {url[:70]}... ({exc})")
        return None, "取得失敗"
    if len(data) > MAX_FILE:
        print(f"  大きすぎるため取りませんでした（{MAX_FILE // 1024 // 1024}MBまで）: {url[:70]}...")
        return None, "大きすぎる"
    return data, ""


def repo_is_private() -> bool:
    """このリポジトリが非公開かどうか（分からないときは False）"""
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as f:
            private = json.load(f).get("repository", {}).get("private")
        if isinstance(private, bool):
            return private
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(api("").get("private"))
    except Exception as exc:  # noqa: BLE001
        print(f"  リポジトリが公開か非公開かを確かめられませんでした（{exc}）")
        return False


def norm(text: str) -> str:
    """全角・半角や大文字・小文字の違いをなくして比べるための形にする"""
    return unicodedata.normalize("NFKC", text).casefold()


def default_branch() -> str:
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as f:
            return json.load(f).get("repository", {}).get("default_branch") or "main"
    except Exception:  # noqa: BLE001
        return "main"


def list_inbox(branch: str) -> list[dict]:
    """資料置き場にあるファイルの一覧（path, size）"""
    tree = api(f"git/trees/{urllib.parse.quote(branch)}?recursive=1")
    return [{"path": t["path"], "size": t.get("size") or 0} for t in tree.get("tree", [])
            if t.get("type") == "blob" and t["path"].startswith(INBOX + "/") and t["path"] not in INBOX_SKIP]


def inbox_info(path: str, branch: str) -> tuple[str, str]:
    """最後にそのファイルを置いた（変えた）日時と人"""
    commits = api(f"commits?path={urllib.parse.quote(path)}&sha={urllib.parse.quote(branch)}&per_page=1")
    if not commits:
        return "", ""
    c = commits[0]
    who = (c.get("author") or {}).get("login") or c["commit"]["author"].get("name", "")
    return c["commit"]["committer"].get("date", ""), who


def read_inbox(path: str, branch: str) -> bytes:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['REPO']}/contents/{urllib.parse.quote(path)}"
        f"?ref={urllib.parse.quote(branch)}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read(MAX_FILE + 1)


# 同じ名前のファイルを保存・コピーしたときに、パソコンが名前の後ろに付ける印
# 「営業_原稿 (1)」（ブラウザでのダウンロード）、「営業_原稿 - コピー」（Windows）、「営業_原稿 のコピー」（Mac）など
COPY_MARK_RE = re.compile(r"(?:\s*\(\d+\)|\s*-\s*(?:コピー|copy)|\s*のコピー(?:\s*\d+)?|\s+copy(?:\s*\d+)?)$", re.I)


def plain_stem(name: str) -> str:
    """拡張子と、コピーの印を除いた名前"""
    stem = norm(os.path.splitext(name)[0]).strip()
    while True:
        cut = COPY_MARK_RE.sub("", stem).strip()
        if cut == stem:
            return stem
        stem = cut


def inbox_keys(path: str) -> list[tuple[str, int]]:
    """Issueの文章から探す名前と、その強さ（3: 拡張子まで一致、2: 拡張子を省いて一致、1: コピーの印も省いて一致）。
    1 の一致（「営業_原稿」と書かれていて、置いてあるのは「営業_原稿 (1).docx」）では取り出さず、
    「名前が少し違う候補」として担当者に確かめてもらう"""
    name = path.rsplit("/", 1)[-1]
    stem = os.path.splitext(name)[0]
    keys = [(norm(name), 3)]
    if len(stem) >= STEM_MIN and stem != name:
        keys.append((norm(stem), 2))
    plain = plain_stem(name)
    if len(plain) >= STEM_MIN and plain != norm(stem):
        keys.append((plain, 1))
    return keys


def pick_inbox(posts: list[dict], files: list[dict]) -> tuple[list[tuple[dict, dict]], list[tuple[dict, dict]]]:
    """このIssueのやり取りで名前が書かれた資料だけを選ぶ。(ファイル, 最初に書かれた投稿) の一覧を2つ返す
    - 選んだ資料: 名前（拡張子は省いてもよい）が書かれているもの
    - 名前が少し違う候補: 後ろの「 (1)」などの印だけが違うもの（取り出さない。担当者に確かめてもらう）
    「営業_原稿_旧.docx」と書かれていたら「営業_原稿.docx」は選ばない（長い名前の一部としての一致は数えない）"""
    picked: dict[str, tuple[dict, dict]] = {}
    near: dict[str, tuple[dict, dict]] = {}
    for post in posts:
        text = norm(post.get("title", "") + "\n" + post.get("body", ""))
        spans = []
        for f in files:
            for key, rank in inbox_keys(f["path"]):
                spans += [(m.start(), m.end(), f["path"], rank) for m in re.finditer(re.escape(key), text)]
        for start, end, path, rank in spans:
            beaten = any(other != path and s <= start and end <= e
                         and ((e - s) > (end - start) or (s == start and e == end and r > rank))
                         for s, e, other, r in spans)
            if beaten:
                continue
            target = picked if rank >= 2 else near
            if path not in target:
                target[path] = (next(f for f in files if f["path"] == path), post)
    near = {k: v for k, v in near.items() if k not in picked}
    return list(picked.values()), list(near.values())


def iso(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def saved_name(number: int, ext: str) -> str:
    return f"file-{number}{ext}"


def save_inbox(posts: list[dict], out_dir: str, count: int, lines: list[str], failed: list[tuple]) -> int:
    """資料置き場から、名前が書かれた資料だけを取り出して out_dir に保存する。inbox.txt に名前の一覧を書く"""
    branch = default_branch()
    upload = f"https://github.com/{os.environ.get('REPO', '')}/upload/{branch}/{INBOX}"
    try:
        files = list_inbox(branch)
    except Exception as exc:  # noqa: BLE001
        print(f"資料置き場の一覧を取れませんでした（{exc}）")
        files = []
    info: dict[str, tuple[str, str]] = {}
    for f in files[:INFO_MAX]:
        try:
            info[f["path"]] = inbox_info(f["path"], branch)
        except Exception:  # noqa: BLE001
            info[f["path"]] = ("", "")
    starts = [iso(p.get("created_at", "")) for p in posts]
    starts = [t for t in starts if t]
    issue_start = min(starts) if starts else None

    picked, near = pick_inbox(posts, files)
    status: dict[str, str] = {}
    for f, post in near:
        status[f["path"]] = ("　← このIssueに書かれた名前と、後ろの「 (1)」などだけが違う。取り出していない。"
                             "勝手に使わず、これのことか担当者に確かめる")
    for f, post in picked:
        path, name = f["path"], f["path"].rsplit("/", 1)[-1]
        when, who = info.get(path, ("", ""))
        placed = f"{who} {jst_time(when)} に置いた。" if when else ""
        where = f"資料置き場 {path}（{placed}{post['where']}（{post['user']} {post['time']}）で指定）"
        placed_at = iso(when)
        if issue_start and placed_at and placed_at < issue_start - dt.timedelta(days=OLD_DAYS):
            days = (issue_start - placed_at).days
            where += f"【このIssueを作る{days}日前から置いてある資料。使う前に、この資料でよいか担当者に確かめる】"
        url = f"https://github.com/{os.environ.get('REPO', '')}/blob/{branch}/{urllib.parse.quote(path)}"
        ext = os.path.splitext(name)[1].lower()
        if ext not in DOC_EXTS:
            print(f"- 資料として読まない種類のため取りませんでした: {path}")
            failed.append((name, where, "読めない種類", url))
            status[path] = "　← このIssueで指定されたが、取り出せなかった（failed.txt）"
            continue
        if f.get("size", 0) > MAX_FILE:
            failed.append((name, where, "大きすぎる", url))
            status[path] = "　← このIssueで指定されたが、取り出せなかった（failed.txt）"
            continue
        try:
            data = read_inbox(path, branch)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 資料置き場から取り出せませんでした: {path} ({exc})")
            failed.append((name, where, "取得失敗", url))
            status[path] = "　← このIssueで指定されたが、取り出せなかった（failed.txt）"
            continue
        status[path] = f"　← このIssueで指定されたので取り出した（{saved_name(count + 1, ext)}）"
        count += 1
        saved = saved_name(count, ext)
        with open(os.path.join(out_dir, saved), "wb") as fp:
            fp.write(data)
        lines.append(f"{saved}\t{name.replace(chr(9), ' ')}\t{where}\t{url}")
        print(f"✓ {saved}（資料置き場の {path}）を保存しました")

    def newest(f):
        return info.get(f["path"], ("", ""))[0] or ""
    names = []
    for f in sorted(files, key=newest, reverse=True):
        when, who = info.get(f["path"], ("", ""))
        mark = status.get(f["path"], "")
        names.append(f"- {f['path'][len(INBOX) + 1:]}" + (f"（{who} {jst_time(when)} に置いた）" if when else "") + mark)
    with open(os.path.join(out_dir, "inbox.txt"), "w", encoding="utf-8") as fp:
        fp.write(f"資料置き場に資料を入れるリンク: {upload}\n")
        got = sum(1 for v in status.values() if "取り出した（" in v)
        fp.write(f"このIssueで名前が書かれていて取り出した資料: {got} 件（list.txt に「資料置き場」として載っている）\n")
        if near:
            fp.write(f"名前が少し違う候補: {len(near)} 件（後ろの「 (1)」などだけが違う。取り出していない。"
                     "下の一覧で印が付いているもの。これのことか担当者に確かめる）\n")
        fp.write("資料置き場にあるファイルの名前（新しい順。中身は取り出していない。"
                 "指定が見つからないときに、名前の候補として見せるためだけに使う）:\n")
        fp.write("".join(n + "\n" for n in names) if names else "（なし）\n")
    print(f"資料置き場: {len(files)} 件のうち、このIssueで指定された {len(picked)} 件を対象にしました")

    # Issueに添付して読めなかった資料と同じ名前の資料を、資料置き場から取り出せたら、「読めなかった」一覧から外す
    def same_name(name: str) -> str:
        return norm(os.path.splitext(name)[0]).strip()
    got = {same_name(f["path"].rsplit("/", 1)[-1]) for f, _ in picked if "取り出した（" in status.get(f["path"], "")}
    before = len(failed)
    failed[:] = [row for row in failed
                 if row[1].startswith("資料置き場") or same_name(row[0]) not in got]
    if len(failed) < before:
        print(f"添付して読めなかった資料のうち {before - len(failed)} 件は、資料置き場から取り出せました")
    return count


def save_files(posts: list[dict], out_dir: str, downloader=None, private=None, inbox=True) -> int:
    """添付された資料を out_dir に file-1.pdf などの名前で保存し、list.txt を書く。
    取り出せなかった資料は、理由と一緒に failed.txt に書く（なければ failed.txt は作らない）"""
    downloader = downloader or download_file
    os.makedirs(out_dir, exist_ok=True)
    lines, failed, seen, count = [], [], set(), 0
    for post in posts:
        for url, name in find_files(post["body"]):
            if url in seen:
                continue
            seen.add(url)
            ext = os.path.splitext(urllib.parse.unquote(url))[1].lower()
            where = f"{post['where']}（{post['user']} {post['time']}）"
            if ext not in DOC_EXTS:
                print(f"- 資料として読まない種類のため取りませんでした: {name}（{where}）")
                failed.append((name, where, "読めない種類", url))
                continue
            data, reason = downloader(url)
            if data is None:
                if reason == "取得失敗":
                    if private is None:
                        private = repo_is_private()
                    if private:
                        reason = "非公開"
                print(f"✗ ダウンロードできませんでした（{reason}）: {where} {url}")
                failed.append((name, where, reason, url))
                continue
            count += 1
            saved = f"file-{count}{ext}"
            with open(os.path.join(out_dir, saved), "wb") as f:
                f.write(data)
            lines.append(f"{saved}\t{name.replace(chr(9), ' ')}\t{where}\t{url}")
            print(f"✓ {saved}（元の名前: {name}）を保存しました: {where}")
    if inbox:
        count = save_inbox(posts, out_dir, count, lines, failed)
    with open(os.path.join(out_dir, "list.txt"), "w", encoding="utf-8") as f:
        f.write("ファイル名\t元の名前（添付したときのファイル名）\t添付されていた場所（投稿者 日時）\t元のURL\n")
        f.write("".join(line + "\n" for line in lines))
    failed_path = os.path.join(out_dir, "failed.txt")
    if failed:
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write("元の名前（添付したときのファイル名）\t添付されていた場所（投稿者 日時）\t理由\t元のURL\n")
            f.write("".join("\t".join(v.replace("\t", " ") for v in row) + "\n" for row in failed))
    elif os.path.exists(failed_path):
        os.remove(failed_path)
    print(f"合計 {count} 件の資料を {out_dir}/ に保存しました（番号が大きいほど新しい。中身は python tools/read_doc.py で読む）")
    if failed:
        print(f"取り出せなかった資料: {len(failed)} 件（理由は {failed_path}）")
    return count


def jst_time(text: str) -> str:
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(JST).strftime("%m/%d %H:%M")
    except (AttributeError, ValueError):
        return ""


def collect_posts(issue_no: str, getter=None) -> list[dict]:
    """Issue本文とコメントを古い順に集める。公開申請なら元のIssueも含める。"""
    getter = getter or api
    posts, visited, queue = [], set(), [issue_no]
    while queue:
        number = queue.pop(0)
        if not number or number in visited:
            continue
        visited.add(number)
        try:
            issue = getter(f"issues/{number}")
        except Exception as exc:  # noqa: BLE001
            print(f"#{number} を取得できませんでした: {exc}")
            continue
        posts.append({
            "where": f"#{number} 本文",
            "title": issue.get("title") or "",
            "user": (issue.get("user") or {}).get("login", ""),
            "time": jst_time(issue.get("created_at", "")),
            "created_at": issue.get("created_at", ""),
            "body": issue.get("body") or "",
            "body_html": issue.get("body_html") or "",
        })
        queue += LINKED_ISSUE_RE.findall(issue.get("body") or "")
        try:
            comments = getter(f"issues/{number}/comments?per_page=100")
        except Exception as exc:  # noqa: BLE001
            print(f"#{number} のコメントを取得できませんでした: {exc}")
            comments = []
        for comment in comments:
            user = comment.get("user") or {}
            if user.get("type") == "Bot":
                continue
            posts.append({
                "where": f"#{number} コメント",
                "user": user.get("login", ""),
                "time": jst_time(comment.get("created_at", "")),
                "created_at": comment.get("created_at", ""),
                "body": comment.get("body") or "",
                "body_html": comment.get("body_html") or "",
            })
    posts.sort(key=lambda p: p["created_at"])
    return posts


def main() -> int:
    out_dir = os.environ.get("IMAGE_DIR", "/tmp/issue-images")
    os.makedirs(out_dir, exist_ok=True)
    posts = collect_posts(os.environ.get("ISSUE_NUMBER", ""))

    lines, seen, count = [], set(), 0
    for post in posts:
        names = image_names(post["body"])
        for url, candidates in find_images(post["body"], post["body_html"]):
            match = GUID_RE.search(url)
            key = match.group(0).lower() if match else url
            if key in seen:
                continue
            seen.add(key)
            data = download(candidates)
            where = f"{post['where']}（{post['user']} {post['time']}）"
            if data is None:
                print(f"✗ ダウンロードできませんでした: {where} {url}")
                continue
            count += 1
            name = f"image-{count}{image_ext(data)}"
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(data)
            original = names.get(key, "").replace("\t", " ")
            lines.append(f"{name}\t{original}\t{where}\t{url}")
            print(f"✓ {name}（元の名前: {original or 'なし'}）を保存しました: {where}")

    with open(os.path.join(out_dir, "list.txt"), "w", encoding="utf-8") as f:
        f.write("ファイル名\t元の名前（貼ったときのファイル名）\t貼られていた場所（投稿者 日時）\t元のURL\n")
        f.write("".join(line + "\n" for line in lines))
    print(f"合計 {count} 枚の画像を {out_dir}/ に保存しました（番号が大きいほど新しい）")
    save_files(posts, os.environ.get("FILE_DIR", "/tmp/issue-files"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
