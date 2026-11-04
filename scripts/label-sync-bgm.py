#!/usr/bin/env python3
"""标注合入后同步到 bgm (设计 9 节, 作业名 label-sync-bgm).

调用 bgm api `POST /v0/users/-/collections/{subject_id}` (带 BGM_TOKEN, 需收藏写权限):
  tags    = (bgm 现有 tags − 词表管理的标签) ∪ 本次标注   → 只增删词表内的词, 其他手打标签原样保留
  rate    仅当本次给了评分
  comment 仅当写了观后感且勾选同步, 且 bgm 上没有已有吐槽 (已有则不覆盖, 在 PR 里提示冲突)
  不改 type (观看状态以 bgm 为准)
失败重试 3 次, 仍失败打 bgm:sync-failed 并在 PR 评论报错; 标注文件照常保留, 之后可 /resync.
防回环: 爬虫下次把 bgm tags 拉回 user_status.tags, 但主站只读 data/labels/, 不会二次计算.

用法: label-sync-bgm.py --pr <标注 PR 号>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BGM_TOKEN = os.environ.get("BGM_TOKEN", "")
BGM_USERNAME = os.environ.get("BGM_USERNAME", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BASE_BRANCH = os.environ.get("LABEL_BASE_BRANCH", "main")
REPORT = os.environ.get("LABEL_REPORT", "")
VOCAB_PATH = os.environ.get("LABEL_VOCAB", "data/labelVocab.json")
LABELS_DIR = "data/labels"
BGM_API = "https://api.bgm.tv"
GITHUB_API = "https://api.github.com"

LABEL_OK = "bgm:synced"
LABEL_FAIL = "bgm:sync-failed"


def _req(method: str, url: str, body: Any, headers: dict, timeout: int = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers)
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def bgm(method: str, path: str, body: Any = None) -> Any:
    headers = {
        "Authorization": f"Bearer {BGM_TOKEN}",
        "Accept": "application/json",
        "User-Agent": f"HXLoLi-ANiMe/label-sync ({BGM_USERNAME or 'hx'})",
    }
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            return _req(method, f"{BGM_API}{path}", body, headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            last = RuntimeError(f"bgm {method} {path} -> {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
    raise last  # type: ignore[misc]


def gh(method: str, path: str, body: Any = None) -> Any:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hx-anime-label-sync",
    }
    return _req(method, f"{GITHUB_API}{path}", body, headers)


def report(obj: Any) -> None:
    if REPORT:
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def managed_bgm_tags() -> List[str]:
    """词表管理的 bgm 同步名全集 (只增删这些, 用户其他手打标签原样保留)."""
    with open(VOCAB_PATH, encoding="utf-8") as f:
        v = json.load(f)
    out = []
    for group in ("susume", "genre"):
        for x in v.get(group, []):
            t = x.get("bgmTag") or x.get("display")
            if t:
                out.append(t)
    return out


def display_to_bgm(name: str) -> str:
    with open(VOCAB_PATH, encoding="utf-8") as f:
        v = json.load(f)
    for group in ("susume", "genre"):
        for x in v.get(group, []):
            if x.get("display") == name:
                return x.get("bgmTag") or name
    return name


def get_label(pr: int) -> Dict[str, Any]:
    """取合入后 main 上该番的标注文件."""
    # 用 PR 的分支名推 subject id (label/<id>)
    pull = gh("GET", f"/repos/{REPO}/pulls/{pr}")
    sid = int(pull["head"]["ref"].split("/")[-1])
    import base64
    cur = gh("GET", f"/repos/{REPO}/contents/{LABELS_DIR}/{sid}.json?ref={BASE_BRANCH}")
    data = json.loads(base64.b64decode(cur["content"]).decode())
    data["id"] = sid
    return data


def set_labels(pr: int, names: List[str]) -> None:
    for name in names:
        try:
            gh("POST", f"/repos/{REPO}/labels", {"name": name, "color": "0e8a16" if "synced" in name else "b60205"})
        except Exception:  # noqa: BLE001 - 已存在
            pass
    gh("POST", f"/repos/{REPO}/issues/{pr}/labels", {"labels": names})


def comment(pr: int, text: str) -> None:
    gh("POST", f"/repos/{REPO}/issues/{pr}/comments", {"body": text})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--id", type=int, help="直接给 subject id (不走 PR 查文件)")
    args = ap.parse_args()

    if not BGM_TOKEN:
        msg = "BGM_TOKEN 未设置, 跳过 bgm 同步 (标注文件已保留, 之后可 /resync)."
        print(msg)
        set_labels(args.pr, [LABEL_FAIL])
        comment(args.pr, msg)
        report({"pr": args.pr, "ok": False, "reason": "no token"})
        return

    try:
        label = get_label(args.pr) if not args.id else {"id": args.id, "tags": [], "rate": None, "review": "", "sync_bgm": True}
    except Exception as e:  # noqa: BLE001
        msg = f"读取标注文件失败: {e}. 标注文件照常保留, 之后可 /resync."
        print(msg, file=sys.stderr)
        set_labels(args.pr, [LABEL_FAIL])
        comment(args.pr, msg)
        report({"pr": args.pr, "ok": False, "reason": str(e)})
        return

    sid = label["id"]
    managed = managed_bgm_tags()
    wanted = [display_to_bgm(t) for t in label.get("tags", [])]

    try:
        # 1) 读 bgm 上该条目当前收藏, 拿到现有 tags 与是否已有 comment
        try:
            cur = bgm("GET", f"/v0/users/-/collections/{sid}")
        except Exception:  # noqa: BLE001 - 404 = 还没收藏, 用空态
            cur = {}
        existing_tags = list(cur.get("tags") or [])
        has_comment = bool((cur.get("comment") or "").strip())

        # 只增删词表内的词, 用户手打的其他标签原样保留
        kept = [t for t in existing_tags if t not in managed]
        tags = kept + [t for t in wanted if t not in kept]

        payload: Dict[str, Any] = {"tags": tags}
        if label.get("rate"):
            payload["rate"] = int(label["rate"])
        review = (label.get("review") or "").strip()
        conflict = False
        if review and label.get("sync_bgm", True) and not has_comment:
            payload["comment"] = review
        elif review and has_comment:
            conflict = True  # 已有吐槽不覆盖, 在 PR 里提示

        # 2) 写回 bgm. 收藏不存在时 POST 会创建; type 不改 (观看状态以 bgm 为准)
        try:
            bgm("POST", f"/v0/users/-/collections/{sid}", payload)
        except urllib.error.HTTPError:
            # 部分部署用 PUT 更新已存在收藏, 退回一次
            bgm("PUT", f"/v0/users/-/collections/{sid}", payload)

        set_labels(args.pr, [LABEL_OK])
        if conflict:
            comment(args.pr, "bgm 上已有吐槽, 按约定没有覆盖; 本次观后感只保留在 HXLoLi 标注文件里.")
        note = f"bgm 同步完成: subject {sid}, tags={tags}, rate={payload.get('rate')}."
        print(note)
        report({"pr": args.pr, "ok": True, "id": sid, "tags": tags, "rate": payload.get("rate"), "comment_conflict": conflict})
    except Exception as e:  # noqa: BLE001
        msg = f"bgm 同步失败 (已重试 3 次): {e}. 标注文件照常保留, 之后可 /resync."
        print(msg, file=sys.stderr)
        set_labels(args.pr, [LABEL_FAIL])
        comment(args.pr, msg)
        report({"pr": args.pr, "ok": False, "id": sid, "reason": str(e)})


if __name__ == "__main__":
    main()
