import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
    duration: int = 2000
    audio_file: str = ""


@dataclass
class SubjectAudioOverride:
    subject: str = ""
    audio_file: str = ""
    volume: Optional[int] = None
    states: Optional[List[int]] = None


@dataclass
class QuietPeriod:
    """静音时段。

    start/end 为 "HH:MM"，精度到分钟；days 为空表示每天生效。
    暂不支持跨天时段（start > end 会被视为非法配置）。
    """

    start: str = ""
    end: str = ""
    days: List[int] = field(default_factory=list)
    name: str = ""


class CustomNotificationManager:
    def __init__(self) -> None:
        self._file_path = CONFIG_HOME / "custom_notifications.json"
        self._notifications: List[CustomNotification] = []
        self._subject_overrides: List[SubjectAudioOverride] = []
        self._sent_today: Dict[str, str] = {}
        self._last_mtime: float = 0
        # 静音时段
        self._quiet_enabled: bool = False
        self._quiet_periods: List[QuietPeriod] = []
        # 保留配置文件中的其它字段，避免保存时把用户手写的键冲掉
        self._raw_data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self._file_path.exists():
            self._notifications = []
            self._quiet_enabled = False
            self._quiet_periods = []
            self._raw_data = {}
            self._save()
            logger.info("已创建空的 custom_notifications.json")
            return
        try:
            self._last_mtime = os.path.getmtime(self._file_path)
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
            self._raw_data = data if isinstance(data, dict) else {}
            # 旧版配置没有 quiet_hours 键：加载完成后补写一次，把文件升级到新格式
            format_upgrade = "quiet_hours" not in self._raw_data

            raw = data.get("notifications", [])
            self._notifications = []
            time_migrated = 0
            known_fields = {f.name for f in fields(CustomNotification)}
            for item in raw:
                if not item.get("id") or item["id"] == "auto":
                    item["id"] = str(uuid.uuid4())
                if isinstance(item.get("enabled"), bool):
                    item["enabled"] = 1 if item["enabled"] else 0
                # 旧版配置的时间精确到秒（HH:MM:SS），统一规范化为分钟级（HH:MM）
                normalized_time, changed = self._normalize_minute_time(item.get("time", ""))
                if changed:
                    time_migrated += 1
                    logger.info(
                        f"自定义通知时间已迁移到分钟精度: "
                        f"{item.get('time')!r} -> {normalized_time!r} "
                        f"({item.get('name', '未命名')})"
                    )
                item["time"] = normalized_time
                # 忽略未知字段，避免用户手写的额外键导致整份通知加载失败
                unknown = set(item) - known_fields
                if unknown:
                    logger.warning(
                        f"自定义通知 '{item.get('name', '未命名')}' 含未知字段 "
                        f"{sorted(unknown)}，加载时已忽略"
                    )
                self._notifications.append(
                    CustomNotification(**{k: v for k, v in item.items() if k in known_fields})
                )

            raw_ov = data.get("subject_audio_overrides", [])
            self._subject_overrides = []
            for s in raw_ov:
                s["subject"] = s.get("subject", "").strip()
                self._subject_overrides.append(SubjectAudioOverride(**s))

            self._load_quiet_hours(data.get("quiet_hours", {}))

            logger.info(
                f"已加载 {len(self._notifications)} 条自定义通知, "
                f"{len(self._subject_overrides)} 条科目音频覆盖, "
                f"{len(self._quiet_periods)} 段静音时段"
                f"（{'已启用' if self._quiet_enabled else '未启用'}）"
            )

            if time_migrated:
                logger.info(f"检测到 {time_migrated} 条通知时间需要迁移到分钟精度，正在写回配置")
            if format_upgrade:
                logger.info("配置缺少 quiet_hours 段落，正在升级配置格式")
            if time_migrated or format_upgrade:
                self._save()
        except Exception as e:
            logger.error(f"加载自定义通知失败: {e}")
            self._notifications = []
            self._subject_overrides = []
            self._quiet_enabled = False
            self._quiet_periods = []

    def _load_quiet_hours(self, raw: Any) -> None:
        """解析静音时段配置；解析失败时降级为不静音，不影响通知功能。"""
        self._quiet_enabled = False
        self._quiet_periods = []
        try:
            if not isinstance(raw, dict):
                return
            self._quiet_enabled = bool(raw.get("enabled", False))
            raw_periods = raw.get("periods", [])
            if not isinstance(raw_periods, list):
                logger.warning("quiet_hours.periods 不是数组，已忽略")
                return

            periods: List[QuietPeriod] = []
            for item in raw_periods:
                if not isinstance(item, dict):
                    continue
                start = str(item.get("start", "")).strip()
                end = str(item.get("end", "")).strip()
                start_sec = self._parse_hhmm(start)
                end_sec = self._parse_hhmm(end)
                if start_sec is None or end_sec is None:
                    logger.warning(f"静音时段格式无效（需 HH:MM），已跳过: {item!r}")
                    continue
                if start_sec >= end_sec:
                    logger.warning(
                        f"静音时段 {start}~{end} 不是有效区间（暂不支持跨天），已跳过"
                    )
                    continue

                days: List[int] = []
                raw_days = item.get("days", [])
                if isinstance(raw_days, list):
                    for d in raw_days:
                        try:
                            day_index = int(d)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= day_index <= 6:
                            days.append(day_index)

                periods.append(
                    QuietPeriod(
                        start=start,
                        end=end,
                        days=days,
                        name=str(item.get("name", "")).strip(),
                    )
                )
            self._quiet_periods = periods
        except Exception as e:
            logger.error(f"解析静音时段配置失败，本次不启用静音: {e}")
            self._quiet_enabled = False
            self._quiet_periods = []

    @staticmethod
    def _normalize_minute_time(value: Any) -> Tuple[str, bool]:
        """把通知时间规范化为分钟精度（HH:MM）。

        旧版配置的时间精确到秒（HH:MM:SS），这里截断秒部分，
        例如 "12:35:30" -> "12:35"（属于 12:35 这一分钟）。

        Args:
            value: 原始时间字符串。

        Returns:
            (规范化后的时间字符串, 是否发生了变更)
        """
        text = str(value or "").strip()
        if not text:
            return text, False
        parts = text.split(":")
        if len(parts) < 2:
            return text, False
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except (TypeError, ValueError):
            return text, False
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return text, False
        normalized = f"{hour:02d}:{minute:02d}"
        return normalized, normalized != text

    @staticmethod
    def _parse_hhmm(value: str) -> Optional[int]:
        """把 "HH:MM" 解析为当天秒数；精度限制在分钟，忽略可能的秒部分。"""
        if not value:
            return None
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 3600 + minute * 60

    def _auto_reload(self) -> None:
        try:
            if self._file_path.exists() and os.path.getmtime(self._file_path) != self._last_mtime:
                self._load()
        except Exception:
            pass

    def _save(self) -> None:
        try:
            # 以读入的原始配置为基底，保留用户手写的其它字段，
            # 避免保存时把 quiet_hours 之外的未知键冲掉。
            data: Dict[str, Any] = dict(getattr(self, "_raw_data", {}) or {})
            data["quiet_hours"] = {
                "enabled": self._quiet_enabled,
                "periods": [asdict(p) for p in self._quiet_periods],
            }
            data["subject_audio_overrides"] = [asdict(s) for s in self._subject_overrides]
            data["notifications"] = [asdict(n) for n in self._notifications]
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._raw_data = data
            # 自己写盘后同步 mtime，避免下一次 _auto_reload 误判为外部修改而重复加载
            try:
                self._last_mtime = os.path.getmtime(self._file_path)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"保存自定义通知失败: {e}")

    def is_quiet_now(self, now: Optional[datetime] = None) -> bool:
        """当前是否处于静音时段。

        时间基准为软件内时间（含 NTP 校正与时差偏移），
        与铃声/课程判定口径保持一致，不直接使用 datetime.now()。

        时段起点提前 1 秒生效：例如设定 12:40 且时差偏移为 0 时，
        软件时间 12:39:59 即开始静音；时差偏移 5 秒时则对应真实时间 12:40:04。
        精度限制在分钟（秒一律视为 0）。

        Args:
            now: 可选的判定时间，便于测试；缺省取软件内当前时间。

        Returns:
            bool: True 表示当前应静音。
        """
        if not self._quiet_enabled or not self._quiet_periods:
            return False

        if now is None:
            now = TimeManagerFactory.get_instance().get_current_time()

        weekday = now.weekday()
        current_sec = now.hour * 3600 + now.minute * 60 + now.second

        for period in self._quiet_periods:
            if period.days and weekday not in period.days:
                continue
            start = self._parse_hhmm(period.start)
            end = self._parse_hhmm(period.end)
            if start is None or end is None or start >= end:
                continue
            # 起点提前 1 秒；00:00 起点无需也无法再提前
            start_adj = start - 1 if start > 0 else 0
            if start_adj <= current_sec < end:
                return True
        return False

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
            logger.debug(f"科目音频覆盖匹配: {subject_name} (states={s.states})")
            return s
        return None

    def check_and_notify(self, next_lessons: Optional[List[str]] = None) -> None:
        self._auto_reload()

        now = TimeManagerFactory.get_instance().get_current_time()
        today_date = now.strftime("%Y-%m-%d")
        weekday = now.weekday()
        # 时间精度到分钟：同一分钟内仅触发一次（由 _sent_today 去重保证）
        time_str = now.strftime("%H:%M")

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
            logger.debug(f"自定义通知音频解析: {item.audio_file!r} → {audio!r}")

            lesson_name = item.name
            if not lesson_name and item.state in (1, 3) and next_lessons:
                lesson_name = next_lessons[0]
                logger.info(f"自定义通知自动补全课程名: {lesson_name}")

            push_notification(
                state=item.state,
                lesson_name=lesson_name,
                title=item.title,
                subtitle=item.subtitle,
                content=lesson_name,
                duration=item.duration,
                audio_file=audio,
            )

            self._sent_today[item.id] = today_date
            extras = []
            if item.duration != 2000:
                extras.append(f"duration={item.duration}")
            if lesson_name != item.name:
                extras.append(f"lesson_name={lesson_name}")
            extras_str = f" ({', '.join(extras)})" if extras else ""
            logger.info(f"自定义通知触发: {item.name} (state={item.state}, time={time_str}){extras_str}")

            if item.enabled == 2:
                item.enabled = 0
                self._save()
                logger.info(f"一次性通知 {item.name} 已自动关闭")

    def _clean_sent_cache(self, today_date: str) -> None:
        expired = [k for k, v in self._sent_today.items() if v != today_date]
        for k in expired:
            del self._sent_today[k]


custom_notification_manager = CustomNotificationManager()

# 向音频层注册静音判定回调：所有音频播放（铃声 / TTS / 插件）都会先询问一次。
# 放在实例创建之后注册，避免与 play_audio 产生循环导入。
try:
    from play_audio import audio_manager as _audio_manager

    _audio_manager.set_mute_checker(custom_notification_manager.is_quiet_now)
except Exception as e:  # pragma: no cover - 仅在异常环境中触发
    logger.warning(f"注册静音时段判定失败，静音功能将不可用: {e}")
