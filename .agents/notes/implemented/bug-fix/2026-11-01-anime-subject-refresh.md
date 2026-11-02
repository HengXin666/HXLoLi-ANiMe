# Agent Note: 已有番剧条目按闸门刷新, 先拉后换

Status: implemented

Decision-ID: anime-subject-refresh

## Code

- `scripts/crawler/Api.py`

## Problem

爬虫每次运行时, 对已经在 ANiMeRecord.json 里的番剧只有两种处理: 要么完全重新拉一遍 (角色 + 关联 + 剧集 + 条目信息), 要么完全跳过。两种都不对。

完全重拉的问题: 每次运行都要为 160 部番逐个请求 Bangumi 的 characters / relations / episodes 三个接口, 加上条目详情一共四个。按限速 119 req/min 算, 一轮下来要跑十几分钟, 而且绝大多数番剧上周和这周的数据一模一样。

完全跳过的问题: 番剧数据会烂掉。正在追的番每周更新剧集, 不加就会显示只有 3 集; 新番的 eps 字段一开始是 0, 拿到之后不会自己补; 有的番 Bangumi 侧改了 eps 而本地还留着旧值。

更隐蔽的是**半截覆盖**: 原来的 `_refresh_anime_full` 先 `clear()` 掉 characters / relations / episodes, 然后逐个重新拉。如果中间任何一个请求失败 (限流、超时、5xx), 那几个字段就**永久空着** —— 因为下一次运行会认为这条记录"已经有数据了"从而跳过, 除非手工删掉整条重来。这等于一次网络抖动毁掉一条数据。

还有一个副作用: `updated_at` 变化会触发完整的重新拉取, 但 `_get_subjects_info` 也在这条路径上, 所以用户只是改了收藏标签, 也会连带把角色和剧集全重拉一遍。

## Decision

**用闸门决定要不要完整刷新, 并且始终坚持先拉到临时变量、成功才替换。**

闸门 `_needs_full_refresh(record, subject)` 判定四个条件, 任一成立才完整刷新:

- 观看状态是未完结 (`WANT_TO_WATCH` / `WATCHING` / `ON_HOLD`) —— 这些番随时在更新
- `eps` 或 `total_episodes` 为 0 —— 数据不全, 之前可能失败过
- 本地 `episodes` 数量少于 `eps` —— 剧集没抓全
- 线上 `subject.eps` 与本地 `data.eps` 不一致 —— Bangumi 侧改了

闸门在同步轻量字段**之前**判定。这个顺序是必须的: `_sync_subject_brief` 会把线上的 eps 写进本地, 如果先同步再判定, 第四个条件就永远不成立。

**轻量字段走零额外请求的路径。** `_sync_subject_brief` 直接从用户收藏列表里自带的 `subject` 摘要取 name / name_cn / short_summary / score / eps / date / image_url。收藏列表本来就要拉一次, 这些字段是白送的, 不需要为它们单独请求。

**先拉后换, 失败保留旧值。** `_refresh_anime_full` 改成:

```python
characters = self._get_anime_characters(idx)
if characters is not None:
    record.anime_data.characters = characters
```

三个抓取函数在失败时返回 `None`, 只有成功才赋值。这样一次网络抖动最多让这条记录保持原样, 不会把字段清空。

`_get_subjects_info` 同样改成分两步: 先把所有字段解析进一个 `fields` 字典, 全部拿到之后再逐个 `setattr`。原来是边解析边写, 中途抛 `KeyError` 会留下写了一半的记录。

同时补抓了 `score` (从 `rating.score`) 和 `image_url` (从 `images.large`) —— 这两个之前根本没写进记录。

## Consequences

- 一轮完整运行的请求数从"每部番四次"降到"每部番零到一次"。用户只改收藏标签时不再触发角色/剧集重拉。
- 闸门的代价是: 已完结且数据完整的番, 只有 Bangumi 侧改了 eps 才会被刷新。如果 Bangumi 改了别的不影响 eps 的字段 (比如简介), 本地不会跟进 —— 因为轻量字段走 `_sync_subject_brief`, 它只覆盖 name / name_cn / short_summary / score / eps / date / image_url 那几项。
- `_get_anime_characters` / `_get_relations` / `_get_episodes` 现在必须返回 `None` 表示失败, 不能返回空列表。空列表会让 `if x is not None` 成立, 从而把"抓取失败"写成"这部番没有角色"。
- 一次网络抖动最多让某条记录保持原样, 不会留下空字段。但这也意味着失败是静默的 —— 日志里有 `获取番剧信息失败` 但要人去看。

## Alternatives considered

- **什么都不做, 保持每次全量重拉**: 正确性没问题, 但每轮十几分钟全花在没变化的番上, 且限流风险随番剧数量线性增长。番剧破百之后这条路已经不可持续。
- **什么都不做, 保持每次跳过已有记录**: 更快, 但数据会持续腐烂 —— 在追的番永远停在第一次抓到的集数。这正是当初加 `last_update` 判定的原因, 只是那个判定条件太粗 (只用户侧变化)。
- **只在 `updated_at` 变化时才刷新**: 复用现有逻辑, 改动最小。但 `updated_at` 只反映用户侧 (改了标签/评分), 完全不反映 Bangumi 侧的数据变化。新番补 eps 这种情况它永远检测不到。
- **引入缓存表记录每个接口的最后抓取时间**: 能精确控制刷新频率, 但要多维护一份状态, 且状态本身会与 ANiMeRecord.json 不一致。闸门用记录自身的字段推断, 没有额外状态。
- **失败时重试几次再放弃**: 能提高成功率, 但解决不了"重试全部失败之后字段已经被 clear 掉"这个根本问题。先拉后换之后, 重试变成一个纯优化项, 不是正确性依赖。
- **用 `total_episodes` 判断是否完结**: 比状态判断更"客观", 但 Bangumi 的 `total_episodes` 对未完结番也是 0, 与 `eps` 为 0 无法区分, 所以两个都要看。
