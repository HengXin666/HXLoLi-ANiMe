"""番剧萌属性标签生成器: data/ANiMeRecord.json -> data/ANiMeTags.json

数据源:
- bangumi-data (CC-BY-4.0): bgm id -> MAL / AniList / AniDB id 的映射层
- manami-project/anime-offline-database, 简称 AOD (ODbL-1.0 + DbCL-1.0): 主标签源, 合并了 AniDB / AniList / Kitsu / anime-planet 等站点的 tags
- AniList GraphQL: genres, tags[].rank, 主角色年龄, 用作萝莉 / 妹控 / 搞笑 / 恋爱的辅助信号
- 本仓库 ANiMeRecord.json 里的 Bangumi 角色 (relation / 年龄 / 身高 / 简介): 萝莉的核心角色群信号

产物是生成物, 不手改. 任何一个源失败都直接退出, 保留旧的 ANiMeTags.json 不覆盖.

用法:
    uv run Tags.py                         # 联网下载全部源
    uv run Tags.py --aod-file aod.json     # 用本地 AOD 文件 (调试用, 省掉 62 MB 下载)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT_PATH = os.path.join(REPO_ROOT, "data")
RECORD_PATH = os.path.join(DATA_ROOT_PATH, "ANiMeRecord.json")
OUT_PATH = os.path.join(DATA_ROOT_PATH, "ANiMeTags.json")

AOD_URL = "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json"
AOD_RELEASE_URL = "https://github.com/manami-project/anime-offline-database/releases/latest"
BANGUMI_DATA_URL = "https://cdn.jsdelivr.net/npm/bangumi-data@latest/dist/data.json"
BANGUMI_DATA_PKG_URL = "https://cdn.jsdelivr.net/npm/bangumi-data@latest/package.json"
ANILIST_URL = "https://graphql.anilist.co"

HEADERS = {"User-Agent": "HXLoLi-ANiMe-Tags/1.0 (https://github.com/HengXin666/HXLoLi-ANiMe)"}
ANILIST_BATCH = 20
ANILIST_SLEEP = 2.5  # AniList 限流 90 req/min, 一批 20 部 = 1 请求

CST = timezone(timedelta(hours=8))

# --- 标签维度 ---
# 舰娘 已按用户判定 (2026-11-02) 整个移除, 不是 userOnly; 输出前再过一遍 DROPPED_TAGS 兜底
DROPPED_TAGS: Set[str] = {"舰娘"}
# 用户判定 (2026-11-02): 萝莉 的显示名改为片假名 ロリ. 输出一律写 ロリ; 旧名只在这里出现, 供归并
LEGACY_TAG_ALIASES: Dict[str, str] = {"萝莉": "ロリ"}

# 直接映射: 站内标签 -> AOD tags 命中任一即上墙 (evidence 记 aod:<tag>)
# 搞笑 / 恋爱 额外要求 AniList genres 同时含对应 genre, 否则 AOD 的 comedy / romance 太宽 (搞笑 136/169)
AOD_TAG_MAP: Dict[str, Set[str]] = {
    "异世界": {"isekai", "reincarnation", "person in a strange world"},
    "校园": {"school life", "school club", "school clubs", "high school", "elementary school", "middle school"},
    "日常": {"slice of life", "iyashikei", "cgdct"},
    "恋爱": {"romance", "romantic comedy"},
    "搞笑": {"comedy"},
    "战斗": {"action", "fighting", "battle"},
    "催泪": {"tragedy", "tearjerker"},
    "悬疑": {"mystery", "psychological", "thriller"},
    "百合": {"yuri", "shoujo ai", "girls love"},
}
ANILIST_GENRE_GATE: Dict[str, str] = {"搞笑": "Comedy", "恋爱": "Romance"}

# ロリ (旧名 萝莉) / 妹控 走打分: score >= 2 上墙, == 1 进 candidates
WALL_SCORE = 2

LOLI_AOD_TAGS = {"loli", "lolicon"}  # +2
LOLI_ANILIST_PCC_RANK = 50  # AniList "Primarily Child Cast" rank >= 50: +1
LOLI_CHILD_AGE = 12  # 小学生年纪
LOLI_SMALL_HEIGHT_CM = 145  # 幼女外形的代理: 女性角色 Bangumi 身高首值 <= 145cm
LOLI_SMALL_AGE_CAP = 14  # 身高代理只对首个年龄 <= 14 的角色生效

# 妹控: 2026-11-02 实测 AOD 全库 tags 中 imouto 10 部, sister complex 10, brother-sister incest 35, siblings 237;
# AniList 没有 imouto / siscon 标签, 只有 Incest (Theme-Romance); Kitsu 没有对应 category (slug sister-complex / siscon / imouto / incest 均 count 0)
SISCON_AOD_STRONG = {"imouto", "sister complex", "brother-sister incest"}  # +2
SISCON_AOD_WEAK = {"siblings"}  # +1, 太宽, 单独只进 candidates
SISCON_ANILIST_INCEST_RANK = 20  # AniList Incest rank >= 20: +1

# 用户硬判定 (不靠打分, 打分没过也上墙). 键是 bgm subject id
# 2026-11-02 用户判定: 点兔系列算ロリ番 (核心角色含重要配角群, 不限第一主角)
JUDGEMENTS: Dict[int, Dict[str, str]] = {
    88287: {"ロリ": "用户判定 2026-11-02: 点兔系列算ロリ (核心角色群含智乃与 Chimame 组)"},  # 请问您今天要来点兔子吗？
    260770: {"ロリ": "用户判定 2026-11-02: 点兔系列算ロリ (核心角色群含智乃与 Chimame 组)"},  # 请问您今天要来点兔子吗？BLOOM
}
# 点兔第二季在库里出现时也走同一判定: 按 name_cn 前缀兜底
JUDGEMENT_NAME_PREFIX: List[Tuple[str, str, str]] = [
    ("请问您今天要来点兔子吗", "ロリ", "用户判定 2026-11-02: 点兔系列算ロリ (核心角色群含智乃与 Chimame 组)"),
]

TAG_ORDER = ["ロリ", "百合", "妹控", "异世界", "校园", "日常", "恋爱", "搞笑", "战斗", "催泪", "悬疑"]


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 120, retries: int = 3) -> requests.Response:
    last: Optional[Exception] = None
    for i in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"下载失败: {url}: {last}")


# --- 1. 读库 ---
def load_records() -> List[dict]:
    with open(RECORD_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --- 2. bangumi-data 映射 ---
def load_bangumi_data(path: Optional[str]) -> Tuple[Dict[int, Dict[str, str]], str]:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        version = "local"
    else:
        data = http_get(BANGUMI_DATA_URL).json()
        version = http_get(BANGUMI_DATA_PKG_URL).json().get("version", "?")
    mapping: Dict[int, Dict[str, str]] = {}
    for item in data["items"]:
        # sites[] 里部分条目只有 url 没有 id (如 iqiyi), 跳过
        sites = {s["site"]: str(s["id"]) for s in item.get("sites", []) if s.get("id")}
        if "bangumi" in sites:
            mapping[int(sites["bangumi"])] = sites
    return mapping, version


# --- 3. AOD ---
def load_aod(path: Optional[str]) -> Tuple[Dict[str, dict], dict]:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            aod = json.load(f)
        release = "local"
    else:
        release_resp = http_get(AOD_RELEASE_URL)
        release = release_resp.url.rstrip("/").rsplit("/", 1)[-1]
        aod = http_get(AOD_URL, timeout=600).json()
    by_url: Dict[str, dict] = {}
    for entry in aod["data"]:
        for src in entry.get("sources", []):
            by_url[src] = entry
    meta = {
        "release": release,
        "lastUpdate": aod.get("lastUpdate"),
        "license": (aod.get("license") or {}).get("name"),
        "url": aod.get("repository"),
    }
    return by_url, meta


def find_aod(sites: Dict[str, str], by_url: Dict[str, dict]) -> Optional[dict]:
    patterns = [
        ("mal", "https://myanimelist.net/anime/{}"),
        ("aniList", "https://anilist.co/anime/{}"),
        ("anidb", "https://anidb.net/anime/{}"),
    ]
    for key, pat in patterns:
        if key in sites:
            hit = by_url.get(pat.format(sites[key]))
            if hit:
                return hit
    return None


def ids_from_aod(entry: Optional[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not entry:
        return out
    pats = {
        "mal": r"myanimelist\.net/anime/(\d+)",
        "anilist": r"anilist\.co/anime/(\d+)",
        "anidb": r"anidb\.net/anime/(\d+)",
        "kitsu": r"kitsu\.app/anime/(\d+)",
    }
    for src in entry.get("sources", []):
        for k, p in pats.items():
            m = re.search(p, src)
            if m:
                out[k] = int(m.group(1))
    return out


# --- 4. AniList ---
ANILIST_ITEM = (
    "a{n}: Media(id:{id},type:ANIME){{id genres tags{{name rank}} "
    "characters(role:MAIN,perPage:10){{nodes{{name{{full}} age}}}}}}"
)


def fetch_anilist(anilist_ids: List[int]) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for b in range(0, len(anilist_ids), ANILIST_BATCH):
        chunk = anilist_ids[b:b + ANILIST_BATCH]
        query = "query{" + " ".join(ANILIST_ITEM.format(n=n, id=a) for n, a in enumerate(chunk)) + "}"
        for attempt in range(4):
            resp = requests.post(ANILIST_URL, json={"query": query}, headers=HEADERS, timeout=60)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", "60")) + 1)
                continue
            body = resp.json()
            # 单个 id 不存在时 AniList 返回 errors + 部分 data, 只要 data 在就收下
            if body.get("data") is None:
                raise RuntimeError(f"AniList 失败: {resp.status_code} {str(body)[:300]}")
            for n, a in enumerate(chunk):
                media = body["data"].get(f"a{n}")
                if media:
                    out[a] = media
            break
        else:
            raise RuntimeError("AniList 连续 429")
        time.sleep(ANILIST_SLEEP)
    return out


# --- 5. 打分与映射 ---
def first_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else None


_PRIMARY_RE = re.compile(r"小学[一二三四五六1-6１-６]年级")


def all_ints(text: Optional[str]) -> List[int]:
    return [int(x) for x in re.findall(r"(\d+)", str(text or ""))]


def bgm_child_like(char: dict) -> Optional[str]:
    """Bangumi 女性角色是否为 小学生年纪 / 幼女外形, 是则返回理由
    - 年龄全部 <= 12 (年龄常写成 "13岁初二生→14岁初三生", 取最大值, 防止回忆年龄误判)
    - 年龄或简介写明 小学N年级
    - 幼女外形: 身高 <= 145cm 且 首个年龄已知且 <= 14 (排除成年矮个角色与无年龄的配角)
    """
    info = {t.get("key"): t.get("value") for t in (char.get("tags") or [])}
    if info.get("性别") != "女":
        return None
    age_text = info.get("年龄") or ""
    ages = [a for a in all_ints(age_text) if a < 200]
    if "小学" in age_text:
        return f"年龄 {age_text}"
    if ages and max(ages) <= LOLI_CHILD_AGE:
        return f"年龄 {age_text}"
    m = _PRIMARY_RE.search(char.get("summary") or "")
    if m:
        return f"简介 {m.group(0)}"
    height = first_int(info.get("身高"))
    if ages and ages[0] <= LOLI_SMALL_AGE_CAP and height is not None and 100 <= height <= LOLI_SMALL_HEIGHT_CM:
        return f"年龄 {age_text} / 身高 {info.get('身高')}"
    return None


def anilist_main_child(media: Optional[dict]) -> List[str]:
    out = []
    for node in ((media or {}).get("characters") or {}).get("nodes") or []:
        age = first_int(node.get("age"))
        if age is not None and age <= LOLI_CHILD_AGE:
            out.append(f"{node['name']['full']} ({node.get('age')})")
    return out


def anilist_rank(media: Optional[dict], name: str) -> int:
    for t in (media or {}).get("tags") or []:
        if t["name"] == name:
            return int(t["rank"])
    return 0


def score_loli(record: dict, aod_tags: Set[str], media: Optional[dict]) -> Tuple[int, List[str]]:
    score, ev = 0, []
    hit = sorted(aod_tags & LOLI_AOD_TAGS)
    if hit:
        score += 2
        ev += [f"aod:{t}" for t in hit]
    # 核心角色群 = Bangumi 主角 + 配角 (不含闲角). >= 2 个小学生 / 幼女外形: +2 (用户判定: 不限第一主角)
    core = [c for c in record["anime_data"].get("characters") or [] if c.get("relation") in ("主角", "配角")]
    kids = []
    for c in core:
        why = bgm_child_like(c)
        if why:
            kids.append((c, why))
    if len(kids) >= 2:
        score += 2
        ev.append("bgm:核心角色群 " + ", ".join(f"{c.get('name_cn') or c.get('name')}[{c['relation']}|{why}]" for c, why in kids[:6]))
    elif len(kids) == 1 and kids[0][0].get("relation") == "主角":
        score += 1
        c, why = kids[0]
        ev.append(f"bgm:主角 {c.get('name_cn') or c.get('name')}[{why}]")
    rank = anilist_rank(media, "Primarily Child Cast")
    if rank >= LOLI_ANILIST_PCC_RANK:
        score += 1
        ev.append(f"anilist:Primarily Child Cast rank={rank}")
    mains = anilist_main_child(media)
    if mains:
        score += 1
        ev.append("anilist:主角色年龄<=12 " + ", ".join(mains[:3]))
    return score, ev


def score_siscon(aod_tags: Set[str], media: Optional[dict]) -> Tuple[int, List[str]]:
    score, ev = 0, []
    strong = sorted(aod_tags & SISCON_AOD_STRONG)
    if strong:
        score += 2
        ev += [f"aod:{t}" for t in strong]
    weak = sorted(aod_tags & SISCON_AOD_WEAK)
    if weak:
        score += 1
        ev += [f"aod:{t}" for t in weak]
    rank = anilist_rank(media, "Incest")
    if rank >= SISCON_ANILIST_INCEST_RANK:
        score += 1
        ev.append(f"anilist:Incest rank={rank}")
    return score, ev


# Agent Notes: 番剧标签由 AOD + bangumi-data + AniList + Bangumi 角色多源生成, 每个标签带 evidence
# .agents/notes/implemented/feature/2026-11-02-anime-tags-source.md
def build_item(record: dict, sites: Optional[Dict[str, str]], aod: Optional[dict], media: Optional[dict]) -> dict:
    sid = record["anime_data"]["id"]
    name_cn = record["anime_data"].get("name_cn") or record["anime_data"].get("name") or ""
    aod_tags = set((aod or {}).get("tags") or [])
    genres = set((media or {}).get("genres") or [])
    evidence: Dict[str, List[str]] = {}
    candidates: Dict[str, List[str]] = {}

    for tag, src in AOD_TAG_MAP.items():
        hit = sorted(aod_tags & src)
        if not hit:
            continue
        gate = ANILIST_GENRE_GATE.get(tag)
        if gate and gate not in genres:
            continue
        evidence[tag] = [f"aod:{t}" for t in hit] + ([f"anilist:genre {gate}"] if gate else [])

    for tag, (score, ev) in (("ロリ", score_loli(record, aod_tags, media)), ("妹控", score_siscon(aod_tags, media))):
        if score >= WALL_SCORE:
            evidence[tag] = ev + [f"score={score}"]
        elif score == 1:
            candidates[tag] = ev + [f"score={score}"]

    judged = dict(JUDGEMENTS.get(sid, {}))
    for prefix, tag, why in JUDGEMENT_NAME_PREFIX:
        if name_cn.startswith(prefix):
            judged.setdefault(tag, why)
    for tag, why in judged.items():
        tag = LEGACY_TAG_ALIASES.get(tag, tag)
        evidence.setdefault(tag, []).append(f"judgement:{why}")
        candidates.pop(tag, None)

    for tag in DROPPED_TAGS:
        evidence.pop(tag, None)
        candidates.pop(tag, None)

    ids: Dict[str, int] = ids_from_aod(aod)
    for k_src, k_out in (("mal", "mal"), ("aniList", "anilist"), ("anidb", "anidb")):
        if sites and k_src in sites and k_out not in ids:
            ids[k_out] = int(sites[k_src])
    order = {t: i for i, t in enumerate(TAG_ORDER)}
    tags = sorted(evidence, key=lambda t: order.get(t, 99))
    return {
        "name_cn": name_cn,
        "tags": tags,
        "candidates": sorted(candidates, key=lambda t: order.get(t, 99)),
        "evidence": {t: evidence[t] for t in tags},
        "candidateEvidence": {t: candidates[t] for t in sorted(candidates, key=lambda t: order.get(t, 99))},
        "ids": ids,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aod-file", help="本地 AOD json (调试用)")
    ap.add_argument("--bangumi-data-file", help="本地 bangumi-data dist/data.json (调试用)")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    records = load_records()
    log(f"番剧 {len(records)} 部")
    bmap, bd_version = load_bangumi_data(args.bangumi_data_file)
    log(f"bangumi-data {bd_version}: {len(bmap)} 条有 bangumi id")
    aod_by_url, aod_meta = load_aod(args.aod_file)
    log(f"AOD {aod_meta['release']} lastUpdate={aod_meta['lastUpdate']}")

    matched: Dict[int, Tuple[Optional[Dict[str, str]], Optional[dict]]] = {}
    for r in records:
        sid = r["anime_data"]["id"]
        sites = bmap.get(sid)
        matched[sid] = (sites, find_aod(sites, aod_by_url) if sites else None)

    anilist_of: Dict[int, int] = {}
    for sid, (sites, aod) in matched.items():
        a = ids_from_aod(aod).get("anilist") or (int(sites["aniList"]) if sites and "aniList" in sites else None)
        if a:
            anilist_of[sid] = a
    media_by_al = fetch_anilist(sorted(set(anilist_of.values())))
    log(f"AniList {len(media_by_al)}/{len(set(anilist_of.values()))}")

    items: Dict[str, dict] = {}
    unmapped: List[int] = []
    for r in records:
        sid = r["anime_data"]["id"]
        sites, aod = matched[sid]
        media = media_by_al.get(anilist_of.get(sid, -1))
        if not aod and not media:
            unmapped.append(sid)
        items[str(sid)] = build_item(r, sites, aod, media)

    # 兜底断言: 舰娘不出现, 每个上墙标签 evidence 非空
    for sid, it in items.items():
        assert not (set(it["tags"]) | set(it["candidates"])) & (DROPPED_TAGS | set(LEGACY_TAG_ALIASES)), sid
        for t in it["tags"]:
            assert it["evidence"].get(t), (sid, t)

    stats_tags: Dict[str, int] = {t: 0 for t in TAG_ORDER}
    stats_cand: Dict[str, int] = {}
    for it in items.values():
        for t in it["tags"]:
            stats_tags[t] = stats_tags.get(t, 0) + 1
        for t in it["candidates"]:
            stats_cand[t] = stats_cand.get(t, 0) + 1
    out = {
        "generatedAt": datetime.now(CST).isoformat(timespec="seconds"),
        "sources": {
            "aod": aod_meta,
            "bangumiData": {"version": bd_version, "license": "CC-BY-4.0", "url": "https://github.com/bangumi-data/bangumi-data"},
            "anilist": {"url": "https://anilist.co", "fields": "genres, tags.rank, characters(role:MAIN).age"},
            "bangumiCharacters": {"from": "data/ANiMeRecord.json anime_data.characters (relation/年龄/身高/简介)"},
        },
        "rules": {
            "wallScore": WALL_SCORE,
            "loli": "aod loli|lolicon +2; Bangumi 主角+配角中 >=2 个女性 小学生年纪(年龄最大值<=12/小学N年级)/幼女外形(身高<=145cm 且首个年龄<=14) +2, 仅 1 个且为主角 +1; AniList Primarily Child Cast rank>=50 +1; AniList 主角色年龄<=12 +1",
            "siscon": "aod imouto|sister complex|brother-sister incest +2; aod siblings +1; AniList Incest rank>=20 +1",
            "genreGate": ANILIST_GENRE_GATE,
        },
        "stats": {
            "records": len(records),
            "covered": len(records) - len(unmapped),
            "inBangumiData": sum(1 for s, _ in matched.values() if s),
            "inAod": sum(1 for _, a in matched.values() if a),
            "inAniList": sum(1 for sid in matched if media_by_al.get(anilist_of.get(sid, -1))),
            "unmapped": len(unmapped),
            "wall": stats_tags,
            "candidates": stats_cand,
        },
        "items": items,
        "unmapped": unmapped,
    }
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.out)
    log(json.dumps(out["stats"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 任一源失败: 不写文件, 非零退出
        print(f"Tags.py 失败, 保留旧 ANiMeTags.json: {e}", file=sys.stderr)
        sys.exit(1)
