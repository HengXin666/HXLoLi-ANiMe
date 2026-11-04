#!/usr/bin/env python3
"""HX-ANiMe tag 标注流 (路4): 标注 PR 的开启 / 勾选与评论指令解析 / 合入 / 一个月自动关闭.

依据 hx-anime-tag-refactor-design.md 第 5.2, 6, 7, 8 节; bgm 同步见同目录 label-sync-bgm.py (第 9 节).
由 .github/workflows/anime-label-flow.yaml 调用, 只用 Python 标准库 + GitHub REST API (GITHUB_TOKEN).

子命令:
  open       读 events.json (data 流合入后的事件清单), 每个事件开 (或更新/升级) 一个标注 PR
  handle     处理标注 PR 上的勾选 (PR 正文 edited) 或评论指令, 写回草稿, 完成即合入
  post-merge 人工在网页上直接合入标注 PR 时补齐 labeledAt/by 与 label
  expire     每日扫描 flow:label + state:等待标注, 满一个月关闭, 第 25 天提醒一次

防回环: 本脚本只写 data/labels/<id>.json; data 流永远不碰 data/labels/, 主站只读 data/labels/.
所有写操作都用 GITHUB_TOKEN, GitHub 不会因 GITHUB_TOKEN 的提交/编辑再触发工作流, 因此 bot 改正文不会自触发.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── 环境 ────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("GITHUB_TOKEN", "")
API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BASE_BRANCH = os.environ.get("LABEL_BASE_BRANCH", "main")
CDN_TAG = os.environ.get("ANIME_CDN_TAG", "")
CDN_BASE = os.environ.get("ANIME_CDN_BASE", "https://cdn.jsdmirror.com/gh")
CDN_REPO = os.environ.get("ANIME_CDN_REPO", REPO)
EXPIRE_DAYS = int(os.environ.get("LABEL_EXPIRE_DAYS", "30"))
REMIND_DAYS = int(os.environ.get("LABEL_REMIND_DAYS", "25"))
REPORT = os.environ.get("LABEL_REPORT", "")
VOCAB_PATH = os.environ.get("LABEL_VOCAB", "data/labelVocab.json")
RECORD_PATH = os.environ.get("ANIME_RECORD", "data/ANiMeRecord.json")
LABELS_DIR = "data/labels"

LABEL_FLOW = "flow:label"
STATE_WAIT = "state:等待标注"
STATE_DONE = "state:已标注"
STATE_EXPIRED = "state:expired"
MODE_NEW = "mode:new"
MODE_REVIEW = "mode:review"


def out(k: str, v: str) -> None:
    """写 GITHUB_OUTPUT, 让 workflow 的 if: steps.*.outputs.* 能判断"""
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{k}={v}\n")


def report(obj: Any) -> None:
    if REPORT:
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def api(method: str, path: str, body: Any = None, headers: Optional[dict] = None) -> Any:
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    h = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hx-anime-label-flow",
    }
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            if e.code in (403, 429, 502, 503) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"GitHub API {method} {path} -> {e.code}: {detail}")
    return {}


def paged(path: str) -> List[dict]:
    items, page = [], 1
    while True:
        chunk = api("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return items


def load_vocab() -> Tuple[List[dict], List[dict], List[str]]:
    """返回 (susume, genre, 全部合法 display 名). 唯一词表来源 data/labelVocab.json."""
    with open(VOCAB_PATH, encoding="utf-8") as f:
        v = json.load(f)
    susume = v.get("susume", [])
    genre = v.get("genre", [])
    names = [x["display"] for x in susume] + [x["display"] for x in genre]
    return susume, genre, names


def bgm_name(vocab_all: List[dict], display: str) -> str:
    for x in vocab_all:
        if x["display"] == display:
            return x.get("bgmTag", display)
    return display


def load_records() -> Dict[int, dict]:
    """id -> 番剧记录 (取名字 / 封面用). 文件很大, 只读一次建索引."""
    try:
        with open(RECORD_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    rows = data if isinstance(data, list) else data.get("items", data.get("animes", []))
    idx = {}
    for r in rows if isinstance(rows, list) else []:
        # ANiMeRecord.json 的 id 在 anime_data.id (与 events.py 的 index_records 同口径)
        anime_data = r.get("anime_data") or {}
        i = (anime_data.get("id") if isinstance(anime_data, dict) else None) \
            or r.get("id") or r.get("bgm_id") or r.get("subject_id")
        if i is not None:
            try:
                idx[int(i)] = r
            except (TypeError, ValueError):
                pass
    return idx


def title_of(rec: dict, sid: int) -> str:
    if not rec:
        return f"番剧 {sid}"
    ad = rec.get("anime_data") or {}
    if not isinstance(ad, dict):
        ad = {}
    return (ad.get("name_cn") or ad.get("name") or rec.get("name_cn") or rec.get("name")
            or rec.get("title") or rec.get("name_jp") or f"番剧 {sid}")


def poster_url(sid: int) -> str:
    tag = CDN_TAG or "main"
    repo = CDN_REPO or "HengXin666/HXLoLi-ANiMe"
    return f"{CDN_BASE}/{repo}@{tag}/data/anime/{sid}.jpg"


def label_for(labels: List[str]) -> None:
    """确保 label 存在 (颜色固定, 已存在则忽略 422)."""
    colors = {
        LABEL_FLOW: "5319e7", STATE_WAIT: "fbca04", STATE_DONE: "0e8a16",
        STATE_EXPIRED: "b60205", MODE_NEW: "1d76db", MODE_REVIEW: "f9d0c4",
        "state:已合入": "0e8a16", "bgm:synced": "0e8a16", "bgm:sync-failed": "b60205",
    }
    for name in labels:
        try:
            api("POST", f"/repos/{REPO}/labels",
                {"name": name, "color": colors.get(name, "ededed")})
        except RuntimeError as e:
            if "422" not in str(e):
                print(f"  label {name}: {e}", file=sys.stderr)


# ── PR 正文模板 ─────────────────────────────────────────────────────────
def checkbox(item: str) -> str:
    return f"- [ ] {item}"


def build_body(sid: int, rec: dict, mode: str, vocab: Tuple[List[dict], List[dict], List[str]]) -> str:
    susume, genre, _ = vocab
    name = title_of(rec, sid)
    is_review = mode == "review"
    head = "[观后感]" if is_review else "[标注]"
    lines = [
        f'<img src="{poster_url(sid)}" width="180" align="right" alt="封面">',
        "",
        f"## {head} 《{name}》",
        "",
        f"- bgm 条目: https://bgm.tv/subject/{sid}",
        f"- 触发: {'状态变化 (在看 → 看过/搁置/抛弃), 邀请评分与标注' if is_review else '新增番剧'}",
        "- 勾完点最下方【标注完成, 合入】, 或评论 /done",
        "",
    ]
    # 推荐分类: 新增模式不主动展示 物凄い (没看完不谈最爱)
    lines += ["**推荐分类 (专属页)**", ""]
    for s in susume:
        if not is_review and s["display"] == "物凄い":
            continue
        lines.append(checkbox(f"{s['display']} (进 /anime/susume/{s.get('slugPage', '')})"))
    lines += ["", "**标注**", ""]
    # 九项一行三到四个, 仍是独立勾选框
    row = []
    for g in genre:
        row.append(checkbox(g["display"]))
    lines += row
    lines += [""]
    if is_review:
        lines += ["**评分 (观后感)**", "",
                  "- [ ] 1 - [ ] 2 - [ ] 3 - [ ] 4 - [ ] 5 - [ ] 6 - [ ] 7 - [ ] 8 - [ ] 9 - [ ] 10",
                  "", "**一句话观后感** (可选, 评论 /review 内容)", ""]
    lines += [
        checkbox("同步到 bgm (默认勾上)"),
        checkbox("✅ 标注完成, 合入"),
        checkbox("🚫 跳过这部, 关闭"),
        "",
        "---",
        "指令: /tag 词 /untag 词 /rate 1-10 /review 文本 /done /skip /nobgm /resync",
    ]
    return "\n".join(lines)


BATCH_SIZE = 5
SID_MARK = re.compile(r"<!--\s*sid:(\d+)\s*-->")


def build_batch_body(ids: List[int], records: dict, vocab: Tuple[List[dict], List[dict], List[str]]) -> str:
    """超过 BATCH_SIZE 部新增番剧合并为一个批量标注 PR, 每部一节, 每节独立勾选.

    每节用 <!-- sid:<id> --> 标记, handle 解析时按节归集标签, 支持部分完成后合入已完成部分.
    """
    susume, genre, _ = vocab
    lines = [
        f"## 批量标注 ({len(ids)} 部新增番剧)",
        "",
        "每部一节, 勾完各自节内的标签即可; 最下方的完成勾选对全部节生效.",
        "只想处理某几部时, 其余节留空即可, 合入时只写有标注的文件.",
        "",
    ]
    for sid in ids:
        name = title_of(records.get(sid, {}), sid)
        lines += [
            f"<!-- sid:{sid} -->",
            f'<img src="{poster_url(sid)}" width="120" align="right" alt="封面">',
            "",
            f"### 《{name}》 · bgm {sid}",
            "",
            "**推荐分类**",
            "",
        ]
        for s in susume:
            if s["display"] == "物凄い":
                continue  # 新增模式不主动展示物凄い
            lines.append(checkbox(f"{s['display']} (/anime/susume/{s.get('slugPage', '')})"))
        lines += ["", "**标注**", ""]
        for g in genre:
            lines.append(checkbox(g["display"]))
        lines += ["", "---", ""]
    lines += [
        checkbox("同步到 bgm (默认勾上)"),
        checkbox("✅ 标注完成, 合入 (只写有标注的节)"),
        checkbox("🚫 跳过整批, 关闭"),
        "",
        "指令: /tag 词 /untag 词 /done /skip /nobgm (批量 PR 上 /tag 作用于全部节, 逐部请用勾选框)",
    ]
    return "\n".join(lines)


def open_batch(ids: List[int], records: dict, vocab: Tuple[List[dict], List[dict], List[str]]) -> dict:
    branch = f"label/batch-{ids[0]}"
    ensure_branch(branch)
    for sid in ids:
        draft = {"id": sid, "tags": [], "rate": None, "review": "", "mode": "new",
                 "by": "", "labeledAt": "", "bgmSynced": False}
        put_file(branch, f"{LABELS_DIR}/{sid}.json",
                 json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                 f"[标注] 批量初始化 {sid} 草稿")
    body = build_batch_body(ids, records, vocab)
    labels = [LABEL_FLOW, STATE_WAIT, MODE_NEW]
    pr = api("POST", f"/repos/{REPO}/pulls",
             {"title": f"[批量标注] {len(ids)} 部新增番剧", "head": branch, "base": BASE_BRANCH,
              "body": body, "labels": labels, "maintainer_can_modify": True})
    return {"ids": ids, "pr": pr.get("number"), "action": "opened-batch"}


def find_open_pr(sid: int) -> Optional[dict]:
    for pr in paged(f"/repos/{REPO}/pulls?state=open"):
        if any(l["name"] == LABEL_FLOW for l in pr.get("labels", [])):
            if pr.get("head", {}).get("ref", "").endswith(f"/{sid}") or f"#{sid}" in (pr.get("title") or ""):
                return pr
    return None


def ensure_branch(branch: str) -> None:
    try:
        api("GET", f"/repos/{REPO}/git/ref/heads/{branch}")
        return
    except RuntimeError:
        pass
    main = api("GET", f"/repos/{REPO}/git/ref/heads/{BASE_BRANCH}")
    sha = main["object"]["sha"]
    api("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})


def put_file(branch: str, path: str, content: str, message: str) -> None:
    try:
        cur = api("GET", f"/repos/{REPO}/contents/{path}?ref={branch}")
        sha = cur.get("sha")
    except RuntimeError:
        sha = None
    import base64
    body: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    api("PUT", f"/repos/{REPO}/contents/{path}", body)


# ── open ────────────────────────────────────────────────────────────────
# Agent Notes: 标注流 open 子命令按 events.json 开 (或更新/升级) 标注 PR, 一部番同时最多一个.
# .agents/notes/proposed/feature/2026-11-04-hx-anime-label-pipeline.md
def cmd_open(args: argparse.Namespace) -> None:
    vocab = load_vocab()
    records = load_records()
    events: Dict[str, Any] = {"new": [], "review": []}
    for p in (args.events or []):
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            events["new"] += [int(x) for x in d.get("new", [])]
            events["review"] += d.get("review", [])
    if args.ids:
        events["new"] += [int(x) for x in args.ids.split()]
    mode = args.mode or "new"
    # 去重
    seen, new_ids = set(), []
    for i in events["new"]:
        if i not in seen:
            seen.add(i)
            new_ids.append(i)
    review_by_id = {int(r["id"]): r for r in events["review"]}

    label_for([LABEL_FLOW, STATE_WAIT, STATE_DONE, STATE_EXPIRED, MODE_NEW, MODE_REVIEW])
    opened = []
    # 观后感事件优先按 review 模式处理
    todo: List[Tuple[int, str]] = []
    for sid in new_ids:
        todo.append((sid, "review" if sid in review_by_id else mode))
    for sid in review_by_id:
        if sid not in new_ids:
            todo.append((sid, "review"))

    # 超过 BATCH_SIZE 部的纯新增 (非观后感) 合并为一个批量标注 PR, 压低 PR 数量.
    plain = [sid for sid, m in todo if m != "review" and not find_open_pr(sid)]
    if len(plain) > BATCH_SIZE:
        batch_set = set(plain)
        opened.append(open_batch(plain, records, vocab))
        todo = [(sid, m) for sid, m in todo if sid not in batch_set]

    for sid, m in todo:
        branch = f"label/{sid}"
        existing = find_open_pr(sid)
        if existing:
            # 升级为观后感: 改标题 / 换 label / 重写正文 (不另开)
            if m == "review":
                body = build_body(sid, records.get(sid, {}), "review", vocab)
                labels = [l["name"] for l in existing.get("labels", [])]
                labels = [l for l in labels if l not in (MODE_NEW, MODE_REVIEW)] + [MODE_REVIEW]
                api("PATCH", f"/repos/{REPO}/pulls/{existing['number']}",
                    {"title": f"[观后感] 《{title_of(records.get(sid, {}), sid)}》",
                     "body": body, "labels": labels})
                opened.append({"id": sid, "pr": existing["number"], "action": "upgraded"})
            else:
                opened.append({"id": sid, "pr": existing["number"], "action": "already-open"})
            continue
        ensure_branch(branch)
        draft = {
            "id": sid, "tags": [], "rate": None, "review": "",
            "mode": m, "by": "", "labeledAt": "", "bgmSynced": False,
        }
        put_file(branch, f"{LABELS_DIR}/{sid}.json",
                 json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                 f"[标注] 初始化 {sid} 草稿")
        body = build_body(sid, records.get(sid, {}), m, vocab)
        title = ("[观后感]" if m == "review" else "[标注]") + f" 《{title_of(records.get(sid, {}), sid)}》"
        labels = [LABEL_FLOW, STATE_WAIT, MODE_REVIEW if m == "review" else MODE_NEW]
        pr = api("POST", f"/repos/{REPO}/pulls",
                 {"title": title, "head": branch, "base": BASE_BRANCH,
                  "body": body, "labels": labels, "maintainer_can_modify": True})
        opened.append({"id": sid, "pr": pr.get("number"), "action": "opened"})
    report({"opened": opened})
    print(f"open: {len(opened)} PR (events: new={len(new_ids)} review={len(review_by_id)})")


# ── 解析勾选 / 指令 ─────────────────────────────────────────────────────
CHECKED = re.compile(r"^- \[[xX]\]\s*(.+?)\s*$", re.M)


def parse_checked(body: str) -> List[str]:
    return [m.group(1).strip() for m in CHECKED.finditer(body or "")]


def parse_body(body: str, vocab: Tuple[List[dict], List[dict], List[str]]) -> Dict[str, Any]:
    _, _, names = vocab
    checked = parse_checked(body)
    tags, rate, sync_bgm, done, skip, nobgm = [], None, True, False, False, False
    for c in checked:
        c_plain = re.sub(r"\s*\(.*?\)\s*$", "", c).strip()
        if c_plain in names:
            if c_plain not in tags:
                tags.append(c_plain)
        elif re.fullmatch(r"\d{1,2}", c_plain) and 1 <= int(c_plain) <= 10:
            rate = int(c_plain)
        elif "标注完成" in c:
            done = True
        elif "跳过" in c:
            skip = True
        elif "同步到 bgm" in c:
            sync_bgm = True
    # 勾了跳过优先于完成
    if skip:
        done = False
    result = {"tags": tags, "rate": rate, "sync_bgm": sync_bgm, "done": done, "skip": skip}

    # 批量 PR: 按 <!-- sid:N --> 分节, 每节独立归集标签, 支持部分完成.
    marks = list(SID_MARK.finditer(body or ""))
    if marks:
        per_id: Dict[int, List[str]] = {}
        for i, mk in enumerate(marks):
            sid = int(mk.group(1))
            seg = body[mk.end(): marks[i + 1].start() if i + 1 < len(marks) else len(body)]
            seg_tags = []
            for c in parse_checked(seg):
                c_plain = re.sub(r"\s*\(.*?\)\s*$", "", c).strip()
                if c_plain in names and c_plain not in seg_tags:
                    seg_tags.append(c_plain)
            per_id[sid] = seg_tags
        result["batch"] = True
        result["per_id"] = per_id
        # 批量 PR 的全局 tags 只用于置顶预览, 不写任何单部草稿
    else:
        result["batch"] = False
        result["per_id"] = {}
    return result


def apply_comment(text: str, state: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
    """评论快捷指令, 返回累积后的状态; 不合法词回复提示交给调用方."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("/"):
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/tag":
            for w in arg.split():
                if w in names and w not in state["tags"]:
                    state["tags"].append(w)
        elif cmd == "/untag":
            state["tags"] = [t for t in state["tags"] if t != arg.strip()]
        elif cmd == "/rate" and arg.isdigit() and 1 <= int(arg) <= 10:
            state["rate"] = int(arg)
        elif cmd == "/review":
            state["review"] = arg
        elif cmd == "/nobgm":
            state["sync_bgm"] = False
        elif cmd == "/done":
            state["done"] = True
        elif cmd == "/skip":
            state["skip"] = True
            state["done"] = False
        elif cmd == "/resync":
            state["resync"] = True
    return state


