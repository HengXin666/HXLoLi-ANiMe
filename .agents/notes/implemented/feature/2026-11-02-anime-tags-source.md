# Agent Note: 萌属性标签改多源加权生成

Status: implemented

Decision-ID: anime-tags-source

## Code

- `scripts/crawler/Tags.py`

## Problem

萌属性标签原本靠人工维护: 每部番的标签由人看一遍再手写进数据。这个办法在番剧数量上百之后失效了 —— 新番进来没人标就一直空着, 同一部番隔几个月再看可能标得不一样, 而且没有任何依据可查, 事后无法回答"这条标签凭什么打上"。

用户的原话是「你应该去找一下权威的呃数据源来做这个描点, 我是说 tag 的这个部分」, 并明确否掉了 Bangumi 自带的标签 (「他们 bgm 那个标注的也不行, 换一个」)。

问题在于: 中文语境下的「萝莉」这类萌属性, 没有单一权威源。BD 站点给的标签偏制作信息 (Studio / Source), 动画数据库给的偏题材 (Action / Comedy), 而「萝莉」这种角色维度的判定分散在各站, 且各站口径不一致。

## Decision

**多源加权, 各源只贡献自己擅长的信号, 任一源失败不出产。**

四个源, 分工明确:

- **AOD (anime-offline-database)** 是主标签源。它本身已经合并了 AniDB / AniList / Kitsu / anime-planet 四家的 tags, 覆盖最广, 许可证是 ODbL-1.0 + DbCL-1.0, 可商用可再分发。
- **bangumi-data** 只做 id 映射层: bgm subject id -> MAL / AniList / AniDB id。它的价值是桥, 不是标签本身。
- **AniList GraphQL** 提供 genres、tags[].rank、主角色年龄。AniList 的 tags 带 rank 值 (0-100), 可以用阈值筛, 比布尔式的 tag 存在与否更细。
- **本仓 ANiMeRecord.json 里的 Bangumi 角色数据** 提供萝莉的核心角色群信号: relation / 年龄 / 身高 / 简介。这是唯一的"角色个体"维度, 其他源给不了。

判定口径 (2026-11-02 用户逐条确认):

- 点兔两季 (`88287` / `260770`) 必须算萝莉番剧
- `舰娘` 这个 tag 整个移除, 不是隐藏, 是删掉
- `妹控` 保留, 依据取 AOD 的 `imouto` / `sister complex` / `brother-sister incest`
- 标签名用日语片假名 `ロリ`, 不用汉字「萝莉」; 历史数据靠 `LEGACY_TAG_ALIASES = {"萝莉": "ロリ"}` 归并

三个硬性约束:

- **任何一个源失败就 `sys.exit`, 保留旧的 ANiMeTags.json 不覆盖。** 残缺的标签数据比没有标签数据更糟, 因为它看起来是完整的。
- **产物是生成物, 不手改。** 要改就改 Tags.py 再重跑。
- **每条标签带 evidence 字段**, 记录命中来自哪个源。583 个标签 evidence 全非空 —— 这是"事后能回答凭什么"的实现。

实测结果: records 160 / covered 154 / unmapped 6。

## Consequences

- 新番进入 ANiMeRecord 后重新跑一次 Tags.py 就有标签, 不再需要人工逐部标注。代价是每次都要联网拉 AOD (62 MB) 和 bangumi-data, 全量跑一轮是分钟级。
- 标签口径变了改 Tags.py 再重跑, 不要手改 ANiMeTags.json。任何手改在下一次流水线运行时都会丢失。
- 标签名与源数据保持一致 (ロリ 而非「萝莉」)。前端展示要中文时做展示层映射, 不要在数据层翻译。
- 舰娘 被整个移除后, 历史上打过舰娘的记录会在下一次重跑时消失。旧数据要保留只能靠 LEGACY_TAG_ALIASES 这类归并, 不能靠隐藏。
- 任一源不可用时流水线整体不产新数据, 旧文件保留。这意味着 AOD 长期挂掉的话标签会停留在旧版本 —— 这是刻意的: 宁可旧, 不可残。
- evidence 字段让每条标签可追责, 代价是 ANiMeTags.json 体积比只存标签名大数倍 (113 KB / 160 部)。

## Alternatives considered

- **什么都不做, 继续手工维护**: 成本看似最低, 但新番永远缺标签, 而且标签一致性随维护者记忆漂移。放宽到"只标热门番"会让数据半残, 反而误导筛选。
- **复用 Bangumi 自带标签**: 一次请求就能拿到, 最省事, 但用户明确否掉 —— Bangumi 的标签是用户众包打的, 粒度粗且大量番剧标注意味着「有就标没有就不标」, 无法区分「不是萝莉」和「没人标过」。
- **单用 AniList**: 它的 tags 带 rank 值, 数据结构最友好。但 AniList 没有 `loli` 维度 (430 个 tag 里只有 `Kids` 和 `Primarily Child Cast`), 且对冷门番覆盖差, 单独用会漏掉大量。
- **单用本仓的角色数据**: 角色 data 最贴近判定口径, 但需要每部番都爬到完整的 characters 关系, 冷门番缺失严重, 且年龄字段大量为空。
- **多源取并集, 不做权重**: 实现最简单, 但各源精度差异大 —— AOD 的 `loli` 标签精度高, AniList 的 `Primarily Child Cast` 只是"主要角色是小孩"的近似。等权并集会让近似信号污染高精度信号。
- **标签名保留汉字「萝莉」**: 中文语境更直观, 但用户要求用日语片假名, 且源数据 (AOD) 本身就是日文标签体系, 保留原样能减少一次翻译映射。
