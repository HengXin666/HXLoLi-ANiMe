# Agent Note: 番剧记录拆成单部文件和轻量索引

Status: implemented

Decision-ID: anime-record-split

## Code

- `scripts/crawler/split.py`

## Problem

博客的番剧详情页只展示一部番, 却要先把整份 `data/ANiMeRecord.json` 拉下来。这份文件解压后约 15 MB, CDN 压缩传输也有约 1.9 MB, 在用户的网络上冷加载要 5 到 10 秒。首页 "在追的番" 和列表页也读同一份全量文件, 其中 7 成以上体积是角色 (characters) 和每集简介 (desc), 这些页面根本用不到。

用户原话: 「为什么每次我去加载都感觉很慢呢? 至少要等 10 秒」「尽量优化它」。

## Decision

**爬虫每次落盘 ANiMeRecord.json 后, 由 `split.py` 同步生成两类产物, ANiMeRecord.json 本身不变。**

- `data/record/<id>.json`: 单部番的完整记录, 紧凑输出, 格式与 ANiMeRecord.json 的元素相同 (保留 `__dataclass__`)。详情页只拉这一份, 一般十几 KB。
- `data/ANiMeIndex.json`: 全部番的轻量列表。`characters` 清空, 每集只留 `id / type / sort / ep / duration_seconds / air_date`。列表页只用到集数类型和时长, 系列页要 `relations` 和 `summary`, 这些都保留。实测 200 部约 1.2 MB, gzip 约 230 KB, 是全量的八分之一左右。

`Api.py` 的 `save_to_json` 在写完 ANiMeRecord.json 和 Actor.json 后调用 `write_split`, 只重写内容有变化的文件, 并删掉已不在记录里的 `record/<id>.json`。手动重跑用 `uv run split.py`。

## Consequences

- 产物是生成物, 不手改; 想改拆分口径就改 `split.py` 再重跑。
- 前端读新产物时要兼容旧 tag: 拿不到 `ANiMeIndex.json` 或 `record/<id>.json` 就退回全量文件, 所以发布顺序不影响可用性。
- 声优页和关系图仍读全量 ANiMeRecord.json, 因为它们要所有番的角色。
- 仓库多了 200 个左右的小 JSON, 每次爬取有变化的番会多出一个文件的 diff; 流水线 `git add -A -- data/` 已经覆盖它们。
- 前端以后在列表页用到新字段时, 要确认 `ANiMeIndex.json` 里有, 没有就在 `split.py` 里放开。

## Alternatives considered

- **什么都不做 / 复用现有全量文件**: 最省事, 只靠前端缓存也能让第二次打开变快 (前端已经按 tag 存进 Cache Storage)。但首次打开和每次换 tag 之后依旧要下载 15 MB, 用户要的就是这个场景变快, 所以不够。
- **直接把 ANiMeRecord.json 改成紧凑输出**: 体积能从 15 MB 降到约 9 MB, 但 gzip 后几乎不变 (2.9 MB 对 2.6 MB), 而且失去逐行 diff, 流水线里的 jq 分析和人工审 PR 都更难读。
- **把角色单独拆文件, 列表仍用全量**: 能省角色那部分, 但每集简介仍在列表数据里, 详情页还是要拉全部番。
- **由前端或 CF Worker 运行时按 id 切片**: 不用改数据仓库, 但 jsDelivr 是静态文件 CDN, 做不到; CF Worker 节点对这条路径返回 403, 依赖它会让 jsDelivr 用户拿不到好处。