def sticky_comment(pr: int, state: Dict[str, Any]) -> None:
    """编辑同一条置顶"当前结果"评论, 不刷屏."""
    marker = "<!-- hx-anime-label-result -->"
    preview = (f"{marker}\n**当前标注结果**\n\n- 标签: {', '.join(state['tags']) or '(无)'}\n"
               f"- 评分: {state['rate'] if state['rate'] is not None else '(未评)'}\n"
               f"- 观后感: {state.get('review') or '(无)'}\n"
               f"- 同步 bgm: {'是' if state['sync_bgm'] else '否'}\n")
    for c in paged(f"/repos/{REPO}/issues/{pr}/comments"):
        if marker in (c.get("body") or ""):
            api("PATCH", f"/repos/{REPO}/issues/comments/{c['id']}", {"body": preview})
            return
    api("POST", f"/repos/{REPO}/issues/{pr}/comments", {"body": preview})


def read_draft(pr: dict) -> Tuple[dict, str]:
    sid = int(pr["head"]["ref"].split("/")[-1])
    path = f"{LABELS_DIR}/{sid}.json"
    cur = api("GET", f"/repos/{REPO}/contents/{path}?ref={pr['head']['ref']}")
    import base64
    content = base64.b64decode(cur["content"]).decode()
    return json.loads(content), path


