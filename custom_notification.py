import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger

from basic_dirs import CONFIG_HOME
from file import config_center
from tip_toast import push_notification
from utils import TimeManagerFactory


@dataclass
class CustomNotification:
    id: str
    enabled: bool = True
    name: str = ""
    days_of_week: List[int] = field(default_factory=list)
    time: str = ""
    state: int = 4
    title: str = ""
    subtitle: str = ""
    audio_file: str = ""


class CustomNotificationManager:
    def __init__(self) -> None:
        self._file_path = CONFIG_HOME / "custom_notifications.json"
        self._notifications: List[CustomNotification] = []
        self._sent_today: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._file_path.exists():
            self._notifications = []
            self._save()
            logger.info("已创建空的 custom_notifications.json")
            return
        try:
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
            self._notifications = [CustomNotification(**item) for item in data.get("notifications", [])]
            logger.info(f"已加载 {len(self._notifications)} 条自定义通知")
        except Exception as e:
            logger.error(f"加载自定义通知失败: {e}")
            self._notifications = []

    def _save(self) -> None:
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"notifications": [asdict(n) for n in self._notifications]},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"保存自定义通知失败: {e}")

    def get_all(self) -> List[CustomNotification]:
        return self._notifications

    def add(self, item: CustomNotification) -> None:
        if not item.id:
            item.id = str(uuid.uuid4())
        self._notifications.append(item)
        self._save()

    def update(self, item: CustomNotification) -> None:
        for i, n in enumerate(self._notifications):
            if n.id == item.id:
                self._notifications[i] = item
                self._save()
                return

    def delete(self, id_: str) -> None:
        self._notifications = [n for n in self._notifications if n.id != id_]
        self._save()

    def reload(self) -> None:
        self._load()

    def check_and_notify(self) -> None:
        now = TimeManagerFactory.get_instance().get_current_time()
        today_date = now.strftime("%Y-%m-%d")
        weekday = now.weekday()
        time_str = now.strftime("%H:%M")

        self._clean_sent_cache(today_date)

        audio_defaults = {
            0: config_center.read_conf("Audio", "finish_class"),
            1: config_center.read_conf("Audio", "attend_class"),
            2: config_center.read_conf("Audio", "finish_class"),
            3: config_center.read_conf("Audio", "prepare_class"),
            4: config_center.read_conf("Audio", "prepare_class"),
        }

        for item in self._notifications:
            if not item.enabled:
                continue
            if weekday not in item.days_of_week:
                continue
            if item.time != time_str:
                continue
            if self._sent_today.get(item.id) == today_date:
                continue

            audio = item.audio_file or audio_defaults.get(item.state, "")

            if item.state == 4:
                push_notification(
                    state=4,
                    title=item.title,
                    subtitle=item.subtitle,
                    content=item.name,
                    audio_file=audio,
                )
            else:
                push_notification(
                    state=item.state,
                    audio_file=audio,
                )

            self._sent_today[item.id] = today_date
            logger.info(f"自定义通知触发: {item.name} (state={item.state}, time={time_str})")

    def _clean_sent_cache(self, today_date: str) -> None:
        expired = [k for k, v in self._sent_today.items() if v != today_date]
        for k in expired:
            del self._sent_today[k]


custom_notification_manager = CustomNotificationManager()
