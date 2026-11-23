"""把 data/ANiMeRecord.json 拆成前端按需读取的两类产物.

- data/record/<id>.json: 单部番的完整记录 (含角色和每集简介), 详情页只拉这一份.
- data/ANiMeIndex.json: 全部番的轻量列表, 去掉 characters, 每集只留 id / type / sort / ep / 时长 / 播出日期 (列表页只用到 type 和时长),
  列表页 / 首页 / 系列页 / 详情页的相似推荐用它.

两者都是生成物, 不手改; 格式与 ANiMeRecord.json 相同 (保留 __dataclass__), 只是紧凑输出.
ANiMeRecord.json 本身不动: 声优页和关系图要全量角色数据, 流水线的 diff / events 也依赖它.

用法 (在 scripts/crawler 下): uv run split.py
爬虫 Api.py 每次 save_to_json 后会自动调用 write_split.
"""
import json
import os
import sys

DATA_ROOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
RECORD_DIR = "record"
INDEX_NAME = "ANiMeIndex.json"
LITE_EPISODE_KEYS = {"id", "type", "sort", "ep", "duration_seconds", "air_date", "__dataclass__"}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _write_if_changed(path: str, text: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def _lite(record: dict) -> dict:
    ad = dict(record["anime_data"])
    ad["characters"] = []
    ad["episodes"] = [{k: v for k, v in ep.items() if k in LITE_EPISODE_KEYS} for ep in ad.get("episodes") or []]
    out = dict(record)
    out["anime_data"] = ad
    return out


def write_split(data_root: str = DATA_ROOT_PATH) -> dict:
    """读 ANiMeRecord.json, 写 record/<id>.json 和 ANiMeIndex.json, 删掉已不在记录里的 record 文件.
    .agents/notes/implemented/feature/2026-11-23-anime-record-split.md
    """
    with open(os.path.join(data_root, "ANiMeRecord.json"), "r", encoding="utf-8") as f:
        records = json.load(f)
    record_dir = os.path.join(data_root, RECORD_DIR)
    os.makedirs(record_dir, exist_ok=True)

    keep = set()
    written = 0
    for r in records:
        rid = r["anime_data"]["id"]
        name = f"{rid}.json"
        keep.add(name)
        written += _write_if_changed(os.path.join(record_dir, name), _dump(r))
    removed = 0
    for name in os.listdir(record_dir):
        if name.endswith(".json") and name not in keep:
            os.remove(os.path.join(record_dir, name))
            removed += 1
    _write_if_changed(os.path.join(data_root, INDEX_NAME), _dump([_lite(r) for r in records]))
    return {"records": len(records), "written": written, "removed": removed}


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else DATA_ROOT_PATH
    print(_dump(write_split(root)))
