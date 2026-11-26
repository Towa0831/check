#!/usr/bin/env python3
"""@chatgpt・@gemini で呼ばれたときの、AI の前と後の処理（.github/workflows/ai.yml から呼ぶ）。

  [start 段]  python .github/scripts/ai_run.py start    呼ばれた AI と、頼んだ人の権限を確かめ、「作業中」のコメントを出す
  [work 段]   python .github/scripts/ai_run.py prepare  作業する下書きに切り替える（このスクリプトは /tmp/ai に写す）
              python /tmp/ai/ai_run.py prompt           AI に渡す依頼文（/tmp/ai/prompt.md）を作る
              （ここで AI が作業する）
              python /tmp/ai/ai_run.py package          AI の変更を「変更のまとめ」（/tmp/ai/out/changes.patch）にし、返事と一緒に渡す
  [finish 段] python .github/scripts/ai_run.py finish   まっさらな環境で、変更のまとめのうち site/ の中だけを取り込んでチェックし、
                                                        下書きに保存し、確認用ページを頼み、返事をコメントする

Claude では claude-code-action が自動でしていること（作業中のコメント、下書きの保存、Create PR のリンク）を、
ここで同じようにする。AI はファイルを変えるだけで、保存（コミット・プッシュ）はしない。
AI が作業する work 段には、保存の権限を渡さない。保存するのは site/ の中の変更だけ（AGENTS.md・CLAUDE.md の「触ってよいファイル」）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORK = Path("/tmp/ai")
OUT = WORK / "out"
JST = timezone(timedelta(hours=9))
AIS = {"chatgpt": "ChatGPT", "gemini": "Gemini"}
MENTION_RE = re.compile(r"(?<![\w.@-])@(claude|chatgpt|gemini)\b", re.I)
COMMIT_RE = re.compile(r"<!--\s*commit:\s*(.*?)\s*-->", re.S)
DRAFT_RE = r"^origin/((?:claude|chatgpt|gemini)/issue-{number}-[^\s]+)$"
WEEKDAYS = "月火水木金土日"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_COMMENT = 60000


# ---------------------------------------------------------------- 共通

def run(*args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, input=input)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} … が失敗しました: {result.stderr.strip()[-500:]}")
    return result


def git(*args: str, check: bool = True) -> str:
    return run("git", *args, check=check).stdout.strip()


def gh_api(path: str, *args: str, check: bool = True):
    out = run("gh", "api", path, *args, check=check).stdout
    return json.loads(out) if out.strip() else None


def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def event() -> dict:
    return json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))


def output(**values) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}<<__END__\n{value}\n__END__\n")


def post_comment(number: int, body: str) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / "comment.md"
    path.write_text(body, encoding="utf-8")
    return gh_api(f"repos/{repo()}/issues/{number}/comments", "-X", "POST", "-F", f"body=@{path}")["id"]


def edit_comment(comment_id: str, body: str) -> None:
    """「作業を始めました」のコメントを書き換える。消されていたら、新しいコメントで知らせる"""
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / "comment.md"
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + "\n\n（返事が長すぎるため、ここで切りました）"
    path.write_text(body, encoding="utf-8")
    try:
        gh_api(f"repos/{repo()}/issues/comments/{comment_id}", "-X", "PATCH", "-F", f"body=@{path}")
    except RuntimeError:
        post_comment(int(os.environ["NUMBER"]), body)


def jst(text: str) -> str:
    """GitHub の日時（UTC）を、日本時間の「2026-11-12 13:05」にする"""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def run_url() -> str:
    return f"{os.environ['GITHUB_SERVER_URL']}/{repo()}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


# ---------------------------------------------------------------- start

def start() -> int:
    ev = event()
    issue = ev["issue"]
    number = issue["number"]
    source = ev.get("comment") or issue
    actor = source["user"]["login"]
    text = source.get("body") or ""
    if "comment" not in ev:
        text = f"{issue.get('title') or ''}\n{text}"

    names = {m.lower() for m in MENTION_RE.findall(text)}
    called = names & AIS.keys()
    if not called:
        print("@chatgpt・@gemini の呼び出しではありません（単語の一部など）。何もしません。")
        output(skip="true")
        return 0

    permission = (gh_api(f"repos/{repo()}/collaborators/{actor}/permission", check=False) or {}).get("permission")
    if permission not in ("admin", "maintain", "write"):
        print(f"{actor} さんには書き込みの権限がないため、何もしません（{permission}）。")
        output(skip="true")
        return 0

    if len(names) > 1:
        listed = "、".join(f"`@{n}`" for n in sorted(names))
        post_comment(number, f"1つのコメントで呼べる AI は1つだけです（今回は {listed} が書かれていました）。"
                             "\n\n次にやること: どれか1つだけを書いて、もう一度コメントしてください"
                             "（例: `@chatgpt 〇〇をお願いします`）。")
        output(skip="true")
        return 0

    ai = called.pop()
    label = AIS[ai]
    is_pr = "pull_request" in issue
    if is_pr:
        pr = json.loads(run("gh", "pr", "view", str(number), "--repo", repo(),
                            "--json", "headRefName,isCrossRepository,state").stdout)
        if pr["isCrossRepository"] or pr["state"] != "OPEN":
            post_comment(number, f"この公開申請では {label} は作業できません（閉じている、またはほかのリポジトリからの公開申請です）。")
            output(skip="true")
            return 0
        branch = pr["headRefName"]
    else:
        branch = f"{ai}/issue-{number}-{datetime.now(JST):%Y%m%d-%H%M}"

    comment_id = post_comment(
        number,
        f"🤖 **{label}** が作業を始めました。終わると、このコメントが返事に置き換わります（数分〜20分ほどかかります）。"
        f"\n\n[作業の記録]({run_url()})",
    )
    output(skip="false", ai=ai, label=label, branch=branch, is_pr=str(is_pr).lower(), number=str(number),
           comment_id=str(comment_id), started=str(int(time.time())))
    print(f"{label} を呼び出します（{'公開申請' if is_pr else 'Issue'} #{number}、下書き {branch}）")
    return 0


# ---------------------------------------------------------------- prepare（work 段）

def prepare() -> int:
    # 公開申請の下書きは、この仕組みより前に作られたものもある。下書きに切り替えても使えるように、
    # このあと使うスクリプトを /tmp/ai に写しておく（ai.yml の work 段の後の手順は、そこから動かす）
    for folder in (WORK, Path("/tmp/issue-images"), Path("/tmp/github-images"), Path("/tmp/issue-files")):
        folder.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    for name in ("ai_run.py", "fetch_issue_images.py"):
        shutil.copy(here / name, WORK / name)
    branch = os.environ["BRANCH"]
    base = f"origin/{branch}" if os.environ["IS_PR"] == "true" else "origin/main"
    git("checkout", "-q", "-B", branch, base)
    output(start_sha=git("rev-parse", "HEAD"))
    print(f"下書き {branch} に切り替えました（もと: {base}）")
    return 0


# ---------------------------------------------------------------- package（work 段）

def package() -> int:
    """AI が変えたものを、変更のまとめ（差分）にする。保存は finish 段が、site/ の中だけを取り込んで行う"""
    OUT.mkdir(parents=True, exist_ok=True)
    for junk in (".gemini", "gemini-artifacts"):  # Gemini の部品が置いた設定と、作業の記録
        if not git("ls-files", junk, check=False):
            shutil.rmtree(junk, ignore_errors=True)
    run("git", "add", "-A", check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--binary", "--no-renames", os.environ["START_SHA"]],
                          capture_output=True)
    if diff.stdout:
        (OUT / "changes.patch").write_bytes(diff.stdout)
    names = git("diff", "--cached", "--name-only", "--no-renames", os.environ["START_SHA"], check=False)
    reply = WORK / "reply.md"
    if reply.is_file():
        shutil.copy(reply, OUT / "reply.md")
    print("変わったファイル:\n" + (names or "（なし）"))
    print(f"返事: {'あり' if reply.is_file() and reply.read_text(encoding='utf-8').strip() else 'なし'}")
    return 0


# ---------------------------------------------------------------- prompt

def all_comments(number: int) -> list[dict]:
    comments, page = [], 1
    while True:
        batch = gh_api(f"repos/{repo()}/issues/{number}/comments?per_page=100&page={page}") or []
        comments += batch
        if len(batch) < 100:
            return comments
        page += 1


def drafts(number: int) -> list[str]:
    """このIssueの下書き（新しい順）と、その公開申請の状態"""
    refs = git("for-each-ref", "--sort=-committerdate",
               "--format=%(refname:short)\t%(committerdate:format-local:%Y-%m-%d %H:%M)", "refs/remotes/origin/")
    lines = []
    pattern = re.compile(DRAFT_RE.format(number=number))
    for row in refs.splitlines():
        name, _, when = row.partition("\t")
        m = pattern.match(name)
        if not m:
            continue
        branch = m.group(1)
        prs = json.loads(run("gh", "pr", "list", "--repo", repo(), "--head", branch, "--state", "all",
                             "--json", "number,state", check=False).stdout or "[]")
        state = {"OPEN": "公開申請中", "MERGED": "公開済み", "CLOSED": "公開申請を閉じた"}
        note = "、".join(f"公開申請 #{p['number']}（{state.get(p['state'], p['state'])}）" for p in prs) or "公開申請なし"
        lines.append(f"- `{branch}`（{when} 保存、{note}）")
    return lines


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"\n\n（長いため、ここから先 {len(text) - limit} 文字を省きました）"


def prompt() -> int:
    ev = event()
    number = int(os.environ["NUMBER"])
    ai, label, branch = os.environ["AI"], os.environ["LABEL"], os.environ["BRANCH"]
    is_pr = os.environ["IS_PR"] == "true"
    progress_id = int(os.environ["COMMENT_ID"])
    issue = gh_api(f"repos/{repo()}/issues/{number}")
    trigger_id = (ev.get("comment") or {}).get("id")

    thread = [("Issue の本文" if not is_pr else "公開申請の説明", issue["user"]["login"], issue["created_at"],
               issue.get("body") or "（本文なし）", trigger_id is None)]
    for c in all_comments(number):
        if c["id"] == progress_id:
            continue
        thread.append(("コメント", c["user"]["login"], c["created_at"], c.get("body") or "", c["id"] == trigger_id))
    if len(thread) > 41:  # 本文と、最近の40件
        thread = thread[:1] + [("（省略）", "", "", f"古いコメント {len(thread) - 41} 件を省きました。", False)] + thread[-40:]

    images = Path("/tmp/issue-images/list.txt")
    image_count = max(0, len(images.read_text(encoding="utf-8").splitlines()) - 1) if images.is_file() else 0
    files = Path("/tmp/issue-files/list.txt")
    file_count = max(0, len(files.read_text(encoding="utf-8").splitlines()) - 1) if files.is_file() else 0
    failed = Path("/tmp/issue-files/failed.txt")
    failed_count = max(0, len(failed.read_text(encoding="utf-8").splitlines()) - 1) if failed.is_file() else 0
    today = datetime.now(JST)
    draft_lines = drafts(number) if not is_pr else []
    if is_pr:
        where = f"公開申請 #{number}「{issue['title']}」"
        work = f"`{branch}`（この公開申請の下書き。ここを直接直す）"
    else:
        where = f"Issue #{number}「{issue['title']}」"
        work = f"`{branch}`（main の最新から、今回新しく作ったもの。まだ何も変えていない）"

    parts = [
        f"# 依頼\n",
        f"あなたは **{label}** です。GitHub の {where} で、担当者に `@{ai}` と呼ばれました。\n",
        "まず、リポジトリの `AGENTS.md` と `CLAUDE.md` を最初から最後まで読み、その決まりに従って、"
        "下の「やり取り」の最後にある **今回のコメント** に答えてください。"
        "作業の種類に合う `claude/skills/〇〇/SKILL.md` も読んでください。\n",
        "## 今の状況\n",
        f"- 場所: {where}",
        f"- 作業する下書き: {work}",
    ]
    if not is_pr:
        parts.append("- このIssueの下書き（新しい順）:" + ("\n  " + "\n  ".join(draft_lines) if draft_lines else " なし"))
    parts += [
        f"- 貼られた画像: " + (f"{image_count} 枚（一覧は `/tmp/issue-images/list.txt`）" if image_count else "なし"),
        f"- 添付された資料: " + (f"{file_count} 件（一覧は `/tmp/issue-files/list.txt`。中身は `python tools/read_doc.py` で読む）"
                            if file_count else "なし"),
    ]
    if failed_count:
        parts.append(f"- 取り出せなかった資料: {failed_count} 件（理由は `/tmp/issue-files/failed.txt`。"
                     "伝え方は CLAUDE.md の「画像と資料」）")
    if Path("/tmp/issue-files/inbox.txt").is_file():
        parts.append("- 資料置き場: このIssueで名前が書かれた資料だけを取り出してある（上の一覧に「資料置き場」として載る）。"
                     "ほかの資料は読まない。名前の一覧と、資料を入れるリンクは `/tmp/issue-files/inbox.txt`")
    parts += [
        f"- 今日: {today:%Y-%m-%d}（{WEEKDAYS[today.weekday()]}）",
        "",
        "## やり取り（古い順。最後が今回のコメント）\n",
    ]
    for i, (kind, login, when, body, is_trigger) in enumerate(thread, 1):
        title = "今回のコメント" if is_trigger else kind
        who = f" — @{login}（{jst(when)}）" if login else ""
        parts.append(f"### {i}. {title}{who}\n")
        parts.append(clip(body, 30000 if is_trigger or i == 1 else 8000) + "\n")
    parts += [
        "## 終わり方（必ず守る）\n",
        "- `git add`・`git commit`・`git push` はしない（保存は、あなたの作業のあとに仕組みが行う）",
        "- ファイルを変えたときは、返事の最後の行に `<!-- commit: 日本語のコミットメッセージ -->` を書く。"
        "変えなかったときは書かない",
        f"- 最後に書くメッセージ（返事）が、そのまま担当者へのコメントになる。`CLAUDE.md` の「話し方のルール」に従い、"
        f"自分の呼び名は `@{ai}` と書く",
    ]
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "prompt.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"依頼文を作りました: {WORK / 'prompt.md'}（{len(''.join(parts))} 文字、やり取り {len(thread)} 件、画像 {image_count} 枚）")
    return 0


# ---------------------------------------------------------------- finish

def changed_paths() -> list[tuple[str, str]]:
    """(状態, ファイル) の一覧。状態は ?? が新しいファイル、D が消したファイル、M が変えたファイル"""
    out = run("git", "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    rows = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            rows.append((entry[:2].strip() or "M", entry[3:]))
    return rows


def undo(path: str, state: str, start_sha: str) -> None:
    if state == "??":
        target = Path(path)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    else:
        git("checkout", start_sha, "--", path)


def finish() -> int:
    number = int(os.environ["NUMBER"])
    ai, label, branch = os.environ["AI"], os.environ["LABEL"], os.environ["BRANCH"]
    is_pr = os.environ["IS_PR"] == "true"
    comment_id, start_sha = os.environ["COMMENT_ID"], os.environ.get("START_SHA", "")
    work_result, outcome = os.environ.get("WORK_RESULT", ""), os.environ.get("OUTCOME", "")
    elapsed = int(time.time()) - int(os.environ.get("STARTED") or time.time())
    reply_file = OUT / "reply.md"
    reply = reply_file.read_text(encoding="utf-8").strip() if reply_file.is_file() else ""

    if work_result != "success" or outcome != "success" or not reply or not start_sha:
        if work_result == "cancelled" or outcome == "cancelled":
            reason = "途中で止められました"
        elif outcome in ("", "skipped"):
            reason = "準備の途中で止まりました"
        elif outcome == "success" and not reply:
            reason = "終わりましたが、返事の文章がありませんでした"
        else:
            reason = "途中で止まりました"
        edit_comment(comment_id, f"⚠️ **{label}** の作業が{reason}。ファイルは保存していません（本番サイトは変わっていません）。"
                                 f"\n\n次にやること: もう一度 `@{ai}` を付けて頼んでください。続けて止まるときは管理者に連絡してください。"
                                 f"\n\n[作業の記録（管理者向け）]({run_url()})")
        return 0

    notes: list[str] = []
    git("checkout", "-q", "-B", branch, start_sha)

    # 変更のまとめのうち、site/ の中だけを取り込む（ほかのファイルは、AI が変えていても取り込まない）
    patch = OUT / "changes.patch"
    if patch.is_file() and patch.stat().st_size:
        listed = run("git", "apply", "--numstat", "-z", str(patch), check=False).stdout
        paths = [row.split("\t", 2)[2] for row in listed.split("\0") if row.count("\t") >= 2]
        outside = [p for p in paths if not p.startswith("site/")]
        applied = run("git", "apply", "--binary", "--include=site/*", str(patch), check=False)
        if applied.returncode != 0:
            git("checkout", "-q", "-f", "-B", branch, start_sha)
            run("git", "clean", "-qfd", "--", "site", check=False)
            notes.append("変更を取り込めなかったため、保存していません。"
                         f"もう一度 `@{ai}` を付けて頼んでください。続けて起きるときは管理者に連絡してください。")
        if outside:
            names = "、".join(f"`{p}`" for p in outside[:5]) + (" など" if len(outside) > 5 else "")
            notes.append(f"`site/` の外のファイル（{names}）が変わっていたため、それは取り込んでいません（保存していません）。")

    # 合いことば（API キーなど）が書き込まれていないか
    secrets = [v for v in (os.environ.get(k, "") for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GH_TOKEN")) if len(v) >= 8]
    for state, path in changed_paths():
        file = Path(path)
        if state == "D" or not file.is_file() or file.stat().st_size > 20_000_000:
            continue
        data = file.read_bytes()
        if any(s.encode() in data for s in secrets):
            undo(path, state, start_sha)
            notes.append(f"`{path}` に、書いてはいけない情報が含まれていたため、取り込んでいません（保存していません）。")

    markers = COMMIT_RE.findall(reply)
    reply = COMMIT_RE.sub("", reply).strip()
    changes = changed_paths()
    saved = False
    check_ok = True
    if changes:
        check = run(sys.executable, "tools/build.py", "--check", check=False)
        check_ok = check.returncode == 0
        message = (markers[-1].strip() if markers else "") or f"{label}による更新（#{number}）"
        git("add", "-A", "--", "site")
        run("git", "-c", f"user.name={label}（AI）", "-c", f"user.email={BOT_EMAIL}",
            "commit", "-q", "-m", f"{message}\n\n#{number} の依頼で {label} が作業しました。")
        token = os.environ["GH_TOKEN"]
        push = run("git", "push", "-q", f"https://x-access-token:{token}@github.com/{repo()}.git",
                   f"HEAD:refs/heads/{branch}", check=False)
        saved = push.returncode == 0
        if not saved:
            notes.append("下書きを保存できませんでした（ほかの保存と重なった可能性があります）。"
                         f"もう一度 `@{ai}` を付けて頼んでください。")
        elif check_ok:
            run("gh", "workflow", "run", "preview.yml", "--repo", repo(), "--ref", branch, check=False)
        else:
            errors = [line for line in (check.stdout + check.stderr).splitlines() if "エラー" in line or "ERROR" in line]
            notes.append("保存しましたが、仕組みのチェックでエラーが出たため、確認用ページは作っていません。"
                         f"`@{ai} チェックのエラーを直して` とコメントしてください。\n\n```\n"
                         + "\n".join(errors[:15] or (check.stdout + check.stderr).splitlines()[-15:]) + "\n```")
    elif markers:
        notes.append("ファイルの変更がなかったため、保存はしていません。")

    footer = ""
    if saved and not is_pr:
        title = markers[-1].strip() if markers else f"{label}による更新（#{number}）"
        query = urllib.parse.urlencode({"quick_pull": "1", "title": title,
                                        "body": f"#{number} の依頼で、{label} が作成しました。"})
        compare = f"https://github.com/{repo()}/compare/main...{urllib.parse.quote(branch, safe='/')}?{query}"
        footer = f"下書き: `{branch}`　・　[**Create PR ➔**]({compare})"
    elif saved and is_pr:
        footer = f"この公開申請の下書き `{branch}` を更新しました。"

    minutes, seconds = divmod(max(elapsed, 0), 60)
    body = [f"🤖 **{label}** が作業しました（{minutes}分{seconds}秒）・[作業の記録]({run_url()})", "", "---", "", reply]
    if notes:
        body += ["", *[f"> ⚠️ {n}" if "```" not in n else f"⚠️ {n}" for n in notes]]
    if footer:
        body += ["", "---", footer]
    edit_comment(comment_id, "\n".join(body))
    print(f"返事をコメントしました（保存: {'あり' if saved else 'なし'}、チェック: {'OK' if check_ok else 'エラー'}）")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if command == "start":
            return start()
        if command == "prepare":
            return prepare()
        if command == "package":
            return package()
        if command == "prompt":
            return prompt()
        if command == "finish":
            return finish()
    except Exception as exc:  # noqa: BLE001  止まったことを、待っている人に知らせる
        print(f"エラー: {exc}", file=sys.stderr)
        if os.environ.get("COMMENT_ID"):
            label = os.environ.get("LABEL", "AI")
            ai = os.environ.get("AI", "chatgpt")
            try:
                edit_comment(os.environ["COMMENT_ID"],
                             f"⚠️ **{label}** の作業の前後の処理で止まりました。ファイルは保存していないことがあります（本番サイトは変わっていません）。"
                             f"\n\n次にやること: もう一度 `@{ai}` を付けて頼んでください。続けて止まるときは管理者に連絡してください。"
                             f"\n\n[作業の記録（管理者向け）]({run_url()})")
            except Exception:  # noqa: BLE001
                pass
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
