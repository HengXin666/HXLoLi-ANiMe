import argparse
import os
from queue import Queue
import threading
from typing import Callable, Dict, List, Tuple
import requests
import json
import time

from ANiMeType import (
    ANiMeData,
    ANiMeDecoder,
    ANiMeEncoder,
    ANiMeRecord,
    Actor,
    Character,
    Episode,
    KeyValue,
    Relation,
    SubjectType,
    UserStatus,
    WatchStatus,
)
from ApiReqRateLimiter import ApiReqRateLimiter

# --- 配置 ---
# 替换为你的 Bangumi 用户名
USERNAME = "heng_xin"
# 替换为你的 Access Token (在 https://next.bgm.tv/demo/access-token 生成)
ACCESS_TOKEN = ""
# API 的基础 URL
BASE_URL = "https://api.bgm.tv"
# 数据存储路径 (相对于仓库根目录)
DATA_ROOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

# --- 请求头 ---
# 强烈建议设置 User-Agent, 这是 API 文档的要求
# 格式: App-Name/Version (your-github-url or your-email)
HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "User-Agent": "HXLoLi/1.0 (https://github.com/HengXin666/HXLoLi)",
    "accept": "application/json",
}

# 带请求频率限制的 api 请求
apiReq = ApiReqRateLimiter(119)

# 未完结的收藏状态: 这些番每次运行都完整刷新
UNFINISHED_WATCH_STATUS = (
    WatchStatus.WANT_TO_WATCH,
    WatchStatus.WATCHING,
    WatchStatus.ON_HOLD,
)


