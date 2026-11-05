#!/usr/bin/env python3
"""HX-ANiMe tag 标注流 (路4): 标注 PR 的开启 / 勾选与评论指令解析 / 合入 / 一个月自动关闭.

依据 hx-anime-tag-refactor-design.md 第 5.2, 6, 7, 8 节; bgm 同步见同目录 label-sync-bgm.py (第 9 节).
由 .github/workflows/anime-label-flow.yaml 调用, 只用 Python 标准库 + GitHub REST API (GITHUB_TOKEN).

子命令:
  open       读 events.json (data 流合入后的事件清单), 每个事件开 (或更新/升级) 一个标注 PR
  handle     PR 被打上 label `state:待合入` (labeled) 或评论 /done 时跑一次: 解析正文勾选 + 评论指令,
             写回草稿并合入. 勾选/编辑正文本身不触发 (workflow 已去掉 edited), 完成信号只看 label / /done
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
# 最终确认: 勾好标签后由人给 PR 打这个 label, workflow 只在 labeled 该 label 时跑一次 handle.
# done 的唯一正文外来源 (另一来源是评论 /done); 正文里的「✅ 标注完成」旧勾选框不再作为依据.
STATE_CONFIRM = "state:待合入"
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


def create_labeled_pr(payload: dict, labels: List[str]) -> dict:
    """开 PR 并打 label. GitHub 的 POST /pulls 不接受 labels 字段 (会静默忽略),
    必须创建后用 POST /issues/{n}/labels 单独加, 否则 PR 没有 tag 区分.
    """
    clean = {k: v for k, v in payload.items() if k != "labels"}
    pr = api("POST", f"/repos/{REPO}/pulls", clean)
    num = pr.get("number")
    if num and labels:
        # 最终确认 label 也一并确保存在 (只创建, 不打到 PR 上); 否则用户想「打 label 才合入」时
        # 仓库里根本没有这个 label, gh pr edit --add-label 会报 not found. 不能把它放进 labels 参数,
        # 那会给 PR 直接打上确认 label 并触发一次 labeled handle.
        label_for(list(labels) + [STATE_CONFIRM])
        api("POST", f"/repos/{REPO}/issues/{num}/labels", {"labels": labels})
    return pr


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
        STATE_CONFIRM: "d93f0b",
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
# 正文不再生成「✅ 标注完成, 合入」勾选框: 完成信号只来自 label STATE_CONFIRM (或评论 /done).
CONFIRM_HINT = "勾好标签后, 给本 PR 加上 label `state:待合入` 才会合入并同步 bgm; 加 label 之前的任意勾选/编辑都不会触发工作流."


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
        "- 勾好标签后, 给本 PR 加上 label `state:待合入` 才会合入并同步 bgm; 加 label 之前的任意勾选/编辑都不会触发工作流.",
        "- 备用: 评论 /done 也会合入一次; 要放弃这部请勾「跳过」再加 label, 或直接评论 /skip.",
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
        checkbox("🚫 跳过这部, 关闭 (勾上后同样要加 label `state:待合入` 或评论 /skip 才生效)"),
        "",
        f"> **{CONFIRM_HINT}**",
        "",
        "---",
        "指令: /tag 词 /untag 词 /rate 1-10 /review 文本 /done /skip /nobgm /resync",
    ]
    return "\n".join(lines)


BATCH_SIZE = 5
MERGE_WAIT_S = int(os.environ.get("LABEL_MERGE_WAIT", "60"))
MERGE_RETRIES = 4
# <!-- sid:N --> 单部节; 系列节内每部带 <!-- sid:N series:K --> (K = 系列 key = 组内最小 id).
# group(1) 恒为 sid, pr_sids / 旧正文解析不受影响; group(2) 为系列 key (单部节为 None).
SID_MARK = re.compile(r"<!--\s*sid:(\d+)(?:\s+series:(\d+))?\s*-->")

# 系列判定: 与主站 HXLoLi/src/components/anime/AnimeGraphPage/buildGraph.ts 的 SERIES_RELATIONS /
# buildSeriesIndex 同口径 (relations 关系属于白名单, 且对方 id 也在库里, 并查集连通分量).
# 标注流只用 relations 严格判定, 不引入主站展示层的名字前缀兜底, 避免把不同作品误连成一个系列.
SERIES_RELATIONS = frozenset({"前传", "续集", "番外篇", "总集篇", "主线故事"})


def series_groups(records: Dict[int, dict]) -> List[List[int]]:
    """把 records 的 id 按系列分组 (并查集连通分量), 单部也保留成单元素列表.

    组内 id 升序, 系列 key = 组内最小 id, 组间按 key 升序, 保证每次运行结果稳定.
    """
    parent: Dict[int, int] = {i: i for i in records}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # 根取较小 id, 与 "系列 key = 组内最小 id" 一致
            parent[max(ra, rb)] = min(ra, rb)

    for sid, rec in records.items():
        ad = rec.get("anime_data") if isinstance(rec, dict) else None
        rels = (ad or {}).get("relations") if isinstance(ad, dict) else None
        for rel in rels or []:
            if not isinstance(rel, dict) or rel.get("relation") not in SERIES_RELATIONS:
                continue
            try:
                other = int(rel.get("id"))
            except (TypeError, ValueError):
                continue
            if other != sid and other in parent:
                union(sid, other)
    groups: Dict[int, List[int]] = {}
    for sid in records:
        groups.setdefault(find(sid), []).append(sid)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def pack_series(groups: List[List[int]], chunk: int) -> List[List[List[int]]]:
    """把系列组装箱成若干批量 PR: 系列组不可拆分, 每箱番剧总数尽量接近但不超过 chunk;
    单个组超过 chunk 时独占一箱. 按 series key 顺序 first-fit, 结果稳定."""
    chunk = max(1, chunk)
    bins: List[List[List[int]]] = []
    sizes: List[int] = []
    for g in sorted(groups, key=lambda x: x[0]):
        if len(g) > chunk:
            bins.append([g])
            sizes.append(len(g))
            continue
        for i, n in enumerate(sizes):
            if n + len(g) <= chunk:  # 独占箱的 n 已 > chunk, 不会再放入
                bins[i].append(g)
                sizes[i] += len(g)
                break
        else:
            bins.append([g])
            sizes.append(len(g))
    return bins


def build_batch_body(ids: List[int], records: dict, vocab: Tuple[List[dict], List[dict], List[str]],
                     include_monosugoi: bool = False, label: str = "新增番剧",
                     groups: Optional[List[List[int]]] = None) -> str:
    """多部番剧合并为一个批量标注 PR, 每个系列一节, 每节独立勾选.

    单部系列维持原样: 一节一部, 用 <!-- sid:<id> --> 标记.
    多部系列为「系列节」: 节头列出该系列全部番剧 (封面 + 名 + bgm id), 每部带
    <!-- sid:<id> series:<key> --> 标记; 勾选框只有一组, 作用于整个系列, handle 解析后
    把这一组勾选广播写入系列内每一部的 data/labels/<sid>.json, 保证同系列 tag 完全一致.
    groups 缺省时每个 id 自成一组 (= 旧版每部一节).
    include_monosugoi: 全量回填存量番剧时打开 (很多是已看完的老番, 需要能标「物凄い」);
    真正的新增番剧批量仍不主动展示 (没看完不谈最爱).
    """
    susume, genre, _ = vocab
    if groups is None:
        groups = [[sid] for sid in ids]
    n_series = sum(1 for g in groups if len(g) > 1)
    lines = [
        f"## 批量标注 ({len(ids)} 部{label})",
        "",
        "每个系列一节 (单部番一节一部), 勾完各自节内的标签即可.",
        f"**{CONFIRM_HINT}** 合入时对全部节生效 (只写有标注的节); 备用: 评论 /done.",
        (f"其中 {n_series} 节是「系列节」: 一组勾选作用于该系列全部番剧, 同系列每一部写入完全相同的 tag."
         if n_series else "本批没有多部系列, 每部一节."),
        "只想处理某几节时, 其余节留空即可, 合入时只写有标注的文件.",
        "",
    ]

    def tag_boxes() -> List[str]:
        out_lines = ["**推荐分类**", ""]
        for s in susume:
            if s["display"] == "物凄い" and not include_monosugoi:
                continue  # 新增模式不主动展示物凄い
            out_lines.append(checkbox(f"{s['display']} (/anime/susume/{s.get('slugPage', '')})"))
        out_lines += ["", "**标注**", ""]
        for g in genre:
            out_lines.append(checkbox(g["display"]))
        return out_lines

    for grp in groups:
        if len(grp) == 1:
            sid = grp[0]
            name = title_of(records.get(sid, {}), sid)
            lines += [
                f"<!-- sid:{sid} -->",
                f'<img src="{poster_url(sid)}" width="120" align="right" alt="封面">',
                "",
                f"### 《{name}》 · bgm {sid}",
                "",
            ]
        else:
            key = min(grp)
            head_name = title_of(records.get(key, {}), key)
            lines += [
                f"### 系列《{head_name}》 · 共 {len(grp)} 部",
                "",
                f"> 勾选作用于本系列全部 {len(grp)} 部: 下面这一组勾选会原样写入系列内每一部, 同系列 tag 完全一致.",
                "",
            ]
            for sid in grp:
                lines += [
                    f"<!-- sid:{sid} series:{key} -->",
                    f'- <img src="{poster_url(sid)}" width="60" alt="封面"> '
                    f"《{title_of(records.get(sid, {}), sid)}》 · bgm {sid}",
                ]
            lines += [""]
        lines += tag_boxes()
        lines += ["", "---", ""]
    lines += [
        checkbox("同步到 bgm (默认勾上)"),
        checkbox("🚫 跳过整批, 关闭 (勾上后同样要加 label `state:待合入` 或评论 /skip 才生效)"),
        "",
        f"> **{CONFIRM_HINT}**",
        "",
        "指令: /done /skip /nobgm (批量 PR 上 /tag /untag 不生效, 逐节请用各节勾选框)",
    ]
    return "\n".join(lines)


def open_batch(ids: List[int], records: dict, vocab: Tuple[List[dict], List[dict], List[str]],
               include_monosugoi: bool = False, label: str = "新增番剧",
               groups: Optional[List[List[int]]] = None) -> dict:
    branch = f"label/batch-{ids[0]}"
    # 不再给每部预写 tags 为空的草稿: 空文件会随 squash 进 main, 且被 cmd_backfill 当作已标注永不回填.
    # 只有 handle 真正解析到某节有勾选时才写 data/labels/<sid>.json.
    # GitHub 不允许无提交差异的 PR, 新建分支时只提交一个不改任何文件的空 commit.
    if ensure_branch(branch):
        empty_commit(branch, f"[批量标注] 初始化 {len(ids)} 部 (不预写草稿)")
    body = build_batch_body(ids, records, vocab, include_monosugoi=include_monosugoi, label=label,
                            groups=groups)
    labels = [LABEL_FLOW, STATE_WAIT, MODE_NEW]
    pr = create_labeled_pr(
        {"title": f"[批量标注] {len(ids)} 部{label}", "head": branch, "base": BASE_BRANCH,
         "body": body, "labels": labels, "maintainer_can_modify": True}, labels)
    res: Dict[str, Any] = {"ids": ids, "pr": pr.get("number"), "action": "opened-batch"}
    if groups:
        res["series"] = [g for g in groups if len(g) > 1]
    return res


def pr_sids(pr: dict) -> set:
    """一个标注 PR 覆盖的全部番剧 id: 批量 PR 解析正文 <!-- sid:N --> 标记 (SID_MARK),
    单部 PR 用分支名 label/<id>. 不再从 label/batch-<ids[0]> 推 id (只含首部)."""
    ids = {int(m.group(1)) for m in SID_MARK.finditer(pr.get("body") or "")}
    last = pr.get("head", {}).get("ref", "").split("/")[-1]
    if last.isdigit():
        ids.add(int(last))
    return ids


def open_label_prs() -> List[dict]:
    return [pr for pr in paged(f"/repos/{REPO}/pulls?state=open")
            if any(l["name"] == LABEL_FLOW for l in pr.get("labels", []))]


def find_open_pr(sid: int) -> Optional[dict]:
    for pr in open_label_prs():
        if sid in pr_sids(pr) or f"#{sid}" in (pr.get("title") or ""):
            return pr
    return None


def ensure_branch(branch: str) -> bool:
    """确保分支存在; 本次新建返回 True."""
    try:
        api("GET", f"/repos/{REPO}/git/ref/heads/{branch}")
        return False
    except RuntimeError:
        pass
    main = api("GET", f"/repos/{REPO}/git/ref/heads/{BASE_BRANCH}")
    sha = main["object"]["sha"]
    api("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    return True


def branch_sha(branch: str) -> str:
    return api("GET", f"/repos/{REPO}/git/ref/heads/{branch}")["object"]["sha"]


def empty_commit(branch: str, message: str) -> str:
    """在分支上提交一个树不变的空 commit (让批量 PR 可开, 又不带任何文件改动), 返回 commit sha."""
    parent = branch_sha(branch)
    tree = api("GET", f"/repos/{REPO}/git/commits/{parent}")["tree"]["sha"]
    c = api("POST", f"/repos/{REPO}/git/commits", {"message": message, "tree": tree, "parents": [parent]})
    api("PATCH", f"/repos/{REPO}/git/refs/heads/{branch}", {"sha": c["sha"], "force": False})
    return c["sha"]


def put_file(branch: str, path: str, content: str, message: str) -> Optional[str]:
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
    res = api("PUT", f"/repos/{REPO}/contents/{path}", body)
    return (res.get("commit") or {}).get("sha")


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

    label_for([LABEL_FLOW, STATE_WAIT, STATE_DONE, STATE_EXPIRED, MODE_NEW, MODE_REVIEW, STATE_CONFIRM])
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
        pr = create_labeled_pr(
            {"title": title, "head": branch, "base": BASE_BRANCH,
             "body": body, "labels": labels, "maintainer_can_modify": True}, labels)
        opened.append({"id": sid, "pr": pr.get("number"), "action": "opened"})
    report({"opened": opened})
    print(f"open: {len(opened)} PR (events: new={len(new_ids)} review={len(review_by_id)})")


# ── 解析勾选 / 指令 ─────────────────────────────────────────────────────
CHECKED = re.compile(r"^- \[[xX]\]\s*(.+?)\s*$", re.M)


def parse_checked(body: str) -> List[str]:
    return [m.group(1).strip() for m in CHECKED.finditer(body or "")]


def has_confirm_label(pr: dict) -> bool:
    """PR 是否已带最终确认 label state:待合入 (pr["labels"] 为 GitHub 返回的 [{name: ...}] 或纯字符串)."""
    for l in pr.get("labels") or []:
        name = l.get("name") if isinstance(l, dict) else l
        if name == STATE_CONFIRM:
            return True
    return False


def parse_body(body: str, vocab: Tuple[List[dict], List[dict], List[str]],
               confirm: bool = False) -> Dict[str, Any]:
    """解析正文勾选. done 的唯一来源 = confirm (PR 已带 label state:待合入), 由 cmd_handle 传入;
    评论 /done 在 apply_comment 里另行置 done. 正文里的旧版「✅ 标注完成」勾选框只做兼容识别, 不再算 done."""
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
            pass  # 旧版正文的完成勾选框: 兼容 (不报错、不当标签), 但不再作为 done 依据
        elif "跳过" in c:
            skip = True
        elif "同步到 bgm" in c:
            sync_bgm = True
    done = bool(confirm)
    # 勾了跳过优先于完成
    if skip:
        done = False
    result = {"tags": tags, "rate": rate, "sync_bgm": sync_bgm, "done": done, "skip": skip}

    # 批量 PR: 按节归集. 单部节 = 一个 <!-- sid:N -->; 系列节 = 连续若干个带同一 series:K 的标记,
    # 节的勾选区在最后一个成员标记之后. 一节的 tags 勾选集合映射到节内全部 sid (sections),
    # per_id 是它的广播展开 {sid: tags}, 同系列每个 sid 拿到同一份 tags.
    # 只有 tags 是作品级 (整系列共用); rate / review 逐季独立, 节里不收集也不广播.
    marks = list(SID_MARK.finditer(body or ""))
    if marks:
        groups: List[Tuple[Optional[int], List[Any]]] = []
        for mk in marks:
            key = int(mk.group(2)) if mk.group(2) else None
            if key is not None and groups and groups[-1][0] == key:
                groups[-1][1].append(mk)
            else:
                groups.append((key, [mk]))
        sections: List[Dict[str, Any]] = []
        per_id: Dict[int, List[str]] = {}
        for gi, (key, mks) in enumerate(groups):
            seg_end = groups[gi + 1][1][0].start() if gi + 1 < len(groups) else len(body)
            seg = body[mks[-1].end(): seg_end]
            seg_tags: List[str] = []
            for c in parse_checked(seg):
                c_plain = re.sub(r"\s*\(.*?\)\s*$", "", c).strip()
                if c_plain in names and c_plain not in seg_tags:
                    seg_tags.append(c_plain)
            sids = list(dict.fromkeys(int(mk.group(1)) for mk in mks))
            # series key 只在脚本内部用于分节, 不写进 data/labels/*.json
            sections.append({"series": key, "ids": sids, "tags": seg_tags})
            for sid in sids:
                per_id[sid] = list(seg_tags)  # 广播: 同节 (同系列) 每一部同一份 tags
        result["batch"] = True
        result["per_id"] = per_id
        result["sections"] = sections
        # 批量 PR 的全局 tags 只用于置顶预览, 不写任何单部草稿
    else:
        result["batch"] = False
        result["per_id"] = {}
        result["sections"] = []
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
    if state.get("batch"):
        filled = [str(k) for k, v in (state.get("per_id") or {}).items() if v]
        preview += f"- 已勾选的节: {', '.join(filled) or '(无)'}\n"
    if state.get("note"):
        preview += f"\n> {state['note']}\n"
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


def get_file(ref: str, path: str) -> Tuple[Optional[dict], Optional[str]]:
    """读分支上的 JSON 文件, 返回 (内容, blob sha); 不存在返回 (None, None)."""
    import base64
    try:
        cur = api("GET", f"/repos/{REPO}/contents/{path}?ref={ref}")
    except RuntimeError:
        return None, None
    try:
        return json.loads(base64.b64decode(cur["content"]).decode()), cur.get("sha")
    except (KeyError, ValueError):
        return None, cur.get("sha")


def write_draft(pr: dict, path: str, data: dict, msg: str) -> Optional[str]:
    """写草稿 (文件不存在则新建), 返回这次写入产生的 commit sha."""
    import base64
    _, sha = get_file(pr["head"]["ref"], path)
    body: Dict[str, Any] = {
        "message": msg, "branch": pr["head"]["ref"],
        "content": base64.b64encode((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    res = api("PUT", f"/repos/{REPO}/contents/{path}", body)
    return (res.get("commit") or {}).get("sha")


def prune_empty_drafts(pr: dict, sids: List[int]) -> Optional[str]:
    """合入前清掉没有标注的节在分支上的文件 (兼容旧版 open_batch 预写的空草稿):
    main 上已有该文件则还原为 main 的内容, 否则从分支删除. 返回最后一次写入的 commit sha."""
    import base64
    ref = pr["head"]["ref"]
    last = None
    for sid in sids:
        path = f"{LABELS_DIR}/{sid}.json"
        cur, sha = get_file(ref, path)
        if sha is None:
            continue
        main_data, _ = get_file(BASE_BRANCH, path)
        if main_data is not None:
            if main_data == cur:
                continue
            res = api("PUT", f"/repos/{REPO}/contents/{path}", {
                "message": f"[批量标注] 还原未标注的 {sid}", "branch": ref, "sha": sha,
                "content": base64.b64encode((json.dumps(main_data, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
            })
        else:
            res = api("DELETE", f"/repos/{REPO}/contents/{path}",
                      {"message": f"[批量标注] 删除空草稿 {sid}", "branch": ref, "sha": sha})
        last = (res.get("commit") or {}).get("sha") or last
    return last


def http_code(err: Exception) -> int:
    m = re.search(r"-> (\d{3}):", str(err))
    return int(m.group(1)) if m else 0


def wait_head(num: int, sha: str, timeout: int = MERGE_WAIT_S) -> bool:
    """轮询 GET /pulls/{n}, 等 PR 的 head.sha 追上最后一次写入的 commit (head 刷新是异步的)."""
    deadline = time.time() + timeout
    while True:
        cur = api("GET", f"/repos/{REPO}/pulls/{num}")
        if cur.get("head", {}).get("sha") == sha:
            return True
        if time.time() >= deadline:
            print(f"  PR #{num} head.sha={cur.get('head', {}).get('sha')} 未追上 {sha} (超时 {timeout}s)",
                  file=sys.stderr)
            return False
        time.sleep(2)


def remove_label(num: int, name: str) -> None:
    """从 PR 上摘掉一个 label (不存在 404 忽略). GITHUB_TOKEN 的操作不会再触发工作流."""
    import urllib.parse
    try:
        api("DELETE", f"/repos/{REPO}/issues/{num}/labels/{urllib.parse.quote(name, safe='')}")
    except RuntimeError as e:
        if http_code(e) != 404:
            print(f"  remove label {name} on #{num}: {e}", file=sys.stderr)


def merge_pr(pr: dict, tags: List[str], susume_names: List[str], expect_sha: Optional[str] = None) -> bool:
    """带 sha 合入: 先等 PR head.sha 等于最后一次写入的 commit, merge body 带 sha, 409/405 重试.
    只有词表 susume 分组内的标签才打 susume:<tag> label, genre 不打. 返回是否合入成功."""
    num = pr["number"]
    ref = pr["head"]["ref"]
    if not expect_sha:
        expect_sha = branch_sha(ref)
    merged = False
    for attempt in range(MERGE_RETRIES):
        if not wait_head(num, expect_sha):
            # head 迟迟不刷新: 以分支 ref (同步更新) 为准再等一轮
            expect_sha = branch_sha(ref)
            continue
        try:
            api("PUT", f"/repos/{REPO}/pulls/{num}/merge",
                {"merge_method": "squash", "sha": expect_sha,
                 "commit_title": f"[标注] {pr['title']} (#{num})"})
            merged = True
            break
        except RuntimeError as e:
            code = http_code(e)
            if code not in (405, 409) or attempt == MERGE_RETRIES - 1:
                print(f"  merge PR #{num} 失败: {e}", file=sys.stderr)
                if code in (405, 409):
                    break
                raise
            print(f"  merge PR #{num} -> {code}, 重试 ({attempt + 1}/{MERGE_RETRIES})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
            expect_sha = branch_sha(ref)
    if not merged:
        return False
    labels = [l["name"] for l in pr.get("labels", [])]
    # 合入成功: 摘掉 state:等待标注 与最终确认 label state:待合入, 换上 state:已标注
    labels = [l for l in labels if l not in (STATE_WAIT, STATE_CONFIRM)] + [STATE_DONE]
    label_for([STATE_DONE])
    # susume 结果 label: 只给推荐分类打, 九项 genre 不打
    for t in dict.fromkeys(tags):
        if t not in susume_names:
            continue
        lab = f"susume:{t}"
        label_for([lab])
        if lab not in labels:
            labels.append(lab)
    api("PATCH", f"/repos/{REPO}/issues/{num}", {"labels": labels})
    return True


def cmd_handle(args: argparse.Namespace) -> None:
    vocab = load_vocab()
    susume, _, names = vocab
    susume_names = [x["display"] for x in susume]
    pr = api("GET", f"/repos/{REPO}/pulls/{args.pr}")
    # 完成信号: 只看 PR 是否带 label state:待合入 (workflow 在 labeled 该 label 时触发); 正文勾选框不算.
    confirm = has_confirm_label(pr)
    body_state = parse_body(pr.get("body") or "", vocab, confirm=confirm)
    # 叠加最新一条评论指令 (只取本次触发的评论, 由调用方经 env 传; 缺省扫全部)
    state = dict(body_state)
    state.setdefault("review", "")
    state.setdefault("resync", False)
    comment = os.environ.get("LABEL_COMMENT_BODY", "")
    state = apply_comment(comment, state, names)
    # review 文本也可能来自 /review

    # 合入门槛: 至少一个非空标注节 (批量: 某节有勾选标签; 单部: 标签/评分/观后感任一非空),
    # 否则 label state:待合入 或 /done 都不写不合并, 只刷新置顶评论, 并摘掉 label 以便勾好后重新加.
    if state.get("batch"):
        has_label = any(state.get("per_id", {}).values())
    else:
        has_label = bool(state["tags"]) or state["rate"] is not None or bool(state.get("review"))
    blocked = bool(state["done"]) and not state["skip"] and not has_label
    if blocked:
        state["note"] = ("还没有任何标注, 本次不写入也不合入. 已摘掉 label `state:待合入`, "
                         "勾选标签后重新加上即可.")
    sticky_comment(args.pr, state)
    out("pr", str(args.pr))
    if blocked:
        if confirm:
            remove_label(args.pr, STATE_CONFIRM)
        report({"pr": args.pr, "action": "blocked-empty"})
        print(f"handle: PR #{args.pr} done 被拒: 没有任何非空标注节")
        return

    if state["skip"]:
        api("PATCH", f"/repos/{REPO}/pulls/{args.pr}", {"state": "closed"})
        api("POST", f"/repos/{REPO}/issues/{args.pr}/labels", {"labels": [STATE_EXPIRED]})
        report({"pr": args.pr, "action": "skipped"})
        print(f"handle: PR #{args.pr} skipped and closed")
        return

    # 批量标注 PR: 按节写草稿, 只写有勾选的节, 支持部分完成.
    # 系列节广播 (broadcast) 只覆盖 tags: tags / 推荐分类是作品级, 整系列共用;
    # rate (评分, 按 bgm 逐部) 与 review (观后感, 由单部状态变化触发) 逐季独立, 不广播.
    # 写回时保留已有文件: 目标 sid 已有 data/labels/<sid>.json (先看 PR 分支, 再看 main) 就读出来,
    # 只替换 tags, rate / review / by / labeledAt / mode 等其余字段原样保留; 没有文件才用新草稿.
    # 本节没勾选 -> 节内任何 sid 都不写 (已有非空标注不会被冲掉); 本节勾选了但某 sid 已有不同的
    # 非空 tags -> 照写 (本次勾选为准), 并列进 overwritten, 写进置顶评论与 report 供核对.
    # series key 不写进标注文件, 一部番一个文件、按各自 sid; bgm 同步按 sid 逐个走.
    if state.get("batch"):
        per_id = state.get("per_id") or {}
        sections = state.get("sections") or [
            {"series": None, "ids": [sid], "tags": tags} for sid, tags in per_id.items()]
        labeled_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        ref = pr["head"]["ref"]
        written, filled, overwritten, last_sha = [], [], [], None
        for sec in sections:
            tags = list(sec.get("tags") or [])
            if not tags:
                continue  # 这节没勾, 不写文件 (未标注的仍是未标注, 已有标注原样保留)
            for sid in sec["ids"]:
                filled.append(sid)
                path = f"{LABELS_DIR}/{sid}.json"
                cur, _ = get_file(ref, path)
                existing = cur if cur is not None else get_file(BASE_BRANCH, path)[0]
                has_file = isinstance(existing, dict) and bool(existing)
                # 保留已有字段: 以已有文件为底, 只替换 tags; 没有文件才从空底开始
                draft = dict(existing) if has_file else {}
                old_tags = list(draft.get("tags") or [])
                if old_tags and old_tags != tags:
                    overwritten.append({"id": sid, "old": old_tags, "new": tags})
                draft["id"] = sid
                draft["tags"] = tags
                # rate / review 逐季独立: 已有值原样保留, 缺失才补默认 (不广播本节的任何评分/观后感)
                draft.setdefault("rate", None)
                draft.setdefault("review", "")
                # by / labeledAt / mode: 已有非空值保留, 新文件或缺失时取本次 PR 的同一份值
                for k, v in (("mode", "new"), ("by", f"pr#{args.pr}"), ("labeledAt", labeled_at)):
                    if not draft.get(k):
                        draft[k] = v
                if old_tags != tags or not has_file:
                    draft["bgmSynced"] = False  # tags 变了 (或新文件), 需要 (重新) 同步 bgm
                draft.setdefault("bgmSynced", False)
                draft["sync_bgm"] = state["sync_bgm"]
                if cur is not None and cur == draft:
                    continue  # 与分支上已写的一致, 不重复提交
                msg = (f"[批量标注] 系列 {sec['series']} 广播 tags 到 {sid} (tags={','.join(tags)})"
                       if sec.get("series") is not None else f"[批量标注] 更新 {sid} (tags={','.join(tags)})")
                last_sha = write_draft(pr, path, draft, msg) or last_sha
                written.append(sid)
        if overwritten:
            state["note"] = "本次勾选覆盖了已有非空 tags 的番: " + "; ".join(
                f"{o['id']} ({','.join(o['old'])} → {','.join(o['new'])})" for o in overwritten)
            print(f"  覆盖已有 tags: {overwritten}", file=sys.stderr)
            try:
                sticky_comment(args.pr, state)
            except RuntimeError as e:
                print(f"  sticky_comment: {e}", file=sys.stderr)
        if state["done"] and filled:
            # 合入前删掉 / 还原没勾的节 (兼容旧版预写的空草稿), 保证只有有标注的文件进 main
            last_sha = prune_empty_drafts(pr, [sid for sid, tags in per_id.items() if not tags]) or last_sha
            ok = merge_pr(pr, [t for tags in per_id.values() for t in tags], susume_names, last_sha)
            if ok:
                out("merged", "true")
                report({"pr": args.pr, "action": "merged-batch", "written": written, "labeled": filled,
                        "overwritten": overwritten})
                print(f"handle: batch PR #{args.pr} merged, {len(filled)} labels")
            else:
                if confirm:
                    remove_label(args.pr, STATE_CONFIRM)  # 让人重新加 label 再试一次
                report({"pr": args.pr, "action": "merge-failed-batch", "written": written,
                        "overwritten": overwritten})
                print(f"handle: batch PR #{args.pr} merge 未完成 (head 未刷新或 409/405 重试耗尽)")
        else:
            report({"pr": args.pr, "action": "updated-batch", "written": written, "overwritten": overwritten})
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
    last_sha = write_draft(pr, path, draft, f"[标注] 更新 {sid} (tags={','.join(state['tags']) or '无'})")

    if state["done"]:
        if merge_pr(pr, state["tags"], susume_names, last_sha):
            out("merged", "true")
            report({"pr": args.pr, "action": "merged", "tags": state["tags"], "rate": state["rate"]})
            print(f"handle: PR #{args.pr} merged")
        else:
            if confirm:
                remove_label(args.pr, STATE_CONFIRM)  # 让人重新加 label 再试一次
            report({"pr": args.pr, "action": "merge-failed", "tags": state["tags"]})
            print(f"handle: PR #{args.pr} merge 未完成 (head 未刷新或 409/405 重试耗尽)")
    elif state.get("resync"):
        out("resync", "true")
        report({"pr": args.pr, "action": "resync"})
    else:
        report({"pr": args.pr, "action": "updated", "tags": state["tags"], "rate": state["rate"]})
        print(f"handle: PR #{args.pr} draft updated")


def cmd_post_merge(args: argparse.Namespace) -> None:
    pr = api("GET", f"/repos/{REPO}/pulls/{args.pr}")
    labels = [l["name"] for l in pr.get("labels", [])]
    labels = [l for l in labels if l not in (STATE_WAIT, STATE_CONFIRM)] + [STATE_DONE]
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
    pr = create_labeled_pr(
        {"title": ("[观后感]" if mode == "review" else "[标注]") + f" 《{title_of(records.get(sid, {}), sid)}》",
         "head": f"label/{sid}", "base": BASE_BRANCH, "body": pr_body,
         "labels": labels, "maintainer_can_modify": True}, labels)
    api("POST", f"/repos/{REPO}/issues/{args.issue}/comments",
        {"body": f"已代开标注 PR #{pr.get('number')}, 请去 PR 里勾选. 合入时本 issue 自动关闭."})
    report({"issue": args.issue, "action": "pr-opened", "pr": pr.get("number"), "id": sid})
    print(f"issue #{args.issue} -> PR #{pr.get('number')} (id={sid}, mode={mode})")


def cmd_backfill(args: argparse.Namespace) -> None:
    """全量回填: 列出所有还没有标注文件的番, 按系列分组后合并成批量标注 PR 供人工逐节勾选.

    清空旧 tag 后 data/labels/ 是空的, 这个入口负责把存量番剧一次性摊开给人标.
    """
    vocab = load_vocab()
    records = load_records()
    # 已标注 = data/labels/ 下有非空标注文件 (旧版预写进 main 的空草稿不算); 排除已开着标注 PR 的番
    labeled = set()
    if os.path.isdir(LABELS_DIR):
        for fn in os.listdir(LABELS_DIR):
            if fn.endswith(".json"):
                try:
                    sid = int(fn[:-5])
                except ValueError:
                    continue
                try:
                    with open(os.path.join(LABELS_DIR, fn), encoding="utf-8") as f:
                        d = json.load(f)
                except (OSError, ValueError):
                    d = {}
                if d.get("tags") or d.get("rate") is not None or d.get("review"):
                    labeled.add(sid)
    # 已开 PR 覆盖的 id: 解析正文 <!-- sid:N --> (SID_MARK) 收集批量 PR 的全部成员, 不再只看分支名里的 ids[0]
    open_pr_ids = set()
    for pr in open_label_prs():
        open_pr_ids |= pr_sids(pr)
    pending_set = {i for i in records if i not in labeled and i not in open_pr_ids}
    if not pending_set:
        report({"action": "backfill", "pending": 0})
        print(f"backfill: 0 部待标注 (共 {len(records)} 部, 已标注 {len(labeled)} 部)")
        return
    # 同一个系列的番剧 tag 应该一样: 先按系列分组 (series_groups), 只保留待标注成员;
    # 一个系列组整体放进同一个批量 PR 的一节 (不拆到不同 PR), 再按 series key 顺序装箱,
    # 每个 PR 番剧总数接近但不超过 args.chunk; 单个组超过 chunk 时独占一个 PR.
    all_groups = series_groups(records)
    groups = [[i for i in g if i in pending_set] for g in all_groups]
    # 系列里已有成员标注过 (或在开着的 PR 里): 已标注的不进本次 PR, 也不会被广播覆盖; 列出来供核对
    partial = [{"series": g[0], "pending": [i for i in g if i in pending_set],
                "excluded": [i for i in g if i not in pending_set]}
               for g in all_groups if len(g) > 1 and any(i in pending_set for i in g)
               and any(i not in pending_set for i in g)]
    groups = [g for g in groups if g]
    bins = pack_series(groups, args.chunk)
    label_for([LABEL_FLOW, STATE_WAIT, MODE_NEW])
    results = []
    for grp_list in bins:
        ids = [i for g in grp_list for i in g]
        # 全量回填的是存量番剧 (很多已看完), 要能标「物凄い」这个最强分类
        results.append(open_batch(ids, records, vocab, include_monosugoi=True, label="存量番剧回填",
                                  groups=grp_list))
    n_series = sum(1 for g in groups if len(g) > 1)
    report({"action": "backfill", "pending": len(pending_set), "series": n_series,
            "series_partial": partial, "prs": results})
    print(f"backfill: {len(pending_set)} 部待标注 ({len(groups)} 个系列组, 其中多部系列 {n_series} 个)"
          f" -> {len(results)} 个批量标注 PR (共 {len(records)} 部)")


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
