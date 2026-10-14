import json
import re

FLOW_INTENTS = ("新建预约", "继续预约", "抢占会议室")
NO_FLOW_INTENTS = ("流程咨询", "闲聊", "其它", "重新开始", "调整会议", "查询会议")
AWAITING_CONTINUE = frozenset({"pick_teacher", "pick_time", "confirm_book", "clarify", "revise_book"})
RESET_WORDS = ("重新开始", "重新来", "清空", "重来")
PREEMPT_PHRASES = ("抢占会议室", "预留会议室", "抢占")
PROCESS_CONSULT_MARKERS = (
    "怎么进行",
    "怎么做",
    "怎么预约",
    "流程怎么",
    "预约流程",
    "能约会议吗",
    "能约吗",
    "怎么用",
    "支持什么",
    "有哪些步骤",
)
UNSUPPORTED_MARKERS = ("取消", "改成", "调整", "昨天订", "把昨天", "把前天", "已有会议")


def _loads_flags(flags_json: str) -> dict:
    try:
        o = json.loads(flags_json or "{}")
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_reset_query(q: str) -> bool:
    s = (q or "").strip()
    return any(w in s for w in RESET_WORDS)


def _is_process_consult(q: str) -> bool:
    s = (q or "").strip()
    if any(m in s for m in PROCESS_CONSULT_MARKERS):
        return True
    if ("能约" in s or "可以约" in s) and not re.search(
        r"(周[一二三四五六日天]|星期|\d+点|上午|下午|晚上|点钟|:\d{2})",
        s,
    ):
        return re.search(r"[\u4e00-\u9fa5]{2,4}", s) is None
    return False


def _is_unsupported_adjust(q: str) -> bool:
    s = (q or "").strip()
    return any(w in s for w in UNSUPPORTED_MARKERS)


def _is_preempt_query(q: str) -> bool:
    s = (q or "").strip()
    if not s:
        return False
    if "会议室" in s and any(k in s for k in ("抢占", "预留", "占")):
        return True
    return any(p in s for p in PREEMPT_PHRASES)


def _is_transactional_booking(q: str) -> bool:
    s = (q or "").strip()
    if not s or _is_process_consult(s) or _is_unsupported_adjust(s):
        return False
    if not re.search(r"(约|订|预约|开会|订会)", s):
        return False
    if re.search(
        r"(周[一二三四五六日天]|星期|明天|后天|\d+点|上午|下午|晚上|点钟|:\d{2})",
        s,
    ):
        return True
    if re.search(r"约\s*[\u4e00-\u9fa5]{2,4}", s):
        return True
    return False


def main(
    arg1: str,
    awaiting: str = "none",
    user_query: str = "",
    flags_json: str = "",
    final_reply: str = "",
) -> dict:
    awaiting = (awaiting or "none").strip()
    user_query = (user_query or "").strip()
    flags = _loads_flags(flags_json)
    if flags.get("confirm_pending") and awaiting not in AWAITING_CONTINUE:
        awaiting = "confirm_book"

    if _is_reset_query(user_query):
        return {"intent_type": "重新开始", "enter_flow": "否"}

    # L0：会话状态优先，不依赖 LLM / 上轮文案
    if flags.get("confirm_pending") or awaiting in AWAITING_CONTINUE:
        return {"intent_type": "继续预约", "enter_flow": "是"}

    if awaiting == "none" and _is_preempt_query(user_query):
        return {"intent_type": "抢占会议室", "enter_flow": "是"}

    data = _extract_json(arg1)
    intent_type = str(data.get("intent_type") or "其它").strip()
    if intent_type == "其它" and data.get("intent"):
        legacy = {"new_booking": "新建预约", "continue": "继续预约", "other": "其它"}
        intent_type = legacy.get(str(data.get("intent")).strip(), "其它")

    if _is_process_consult(user_query):
        return {"intent_type": "流程咨询", "enter_flow": "否"}

    if _is_unsupported_adjust(user_query):
        return {"intent_type": "其它", "enter_flow": "否"}

    if intent_type in ("其它", "闲聊") and _is_transactional_booking(user_query):
        intent_type = "新建预约"

    explicit = data.get("enter_flow")
    if explicit in ("是", "否"):
        enter_flow = explicit
    else:
        enter_flow = "是" if intent_type in FLOW_INTENTS else "否"

    if intent_type in FLOW_INTENTS:
        enter_flow = "是"
    elif intent_type in NO_FLOW_INTENTS or intent_type == "重新开始":
        enter_flow = "否"

    if enter_flow == "是" and intent_type not in FLOW_INTENTS:
        intent_type = "继续预约" if awaiting != "none" else "新建预约"

    return {"intent_type": intent_type, "enter_flow": enter_flow}
