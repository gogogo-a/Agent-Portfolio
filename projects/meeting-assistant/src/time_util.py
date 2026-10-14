# 会议时间统一格式：周X HH:MM（如 周五 14:00）
# 由 build_meeting_zip 注入到各 code 节点，勿单独作为 Dify 节点。
import re

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日", "星期天", "星期日")
_CN_HOUR = {
    "零": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12,
}


def _extract_weekday(text: str) -> str:
    for d in WEEKDAYS:
        if d in text:
            return "周日" if d in ("星期天", "星期日") else d[:2]
    return ""


def _cn_to_int(token: str):
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _CN_HOUR:
        return _CN_HOUR[token]
    if token.startswith("十"):
        tail = token[1:]
        return 10 + (_CN_HOUR.get(tail, 0) if tail else 0)
    if "十" in token:
        parts = token.split("十", 1)
        tens = _CN_HOUR.get(parts[0], 0) if parts[0] else 1
        ones = _CN_HOUR.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


def _parse_hour_minute(text: str):
    s = text or ""
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})\s*点\s*(\d{1,2})?", s)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    m = re.search(r"([零一二两三四五六七八九十]+)\s*点\s*([零一二两三四五六七八九十]+)?", s)
    if m:
        h = _cn_to_int(m.group(1))
        mi = _cn_to_int(m.group(2)) if m.group(2) else 0
        if h is not None:
            return h, mi or 0
    return None


def _apply_daypart(text: str, hour: int) -> int:
    s = text or ""
    if ("下午" in s or "晚上" in s or "傍晚" in s) and hour < 12:
        return hour + 12
    if "上午" in s and hour == 12:
        return 0
    return hour


def normalize_time(text: str) -> str:
    """将自然语言时间规范为 周X HH:MM；无法解析时返回去空白原文。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"周[一二三四五六日天]\s+\d{1,2}:\d{2}", raw):
        day = _extract_weekday(raw)
        hm = _parse_hour_minute(raw)
        if day and hm:
            h, mi = hm
            return f"{day} {h:02d}:{mi:02d}"
        return raw
    day = _extract_weekday(raw)
    hm = _parse_hour_minute(raw)
    if hm is None:
        return raw
    h, mi = _apply_daypart(raw, hm[0]), hm[1]
    if day:
        return f"{day} {h:02d}:{mi:02d}"
    return f"{h:02d}:{mi:02d}"


def parse_hm(text: str):
    """解析规范时间，返回 (hour, minute) 或 None。"""
    norm = normalize_time(text)
    hm = _parse_hour_minute(norm)
    if hm is None:
        return None
    return hm[0], hm[1]


def normalize_duration(text: str, default: str = "1小时") -> str:
    """规范会议时长；空/null/无法解析 → default。"""
    raw = (text or "").strip()
    if not raw:
        return default
    if re.search(r"\d+\s*分钟", raw):
        m = re.search(r"(\d+)", raw)
        return f"{m.group(1)}分钟" if m else default
    if re.search(r"\d+\s*小时", raw) or re.search(r"\d+\s*h", raw, re.I):
        m = re.search(r"(\d+)", raw)
        return f"{m.group(1)}小时" if m else default
    if any(w in raw for w in ("半小时", "30分钟", "三十分钟")):
        return "30分钟"
    if any(w in raw for w in ("一个小时", "一小时", "1小时")):
        return "1小时"
    if any(w in raw for w in ("两个小时", "二小时", "两小时", "2小时")):
        return "2小时"
    m = re.search(r"(\d+)", raw)
    if m:
        n = int(m.group(1))
        return f"{n}分钟" if "分" in raw else f"{n}小时"
    return default


def duration_minutes(text: str, default_minutes: int = 60) -> int:
    """时长 → 分钟数（用于上班窗口计算）。"""
    norm = normalize_duration(text)
    m = re.search(r"(\d+)", norm)
    if not m:
        return default_minutes
    n = int(m.group(1))
    if "分钟" in norm:
        return max(n, 1)
    return max(n * 60, 1)


def normalize_draft(draft: dict) -> dict:
    """合并槽位后统一规范化 draft（时间、时长、空值）。"""
    if not isinstance(draft, dict):
        return {}
    out = dict(draft)
    if out.get("time"):
        out["time"] = normalize_time(str(out["time"]))
    out["duration"] = normalize_duration(out.get("duration"))
    for key in list(out.keys()):
        if out[key] is None or out[key] == "":
            if key != "topic":
                out.pop(key, None)
    if not out.get("duration"):
        out["duration"] = "1小时"
    return out
