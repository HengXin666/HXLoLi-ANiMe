"""爬虫事件检测: 对比前后两份 data/ANiMeRecord.json, 产出 events.json

依据: hx-anime-tag-refactor-design.md 第 5.1 节 (data 更新流同时产出事件清单) / 第 10 节第 2 步.

两类事件:
- new:    新增番剧 id. 与 anime-pipeline.yaml 中
          `comm -23 <(当前 id 排序) <(HEAD~1 id 排序)` 等价: 当前有, 之前没有.
- review: 状态变化 (观后感 模式). 同一 id 的 user_status.watch_status
          从 3 (在看) 变为 2 (看过) / 4 (搁置) / 5 (抛弃).

输出结构 (events.json):
    {"new": [id, ...], "review": [{"id": id, "from": 3, "to": 2}, ...]}

用法 (供 workflow 调用, 工作目录 scripts/crawler):
    uv run events.py                                  # 旧版本取 git HEAD~1:data/ANiMeRecord.json
    uv run events.py --prev-ref HEAD~1 --out events.json
    uv run events.py --prev-file old.json --curr-file new.json --out events.json
函数入口:
    from events import detect_events
    detect_events(prev_records, curr_records) -> {"new": [...], "review": [...]}

旧版本不存在 (首次接入, git show 失败) 时: 所有当前 id 记为 new, review 为空, 与 pipeline 现有
"PREVIOUS_IDS 为空" 分支不同 (那里只计数) - 这里保留完整 id 列表, 由标注流按 >5 部合并批量 PR (设计 第 11 节).
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORD_REL = "data/ANiMeRecord.json"
RECORD_PATH = os.path.join(REPO_ROOT, RECORD_REL)
DEFAULT_OUT = "events.json"

# ANiMeType.WatchStatus 的取值, 这里只用数值, 避免把 dataclass 解码器拉进来
WATCHING = 3  # 在看
REVIEW_TARGETS = {2, 4, 5}  # 看过 / 搁置 / 抛弃


def _status_value(raw: Any) -> Optional[int]:
    """watch_status 在 JSON 里是 int (ANiMeEncoder 把 Enum 写成 value); 兼容 {"value": n} 等异常形态"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    if isinstance(raw, dict) and "value" in raw:
        return _status_value(raw["value"])
    return None


def index_records(records: List[dict]) -> Dict[int, Optional[int]]:
    """id -> watch_status"""
    out: Dict[int, Optional[int]] = {}
    for r in records or []:
        try:
            sid = int(r["anime_data"]["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[sid] = _status_value((r.get("user_status") or {}).get("watch_status"))
    return out


# Agent Notes: 爬虫事件检测产出 events.json, 供标注流按新增番剧 / 状态变化开标注 PR.
# .agents/notes/proposed/feature/2026-11-04-hx-anime-label-pipeline.md
def detect_events(prev_records: Optional[List[dict]], curr_records: List[dict]) -> Dict[str, list]:
    curr = index_records(curr_records)
    prev = index_records(prev_records or [])
    # 等价 comm -23: 当前有, 之前没有
    new_ids = sorted(set(curr) - set(prev))
    review = []
    for sid in sorted(set(curr) & set(prev)):
        before, after = prev[sid], curr[sid]
        if before == WATCHING and after in REVIEW_TARGETS:
            review.append({"id": sid, "from": before, "to": after})
    return {"new": new_ids, "review": review}


def load_json_file(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_git_rev(ref: str) -> Optional[List[dict]]:
    """git show <ref>:data/ANiMeRecord.json; 取不到 (首次提交 / 文件不存在) 返回 None"""
    try:
        blob = subprocess.run(
            ["git", "-C", REPO_ROOT, "show", f"{ref}:{RECORD_REL}"],
            check=True, capture_output=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def write_events(events: Dict[str, list], out: str) -> None:
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="检测新增番剧与 在看→看过/搁置/抛弃 状态变化, 输出 events.json")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--prev-ref", default="HEAD~1", help="旧版本的 git 引用 (默认 HEAD~1, 与 pipeline 一致)")
    src.add_argument("--prev-file", help="旧版本 ANiMeRecord.json 文件路径 (优先于 --prev-ref)")
    ap.add_argument("--curr-file", default=RECORD_PATH, help="当前 ANiMeRecord.json (默认 data/ANiMeRecord.json)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出路径 (默认当前目录 events.json, 不写进 data/)")
    args = ap.parse_args(argv)

    curr = load_json_file(args.curr_file)
    prev = load_json_file(args.prev_file) if args.prev_file else load_git_rev(args.prev_ref)
    if prev is None:
        print(f"events.py: 取不到旧版本 ({args.prev_ref}), 视为首次接入", file=sys.stderr)
    events = detect_events(prev, curr)
    write_events(events, args.out)
    print(json.dumps({"new": len(events["new"]), "review": len(events["review"]), "out": args.out}, ensure_ascii=False))
    # workflow 可直接读这两个输出决定是否触发标注流
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"new_count={len(events['new'])}\nreview_count={len(events['review'])}\nevents_file={args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