def write_draft(pr: dict, path: str, data: dict, msg: str) -> None:
    import base64
    cur = api("GET", f"/repos/{REPO}/contents/{path}?ref={pr['head']['ref']}")
    api("PUT", f"/repos/{REPO}/contents/{path}", {
        "message": msg, "branch": pr["head"]["ref"], "sha": cur["sha"],
        "content": base64.b64encode((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
    })


def merge_pr(pr: dict, state: dict) -> None:
    labels = [l["name"] for l in pr.get("labels", [])]
    labels = [l for l in labels if l != STATE_WAIT] + [STATE_DONE]
    # susume 结果 label
    for t in state["tags"]:
        lab = f"susume:{t}"
        label_for([lab])
        labels.append(lab)
    api("PATCH", f"/repos/{REPO}/issues/{pr['number']}", {"labels": labels, "state": "open"})
    api("PUT", f"/repos/{REPO}/pulls/{pr['number']}/merge",
        {"merge_method": "squash", "commit_title": f"[标注] {pr['title']} (#{pr['number']})"})


def cmd_handle(args: argparse.Namespace) -> None:
    vocab = load_vocab()
    _, _, names = vocab
    pr = api("GET", f"/repos/{REPO}/pulls/{args.pr}")
    body_state = parse_body(pr.get("body") or "", vocab)
    # 叠加最新一条评论指令 (只取本次触发的评论, 由调用方经 env 传; 缺省扫全部)
    state = dict(body_state)
    state.setdefault("review", "")
    state.setdefault("resync", False)
    comment = os.environ.get("LABEL_COMMENT_BODY", "")
    state = apply_comment(comment, state, names)
    # review 文本也可能来自 /review
    sticky_comment(args.pr, state)
    out("pr", str(args.pr))

    if state["skip"]:
        api("PATCH", f"/repos/{REPO}/pulls/{args.pr}", {"state": "closed"})
        api("POST", f"/repos/{REPO}/issues/{args.pr}/labels", {"labels": [STATE_EXPIRED]})
        report({"pr": args.pr, "action": "skipped"})
        print(f"handle: PR #{args.pr} skipped and closed")
        return

    # 批量标注 PR: 按 <!-- sid:N --> 分节逐部写草稿, 只写有标注的节, 支持部分完成.
    if state.get("batch"):
        per_id = state.get("per_id") or {}
        written = []
        for sid, tags in per_id.items():
            if not tags:
                continue  # 这节没勾, 不写文件, 主站仍当作未标注
            path = f"{LABELS_DIR}/{sid}.json"
            draft = {"id": sid, "tags": tags, "rate": None, "review": "", "mode": "new",
                     "by": f"pr#{args.pr}", "labeledAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                     "bgmSynced": False, "sync_bgm": state["sync_bgm"]}
            write_draft(pr, path, draft, f"[批量标注] 更新 {sid} (tags={','.join(tags)})")
            written.append(sid)
        if state["done"] and written:
            merge_pr(pr, {"tags": [t for tags in per_id.values() for t in tags]})
            out("merged", "true")
            report({"pr": args.pr, "action": "merged-batch", "written": written})
            print(f"handle: batch PR #{args.pr} merged, {len(written)} labels written")
        else:
            report({"pr": args.pr, "action": "updated-batch", "written": written})
            print(f"handle: batch PR #{args.pr} draft updated, {len(written)} sections")
        return

    draft, path = read_draft(pr)
    sid = draft.get("id") or int(pr["head"]["ref"].split("/")[-1])
    draft["tags"] = state["tags"]
    draft["rate"] = state["rate"]
    draft["review"] = state.get("review", "")
    draft["mode"] = "review" if MODE_REVIEW in [l["name"] for l in pr.get("labels", [])] else "new"
    if state["done"]:
        draft["by"] = f"pr#{args.pr}"
        draft["labeledAt"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        draft["bgmSynced"] = False
    write_draft(pr, path, draft, f"[标注] 更新 {sid} (tags={','.join(state['tags']) or '无'})")

    if state["done"]:
        merge_pr(pr, state)
        out("merged", "true")
        report({"pr": args.pr, "action": "merged", "tags": state["tags"], "rate": state["rate"]})
        print(f"handle: PR #{args.pr} merged")
    elif state.get("resync"):
        out("resync", "true")
        report({"pr": args.pr, "action": "resync"})
    else:
        report({"pr": args.pr, "action": "updated", "tags": state["tags"], "rate": state["rate"]})
        print(f"handle: PR #{args.pr} draft updated")


def cmd_post_merge(args: argparse.Namespace) -> None:
    pr = api("GET", f"/repos/{REPO}/pulls/{args.pr}")
    labels = [l["name"] for l in pr.get("labels", [])]
    labels = [l for l in labels if l != STATE_WAIT] + [STATE_DONE]
    api("PATCH", f"/repos/{REPO}/issues/{args.pr}", {"labels": labels})
    # 补齐草稿的 by / labeledAt (合入后文件已在 main, 用 main 上的内容)
    try:
        sid = int(pr["head"]["ref"].split("/")[-1])
        path = f"{LABELS_DIR}/{sid}.json"
        cur = api("GET", f"/repos/{REPO}/contents/{path}?ref={BASE_BRANCH}")
        import base64
        d = json.loads(base64.b64decode(cur["content"]).decode())
        if not d.get("by"):
            d["by"] = f"pr#{args.pr}"
            d["labeledAt"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
            write_draft({"head": {"ref": BASE_BRANCH}}, path, d, f"[标注] 补齐 {sid}")
    except Exception as e:  # noqa: BLE001 - 兜底, 不阻断 bgm 同步
        print(f"post-merge 补齐跳过: {e}", file=sys.stderr)
    out("merged", "true")
    report({"pr": args.pr, "action": "post-merge"})
    print(f"post-merge: PR #{args.pr} normalized")


# ── expire ──────────────────────────────────────────────────────────────
def cmd_expire(_args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    remind_at = timedelta(days=REMIND_DAYS)
    expire_at = timedelta(days=EXPIRE_DAYS)
    result = []
    for pr in paged(f"/repos/{REPO}/pulls?state=open"):
        labels = [l["name"] for l in pr.get("labels", [])]
        if LABEL_FLOW not in labels or STATE_WAIT not in labels:
            continue
        created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
        last = created
        for c in paged(f"/repos/{REPO}/issues/{pr['number']}/comments"):
            if c.get("user", {}).get("type") != "Bot":
                t = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                if t > last:
                    last = t
        age = now - last
        if age >= expire_at:
            api("POST", f"/repos/{REPO}/issues/{pr['number']}/comments",
                {"body": "一个月没有标注, 先关掉啦. 评论 /reopen 或在 issue 里重新发起即可."})
            api("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"state": "closed"})
            api("POST", f"/repos/{REPO}/issues/{pr['number']}/labels", {"labels": [STATE_EXPIRED]})
            try:
                api("DELETE", f"/repos/{REPO}/git/refs/heads/{pr['head']['ref']}")
            except RuntimeError:
                pass
            result.append({"pr": pr["number"], "action": "expired", "days": age.days})
        elif age >= remind_at and not any(
            "第 25 天" in (c.get("body") or "") for c in paged(f"/repos/{REPO}/issues/{pr['number']}/comments")
        ):
            api("POST", f"/repos/{REPO}/issues/{pr['number']}/comments",
                {"body": "第 25 天提醒: 这个标注 PR 还有 5 天就要自动关闭啦, 有空勾一下."})
            result.append({"pr": pr["number"], "action": "reminded", "days": age.days})
    report({"expire": result})
    print(f"expire: {len(result)} PR handled")


def cmd_issue(args: argparse.Namespace) -> None:
    """issue 表单备用入口: 读 issue body, 反查 bgm id, 代开标注 PR 并 Closes #n, 保证单一落点."""
    vocab = load_vocab()
    records = load_records()
    issue = api("GET", f"/repos/{REPO}/issues/{args.issue}")
    body = issue.get("body") or ""

    # bgm id: 表单的 subject_id 字段, 或按番剧名在记录里反查
    sid = None
    m = re.search(r"###\s*bgm subject id\s*\n+(\d+)", body)
    if m:
        sid = int(m.group(1))
    if sid is None:
        m = re.search(r"###\s*番剧名[^\n]*\n+([^\n]+)", body)
        name = m.group(1).strip() if m else ""
        if name and name not in ("_No response_", ""):
            for i, r in records.items():
                if name in title_of(r, i):
                    sid = i
                    break
    if sid is None:
        api("POST", f"/repos/{REPO}/issues/{args.issue}/comments",
            {"body": "没解析到 bgm subject id 或番剧名, 请补一项后重新提交 (或在评论里给 /tag 指令). 本 issue 暂不代开 PR."})
        report({"issue": args.issue, "action": "unresolved"})
        return

    mode = "review" if "review" in body.lower() or "观后感" in body else "new"
    existing = find_open_pr(sid)
    if existing:
        api("POST", f"/repos/{REPO}/issues/{args.issue}/comments",
            {"body": f"这部番已有一个开着的标注 PR #{existing['number']}, 请直接去那边勾选, 本 issue 关闭."})
        api("PATCH", f"/repos/{REPO}/issues/{args.issue}", {"state": "closed"})
        report({"issue": args.issue, "action": "already-open", "pr": existing["number"]})
        return

    ensure_branch(f"label/{sid}")
    draft = {"id": sid, "tags": [], "rate": None, "review": "", "mode": mode,
             "by": "", "labeledAt": "", "bgmSynced": False}
    put_file(f"label/{sid}", f"{LABELS_DIR}/{sid}.json",
             json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
             f"[标注] issue #{args.issue} 代开草稿 {sid}")
    pr_body = build_body(sid, records.get(sid, {}), mode, vocab) + f"\n\nCloses #{args.issue}\n"
    labels = [LABEL_FLOW, STATE_WAIT, MODE_REVIEW if mode == "review" else MODE_NEW]
    pr = api("POST", f"/repos/{REPO}/pulls",
             {"title": ("[观后感]" if mode == "review" else "[标注]") + f" 《{title_of(records.get(sid, {}), sid)}》",
              "head": f"label/{sid}", "base": BASE_BRANCH, "body": pr_body,
              "labels": labels, "maintainer_can_modify": True})
    api("POST", f"/repos/{REPO}/issues/{args.issue}/comments",
        {"body": f"已代开标注 PR #{pr.get('number')}, 请去 PR 里勾选. 合入时本 issue 自动关闭."})
    report({"issue": args.issue, "action": "pr-opened", "pr": pr.get("number"), "id": sid})
    print(f"issue #{args.issue} -> PR #{pr.get('number')} (id={sid}, mode={mode})")


def cmd_backfill(args: argparse.Namespace) -> None:
    """全量回填: 列出所有还没有标注文件的番, 合并成批量标注 PR 供人工逐部勾选.

    清空旧 tag 后 data/labels/ 是空的, 这个入口负责把存量番剧一次性摊开给人标.
    """
    vocab = load_vocab()
    records = load_records()
    # 已标注 = data/labels/ 下有文件; 排除已开着标注 PR 的番
    labeled = set()
    if os.path.isdir(LABELS_DIR):
        for fn in os.listdir(LABELS_DIR):
            if fn.endswith(".json"):
                try:
                    labeled.add(int(fn[:-5]))
                except ValueError:
                    pass
    open_pr_ids = set()
    for pr in paged(f"/repos/{REPO}/pulls?state=open"):
        if any(l["name"] == LABEL_FLOW for l in pr.get("labels", [])):
            ref = pr.get("head", {}).get("ref", "")
            for part in ref.split("/")[-1].replace("batch-", "").split(","):
                if part.isdigit():
                    open_pr_ids.add(int(part))
    pending = sorted(i for i in records if i not in labeled and i not in open_pr_ids)
    if not pending:
        report({"action": "backfill", "pending": 0})
        print(f"backfill: 0 部待标注 (共 {len(records)} 部, 已标注 {len(labeled)} 部)")
        return
    # 按 BATCH_SIZE 切成多个批量 PR, 每个 PR 正文每部一节独立勾选
    label_for([LABEL_FLOW, STATE_WAIT, MODE_NEW])
    results = []
    for start in range(0, len(pending), args.chunk):
        chunk = pending[start:start + args.chunk]
        results.append(open_batch(chunk, records, vocab))
    report({"action": "backfill", "pending": len(pending), "prs": results})
    print(f"backfill: {len(pending)} 部待标注 -> {len(results)} 个批量标注 PR (共 {len(records)} 部)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("--events", action="append", default=[])
    o.add_argument("--ids", default="")
    o.add_argument("--mode", default="new", choices=["new", "review"])
    h = sub.add_parser("handle")
    h.add_argument("--pr", required=True, type=int)
    pm = sub.add_parser("post-merge")
    pm.add_argument("--pr", required=True, type=int)
    sub.add_parser("expire")
    iss = sub.add_parser("issue")
    iss.add_argument("--issue", required=True, type=int)
    bf = sub.add_parser("backfill")
    bf.add_argument("--chunk", type=int, default=20, help="每个批量 PR 最多列几部 (默认 20)")
    args = ap.parse_args()
    {"open": cmd_open, "handle": cmd_handle,
     "post-merge": cmd_post_merge, "expire": cmd_expire, "issue": cmd_issue,
     "backfill": cmd_backfill}[args.cmd](args)


if __name__ == "__main__":
    main()
