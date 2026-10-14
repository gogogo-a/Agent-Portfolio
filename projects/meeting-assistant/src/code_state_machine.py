import json

def _loads(s, default=None):
    if default is None:
        default = {}
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default

def main(draft_json: str, flags_json: str, awaiting: str) -> dict:
    draft = _loads(draft_json)
    flags = _loads(flags_json)
    aw = (awaiting or "none").strip()
    flow_mode = flags.get("flow_mode") or "新建预约"

    if aw == "revise_book":
        return {"branch": "改口引导"}

    if flow_mode == "抢占会议室":
        return {"branch": "抢占会议室"}

    locked = flags.get("teachers_locked") is True
    has_time = bool(draft.get("time"))
    has_name = bool(draft.get("teachers_input") or draft.get("teachers"))

    if not locked:
        if has_name:
            branch = "查老师重名" if has_time else "仅查老师"
        else:
            branch = "追问参会老师"
    elif locked and not has_time:
        branch = "展示空闲表"
    elif locked and has_time:
        branch = "继续预约"
    else:
        branch = "追问补充信息"

    return {"branch": branch}
