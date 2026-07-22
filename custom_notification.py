import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from loguru import logger

from basic_dirs import CONFIG_HOME
from file import config_center
from tip_toast import push_notification
from utils import TimeManagerFactory


@dataclass
class CustomNotification:
    id: str
    enabled: int = 1
    name: str = ""
    days_of_week: List[int] = field(default_factory=list)
    time: str = ""
    state: int = 4
    title: str = ""
    subtitle: str = ""
    audio_file: str = ""


@dataclass
class SubjectAudioOverride:
    subject: str = ""
    audio_file: str = ""
    volume: Optional[int] = None
    states: Optional[List[int]] = None


class CustomNotificationManager:
    def __init__(self) -> None:
        self._file_path = CONFIG_HOME / "custom_notifications.json"
        self._notifications: List[CustomNotification] = []
        self._subject_overrides: List[SubjectAudioOverride] = []
        self._sent_today: Dict[str, str] = {}
        self._last_mtime: float = 0
        self._load()

    def _load(self) -> None:
        if not self._file_path.exists():
            self._notifications = []
            self._save()
            logger.info("已创建空的 custom_notifications.json")
            return
        try:
            self._last_mtime = os.path.getmtime(self._file_path)
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("notifications", [])
            self._notifications = []
            for item in raw:
                if not item.get("id"):
                    item["id"] = str(uuid.uuid4())
                if isinstance(item.get("enabled"), bool):
                    item["enabled"] = 1 if item["enabled"] else 0
                self._notifications.append(CustomNotification(**item))

            raw_ov = data.get("subject_audio_overrides", [])
            self._subject_overrides = []
            for s in raw_ov:
                s["subject"] = s.get("subject", "").strip()
                self._subject_overrides.append(SubjectAudioOverride(**s))

            logger.info(f"已加载 {len(self._notifications)} 条自定义通知, {len(self._subject_overrides)} 条科目音频覆盖")
        except Exception as e:
            logger.error(f"加载自定义通知失败: {e}")
            self._notifications = []
            self._subject_overrides = []

    def _auto_reload(self) -> None:
        try:
            if self._file_path.exists() and os.path.getmtime(self._file_path) != self._last_mtime:
                self._load()
        except Exception:
            pass

    def _save(self) -> None:
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "subject_audio_overrides": [asdict(s) for s in self._subject_overrides],
                        "notifications": [asdict(n) for n in self._notifications],
                    },
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

    def get_subject_audio(self, subject_name: str, state: int = -1) -> Optional[SubjectAudioOverride]:
        subject_name = subject_name.strip()
        for s in self._subject_overrides:
            if s.subject != subject_name:
                continue
            if not s.states or state not in s.states:
                continue
            return s
        return None

    def check_and_notify(self, next_lessons: Optional[List[str]] = None) -> None:
        self._auto_reload()

        now = TimeManagerFactory.get_instance().get_current_time()
        today_date = now.strftime("%Y-%m-%d")
        weekday = now.weekday()
        time_str = now.strftime("%H:%M:%S")

        self._clean_sent_cache(today_date)

        audio_defaults = {
            0: config_center.read_conf("Audio", "finish_class"),
            1: config_center.read_conf("Audio", "attend_class"),
            2: config_center.read_conf("Audio", "finish_class"),
            3: config_center.read_conf("Audio", "prepare_class"),
            4: config_center.read_conf("Audio", "other"),
        }

        for item in self._notifications:
            if item.enabled == 0:
                continue
            if weekday not in item.days_of_week:
                continue
            if item.time != time_str:
                continue
            if self._sent_today.get(item.id) == today_date:
                continue

            if item.audio_file == "default":
                audio = audio_defaults.get(item.state, "")
            elif item.audio_file:
                audio = item.audio_file
            else:
                audio = ""

            lesson_name = item.name
            if not lesson_name and item.state in (1, 3) and next_lessons:
                lesson_name = next_lessons[0]

            push_notification(
                state=item.state,
                lesson_name=lesson_name,
                title=item.title,
                subtitle=item.subtitle,
                content=lesson_name,
                audio_file=audio,
            )

            self._sent_today[item.id] = today_date
            logger.info(f"自定义通知触发: {item.name} (state={item.state}, time={time_str})")

            if item.enabled == 2:
                item.enabled = 0
                self._save()
                logger.info(f"一次性通知 {item.name} 已自动关闭")

    def _clean_sent_cache(self, today_date: str) -> None:
        expired = [k for k, v in self._sent_today.items() if v != today_date]
        for k in expired:
            del self._sent_today[k]


custom_notification_manager = CustomNotificationManager()