class Api:
    def __init__(
        self, anime_record: List[ANiMeRecord] = [], actor_map: Dict[int, Actor] = {}
    ) -> None:
        self._anime_record: List[ANiMeRecord] = anime_record  # 番剧记录
        self._actor_map: Dict[int, Actor] = actor_map  # 声优信息

        self._record_map: Dict[int, ANiMeRecord] = {}  # 番剧记录索引映射
        for record in self._anime_record:
            # 浅拷贝
            self._record_map[record.anime_data.id] = record

    @staticmethod
    def _get_img_and_save(id: int, type: str, url: str) -> bool:
        if url == "":
            return False
        save_path = f"{DATA_ROOT_PATH}/{type}/{id}.jpg"
        if not os.path.exists(f"{DATA_ROOT_PATH}/{type}"):
            os.makedirs(f"{DATA_ROOT_PATH}/{type}")
        if os.path.exists(save_path):
            return False
        try:
            response = apiReq.get(url, headers=HEADERS)
            response.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(response.content)
            return True
        except requests.exceptions.RequestException as e:
            print(f"获取图片失败: {e}")
        except json.JSONDecodeError:
            print("解析图片响应失败, 内容可能不是有效的JSON。")
            print("响应内容:", response.text)
        return False

    def requires(self, username: str, lambda_func: Callable = lambda: None) -> None:
        def _task(idx: int) -> None:
            print(f"{idx}: {self._anime_record[idx].anime_data.name}")
            self._refresh_anime_full(idx)
            lambda_func()

        self._get_user_watching_anime(username, _task)

    def _refresh_anime_full(self, idx: int) -> None:
        """完整刷新一条记录: 先把各接口拉到临时列表, 成功才替换, 失败保留旧数据"""
        record = self._anime_record[idx]
        self._get_subjects_info(record.anime_data.id, record)
        characters = self._get_anime_characters(idx)
        if characters is not None:
            record.anime_data.characters = characters
        relations = self._get_relations(idx)
        if relations is not None:
            record.anime_data.relations = relations
        episodes = self._get_episodes(idx)
        if episodes is not None:
            record.anime_data.episodes = episodes

    def download_all_img(self) -> None:
        def _download_img_thread(queue: Queue[Tuple[str, int, str]]) -> None:
            cnt: int = 0
            while not queue.empty():
                type, id, url = queue.get()
                is_ok = self._get_img_and_save(id, type, url)
                cnt += is_ok
                if is_ok:
                    print(f"[{threading.current_thread().name}]: 已下载 {id} 图片")
                    if cnt % 100 == 0:
                        print(
                            f"[{threading.current_thread().name}]: 已下载 {cnt} 张图片"
                        )
                        time.sleep(3)
                queue.task_done()

        # 基于线程安全的队列 <type, id, url> 开启多线程爬虫
        queue: Queue[Tuple[str, int, str]] = Queue()
        for record in self._anime_record:
            queue.put(("anime", record.anime_data.id, record.anime_data.image_url))
            for kyara in record.anime_data.characters:
                queue.put(("kyara", kyara.id, kyara.image_url))
            for relation in record.anime_data.relations:
                queue.put(("relation", relation.id, relation.image_url))
        for cv in self._actor_map.values():
            queue.put(("cv", cv.id, cv.image_url))

        for _ in range(8):
            threading.Thread(target=_download_img_thread, args=(queue,)).start()

        print("主线程: 等待所有下载任务完成...")
        queue.join()
        print("主线程: 所有图片下载任务已完成! ")

    def _get_subjects_info(self, id: int, record_ref: ANiMeRecord) -> None:
        url = f"{BASE_URL}/v0/subjects/{id}"
        response = None
        try:
            response = apiReq.get(url, headers=HEADERS)
            response.raise_for_status()
            # 补充更新: 全部解析成功后再写回, 避免半截覆盖
            data = response.json()
            fields = {
                "name": data["name"],
                "name_cn": data["name_cn"],
                "summary": data["summary"],
                "eps": data["eps"],
                "total_episodes": data["total_episodes"],
                "date": data["date"],
            }
            score = (data.get("rating") or {}).get("score")
            if score is not None:
                fields["score"] = score
            image_url = (data.get("images") or {}).get("large")
            if image_url:
                fields["image_url"] = image_url
            for attr, value in fields.items():
                if value is not None:
                    setattr(record_ref.anime_data, attr, value)
        except requests.exceptions.RequestException as e:
            try:
                description: str = response.json().get("description", "")
            except Exception:
                description = ""
            if description.startswith("offset"):
                return
            print(f"获取番剧信息失败: {e}")
        except KeyError as e:
            print(f"番剧 {id} 信息缺少字段: {e}")
        except json.JSONDecodeError:
            print("解析番剧信息响应失败, 内容可能不是有效的JSON。")
            print("响应内容:", response.text)

    @staticmethod
    def _sync_subject_brief(record: ANiMeRecord, subject: dict) -> None:
        """用收藏列表自带的 subject 摘要同步已有记录的轻量字段 (零额外请求)"""
        data = record.anime_data
        for attr, key in (
            ("name", "name"),
            ("name_cn", "name_cn"),
            ("short_summary", "short_summary"),
            ("score", "score"),
            ("eps", "eps"),
            ("date", "date"),
        ):
            value = subject.get(key)
            if value is not None:
                setattr(data, attr, value)
        image_url = (subject.get("images") or {}).get("large")
        if image_url:
            data.image_url = image_url

    # Agent Notes: 已有番剧的条目级数据按闸门刷新, 先拉后换
    # .agents/notes/implemented/bug-fix/2026-11-01-anime-subject-refresh.md
    @staticmethod
    def _needs_full_refresh(record: ANiMeRecord, subject: dict) -> bool:
        """已有记录是否需要完整刷新 (条目信息 + 角色 + 关联 + 剧集)
        用户收藏 updated_at 变化由调用方单独判定, 这里只看番剧侧:
        - 未完结状态: 想看 / 在看 / 搁置
        - 数据不全: eps 或 total_episodes 为 0, 剧集数少于 eps, 或本地 eps 与线上不一致
        """
        data = record.anime_data
        status = record.user_status.watch_status
        if not isinstance(status, WatchStatus):
            status = WatchStatus(status)
        if status in UNFINISHED_WATCH_STATUS:
            return True
        if not data.eps or not data.total_episodes:
            return True
        if len(data.episodes) < data.eps:
            return True
        subject_eps = subject.get("eps")
        return subject_eps is not None and subject_eps != data.eps

    def _get_user_watching_anime(self, username: str, lambda_func: Callable) -> None:
        url = f"{BASE_URL}/v0/users/{username}/collections"
        limit = 100
        offset = 0

        while True:
            params = {
                "subject_type": 2,  # 仅请求动画
                "limit": limit,  # 限制返回的条数
                "offset": offset,  # 偏移量
            }
            try:
                response = apiReq.get(url, headers=HEADERS, params=params)
                response.raise_for_status()
                data = response.json().get("data", [])

                for it in data:
                    if int(it["subject_id"]) in self._record_map:
                        ref = self._record_map[int(it["subject_id"])]
                        user_changed = ref.user_status.last_update != it["updated_at"]
                        if ref.user_status.watch_status != WatchStatus(it["type"]):
                            ref.user_status.watch_status = WatchStatus(it["type"])

                        if ref.user_status.watched_eps != it["ep_status"]:
                            ref.user_status.watched_eps = it["ep_status"]

                        if ref.user_status.comment != it["comment"]:
                            ref.user_status.comment = it["comment"]

                        if ref.user_status.tags != it["tags"]:
                            ref.user_status.tags = it["tags"]

                        # 闸门在同步轻量字段之前判定, 才能看出 eps 是否与线上不一致
                        need_full = user_changed or self._needs_full_refresh(
                            ref, it["subject"]
                        )
                        self._sync_subject_brief(ref, it["subject"])

                        if user_changed:
                            ref.user_status.last_update = it["updated_at"]
                            print(f"更新: {ref.anime_data.name_cn} 的用户数据")
                        if need_full:
                            print(f"刷新: {ref.anime_data.name_cn} 的番剧数据")
                            idx = -1
                            for i in range(len(self._anime_record)):
                                if (
                                    self._anime_record[i].anime_data.id
                                    == ref.anime_data.id
                                ):
                                    idx = i
                                    break
                            if idx == -1:
                                print("严重错误: 已经存在但找不到数据")  # 不可能
                                exit(2233)
                            lambda_func(idx)
                        continue

                    # 新增记录
                    record = ANiMeRecord(
                        ANiMeData(
                            id=it["subject_id"],
                            name=it["subject"]["name"],
                            name_cn=it["subject"]["name_cn"],
                            short_summary=it["subject"]["short_summary"],
                            summary="",
                            score=it["subject"]["score"],
                            image_url=it["subject"]["images"]["large"],
                            eps=it["subject"]["eps"],
                            total_episodes=it["subject"]["eps"],
                            date=it["subject"]["date"],
                        ),
                        UserStatus(
                            watch_status=it["type"],
                            watched_eps=it["ep_status"],
                            last_update=it["updated_at"],
                            comment=it["comment"],
                            tags=it["tags"],
                        ),
                    )
                    # 条目详情由 lambda_func -> _refresh_anime_full 统一拉取
                    self._record_map[record.anime_data.id] = record
                    self._anime_record.append(record)
                    lambda_func(len(self._anime_record) - 1)

                offset += limit

            except requests.exceptions.RequestException as e:
                try:
                    description: str = response.json().get("description", "")
                except Exception:
                    description = ""
                if description.startswith("offset"):
                    return
                print(f"获取用户追番列表失败: {e}")
                return  # 非 offset 错误 (如 401 Unauthorized) 直接退出, 避免死循环
            except json.JSONDecodeError:
                print("解析追番列表响应失败, 内容可能不是有效的JSON。")
                print("响应内容:", response.text)
                return  # 解析失败也退出, 避免死循环

    def _get_anime_kyara_data(self, kyara_ref: Character) -> bool:
        """获取角色详细信息, 返回是否成功"""
        url = f"{BASE_URL}/v0/characters/{kyara_ref.id}"
        try:
            response = apiReq.get(url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()
            kyara_ref.summary = data["summary"]
            kyara_ref.birth_year = data.get("birth_year", None)
            kyara_ref.birth_month = data.get("birth_mon", None)
            kyara_ref.birth_day = data.get("birth_day", None)
            for kv in data["infobox"]:
                if kv["key"] == "简体中文名":
                    kyara_ref.name_cn = kv["value"]
                elif kv["key"] == "别名":
                    continue
                kyara_ref.tags.append(KeyValue(key=kv["key"], value=kv["value"]))
            return True
        except requests.exceptions.RequestException as e:
            print(f"获取角色信息失败: {e}")
        except json.JSONDecodeError:
            print("解析角色信息响应失败, 内容可能不是有效的JSON。")
            print("响应内容:", response.text)
        return False

    def _get_anime_characters(self, data_idx: int) -> List[Character] | None:
        """获取番剧角色声优信息
        Args:
            data_idx (int): _anime_record 中的索引
        Returns:
            List[Character] | None: 拉取成功返回新列表, 失败返回 None (调用方保留旧数据)
        """
        url = f"{BASE_URL}/v0/subjects/{self._anime_record[data_idx].anime_data.id}/characters"
        old_map: Dict[int, Character] = {
            c.id: c for c in self._anime_record[data_idx].anime_data.characters
        }
        characters: List[Character] = []
        try:
            response = apiReq.get(url, headers=HEADERS)
            response.raise_for_status()
            for it in response.json():
                character = Character(
                    id=it["id"],
                    name=it["name"],
                    relation=it["relation"],
                    image_url=it["images"]["large"],
                    actor_ids=[actor["id"] for actor in it["actors"]],
                )
                if not self._get_anime_kyara_data(character) and character.id in old_map:
                    # 角色详情拉取失败: 沿用旧详情, 不用空值覆盖
                    old = old_map[character.id]
                    character.name_cn = old.name_cn
                    character.summary = old.summary
                    character.birth_year = old.birth_year
                    character.birth_month = old.birth_month
                    character.birth_day = old.birth_day
                    character.tags = old.tags
                characters.append(character)

                # 记录角色的声优
                for actor in it["actors"]:
                    if actor["id"] not in self._actor_map:
                        actor_data = Actor(
                            id=actor["id"],
                            name=actor["name"],
                            short_summary=actor["short_summary"],
                            image_url=actor["images"]["large"],
                        )
                        self._actor_map[actor_data.id] = actor_data
            return characters
        except requests.exceptions.RequestException as e:
            print(
                f"获取番剧 {self._anime_record[data_idx].anime_data.id} 角色失败: {e}"
            )
        except json.JSONDecodeError:
            print(
                f"解析番剧 {self._anime_record[data_idx].anime_data.id} 角色响应失败。"
            )
            print("响应内容:", response.text)
        return None

    def _get_relations(self, data_idx: int) -> List[Relation] | None:
        """获取番剧关联信息, 失败返回 None"""
        url = f"{BASE_URL}/v0/subjects/{self._anime_record[data_idx].anime_data.id}/subjects"
        relations: List[Relation] = []
        try:
            response = apiReq.get(url, headers=HEADERS)
            response.raise_for_status()
            for it in response.json():
                relation = Relation(
                    id=it["id"],
                    name=it["name"],
                    name_cn=it["name_cn"],
                    relation=it["relation"],
                    type=SubjectType(it["type"]),
                    image_url=it["images"]["large"],
                )
                relations.append(relation)
            return relations
        except requests.exceptions.RequestException as e:
            print(
                f"获取番剧 {self._anime_record[data_idx].anime_data.id} 关联失败: {e}"
            )
        except json.JSONDecodeError:
            print(
                f"解析番剧 {self._anime_record[data_idx].anime_data.id} 关联响应失败。"
            )
            print("响应内容:", response.text)
        return None

    def _get_episodes(self, data_idx: int) -> List[Episode] | None:
        """获取番剧剧集信息, 全部分页成功才返回列表, 失败返回 None"""
        url = f"{BASE_URL}/v0/episodes"
        limit = 100
        offset = 0
        episodes: List[Episode] = []
        response = None
        try:
            while True:
                params = {
                    "subject_id": self._anime_record[data_idx].anime_data.id,
                    "limit": limit,
                    "offset": offset,
                }
                response = apiReq.get(url, headers=HEADERS, params=params)
                response.raise_for_status()
                data = response.json()["data"]

                if not data:
                    return episodes

                for it in data:
                    episode = Episode(
                        id=it["id"],
                        name=it["name"],
                        name_cn=it["name_cn"],
                        type=it["type"],
                        ep=it["ep"],
                        sort=it["sort"],
                        duration_seconds=it["duration_seconds"],
                        air_date=it["airdate"],
                        desc=it["desc"],
                    )
                    episodes.append(episode)

                offset += limit
        except requests.exceptions.RequestException as e:
            try:
                description: str = response.json().get("description", "")
            except Exception:
                description = ""
            if description.startswith("offset"):
                return episodes  # 越过最后一页, 已拉到的就是全部
            print(
                f"获取番剧 {self._anime_record[data_idx].anime_data.id} 剧集失败: {e}"
            )
        except json.JSONDecodeError:
            print(
                f"解析番剧 {self._anime_record[data_idx].anime_data.id} 剧集响应失败。"
            )
            print("响应内容:", response.text)
        return None


def load_from_json() -> Api:
    """从 json 文件中加载数据
    运行路径, 应该在 py/anime 下
    如果文件不存在, 则返回一个空的 Api 对象
    """
    if not os.path.exists(DATA_ROOT_PATH):
        os.makedirs(DATA_ROOT_PATH)
    try:
        # 番剧记录
        with open(f"{DATA_ROOT_PATH}/ANiMeRecord.json", "r", encoding="utf-8") as f:
            anime_record: List[ANiMeRecord] = json.load(f, cls=ANiMeDecoder)
    except Exception as e:
        print("Open File Err:", e)
        anime_record = []
    try:
        # 声优信息
        with open(f"{DATA_ROOT_PATH}/Actor.json", "r", encoding="utf-8") as f:
            actor_list: List[Actor] = json.load(f, cls=ANiMeDecoder)
    except:
        actor_list = []
    actor_map: Dict[int, Actor] = {}
    for actor in actor_list:
        actor_map[actor.id] = actor
    return Api(anime_record, actor_map)


def save_to_json(api: Api) -> None:
    """保存数据为 json 文件
        运行路径, 应该在 py/anime 下
    Args:
        api (Api): 数据
    """
    if not os.path.exists(DATA_ROOT_PATH):
        os.makedirs(DATA_ROOT_PATH)
    # 番剧记录
    with open(f"{DATA_ROOT_PATH}/ANiMeRecord.json", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                api._anime_record, cls=ANiMeEncoder, indent=4, ensure_ascii=False
            )
        )
    # 声优信息
    with open(f"{DATA_ROOT_PATH}/Actor.json", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                list(api._actor_map.values()),
                cls=ANiMeEncoder,
                indent=4,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    # 接受命令行参数
    ag = argparse.ArgumentParser()
    ag.add_argument(
        "-u", "--username", help="Bangumi 用户名", required=False, default="heng_xin"
    )
    ag.add_argument("-o", "--token", help="Bangumi Token", required=True)
    args = ag.parse_args()

    # 重新绑定
    USERNAME = args.username
    ACCESS_TOKEN = args.token
    HEADERS = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "User-Agent": "HXLoLi/1.0 (https://github.com/HengXin666/HXLoLi)",
        "accept": "application/json",
    }

    api = load_from_json()
    # 边爬取边输出为 json 文件
    api.requires(USERNAME, lambda: save_to_json(api))
    # 保存为 json 文件, 防止因为没有新增番剧而不更新
    save_to_json(api)
    # 下载图片
    api.download_all_img()
