# 上班时段规则 · meeting-assistant-design.md §2.3
import json

MORNING = (8, 30), (11, 20)
AFTERNOON = (13, 0), (17, 0)


def _in_window(sh, sm, eh, em):
    s, e = sh * 60 + sm, eh * 60 + em
    ms, me = MORNING[0][0] * 60 + MORNING[0][1], MORNING[1][0] * 60 + MORNING[1][1]
    a_s, a_e = AFTERNOON[0][0] * 60 + AFTERNOON[0][1], AFTERNOON[1][0] * 60 + AFTERNOON[1][1]
    return (ms <= s and e <= me) or (a_s <= s and e <= a_e)


def main(draft_json: str, flags_json: str = "", cache_json: str = "", flags_fallback: str = "", cache_fallback: str = "") -> dict:
    draft = normalize_draft(json.loads(draft_json or "{}"))
    flags = json.loads(flags_json or "{}") if flags_json and flags_json.strip() not in ("", "{}") else json.loads(flags_fallback or "{}")
    cache = json.loads(cache_json or "{}") if cache_json and cache_json.strip() not in ("", "{}") else json.loads(cache_fallback or "{}")
    t = draft.get("time", "")
    hm = parse_hm(t)
    if not hm:
        return {
            "work_valid": "非法",
            "reply_text": "无法识别会议时间，请重新说明时段（如「周五下午16点」）。",
            "draft_json": json.dumps(draft, ensure_ascii=False),
            "flags_json": json.dumps(flags, ensure_ascii=False) if flags else "{}",
            "cache_json": json.dumps(cache, ensure_ascii=False) if cache else "{}",
            "awaiting": "none",
        }
    total_end = hm[0] * 60 + hm[1] + duration_minutes(draft.get("duration"))
    eh, em = total_end // 60, total_end % 60
    ok = _in_window(hm[0], hm[1], eh, em)
    msg = ""
    if not ok:
        msg = "会议须在上午 08:30–11:20 或下午 13:00–17:00 内开始且结束。请重新选择时段。"
    return {
        "work_valid": "合法" if ok else "非法",
        "reply_text": msg,
        "draft_json": json.dumps(draft, ensure_ascii=False),
        "flags_json": json.dumps(flags, ensure_ascii=False) if flags else "{}",
        "cache_json": json.dumps(cache, ensure_ascii=False) if cache else "{}",
        "awaiting": "none",
    }
