"""
会议预约 · 本机 Mock API（端口 8888）
按真实接口路径/形状返回 demo 数据；进内网后改 workflow 全局变量 api_config 即可。

启动：
  pip install -r requirements.txt
  python app.py
  # 或: uvicorn app:app --host 0.0.0.0 --port 8888
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Meeting Mock API", version="1.1.0")

# 显式「查无此人」姓名（验收：T28）
NOT_FOUND_NAMES = frozenset({"不存在", "查无此人", "赵敏", "nobody"})

# Demo 规则目录（GET /mock/rules 与 README 对齐，便于试跑）
MOCK_RULES = [
    {"id": "dup_wang", "match": "王小明", "effect": "OA 返回 2 人重名（1001/2033）"},
    {"id": "busy_lihua_fri14", "match": "李华+周五14:00", "effect": "Outlook HasMeeting"},
    {"id": "full_room_16", "match": "开始时刻=16:00", "effect": "无空房+占用记录"},
    {"id": "leave_zhang_fri", "match": "工号4001+周五", "effect": "全天请假"},
    {"id": "leave_wang_mon_am", "match": "工号2033+周一上午", "effect": "请假"},
    {"id": "leave_9999", "match": "工号9999", "effect": "任意日全天请假"},
    {"id": "busy_wanglei_wed", "match": "王磊+周三", "effect": "Outlook 全天忙"},
    {"id": "not_found", "match": "不存在/查无此人/赵敏", "effect": "OA 返回空列表"},
    {"id": "room_large_b202", "match": "参会>10人", "effect": "仅推荐 B202大会议室"},
]

OUTLOOK_SECRETS = {
    "DEMO_OUTLOOK_SECRET_A",
    "DEMO_OUTLOOK_SECRET_B",
}


# ── models ──────────────────────────────────────────────

class FreeBusyBody(BaseModel):
    Email: str
    StartDate: str
    EndDate: str


class LeaveBody(BaseModel):
    empNoList: list[str]
    attnDate: str


class TimeRangeBody(BaseModel):
    startTime: str
    endTime: str
    fab: str = ""
    roomName: str = ""
    level: int = 0
    amount: int = 0


class SaveBookingBody(BaseModel):
    title: str = ""
    startTime: str
    endTime: str
    fab: str = ""
    roomId: str = ""
    attend: int = 0


class NotifyBody(BaseModel):
    type: str = "meeting_invite"
    meeting_id: str = ""
    draft: dict[str, Any] = Field(default_factory=dict)


# ── helpers ─────────────────────────────────────────────

def _parse_attn_date(s: str) -> Optional[datetime]:
    """请假 attnDate：严格 YYYY-MM-DD HH:mm（中间空格，无秒）。"""
    s = (s or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _leave_for_emp(emp_no: str, when: datetime) -> Optional[dict]:
    """
    Demo 请假规则（按工号 + 星期 / 固定工号）：
    - 4001 张老师：每周五全天请假 08:30-17:00
    - 2033 王小明(经管)：每周一上午请假 08:30-12:00
    - 9999：任意日期整天请假（试跑专用）
    - 3001 李华：不在此请假（周五14点走 Outlook 忙，方便分支分开测）
    """
    emp = (emp_no or "").strip()
    day = when.date()
    wd = when.weekday()  # 0=周一 … 4=周五

    if emp == "9999":
        b0 = datetime(day.year, day.month, day.day, 8, 30)
        b1 = datetime(day.year, day.month, day.day, 17, 0)
        return {"empNo": emp, "isOnLeave": True, "startTime": b0.strftime("%Y-%m-%d %H:%M:%S"), "endTime": b1.strftime("%Y-%m-%d %H:%M:%S")}

    if emp == "4001" and wd == 4:
        b0 = datetime(day.year, day.month, day.day, 8, 30)
        b1 = datetime(day.year, day.month, day.day, 17, 0)
        return {
            "empNo": emp,
            "isOnLeave": True,
            "startTime": b0.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": b1.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if emp == "2033" and wd == 0:
        b0 = datetime(day.year, day.month, day.day, 8, 30)
        b1 = datetime(day.year, day.month, day.day, 12, 0)
        return {"empNo": emp, "isOnLeave": True, "startTime": b0.strftime("%Y-%m-%d %H:%M:%S"), "endTime": b1.strftime("%Y-%m-%d %H:%M:%S")}

    return None


def _parse_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def _employees_by_name(name: str) -> list[dict]:
    name = (name or "").strip()
    if name in NOT_FOUND_NAMES:
        return []
    catalog = [
        {
            "empNo": "1001",
            "cName": "王小明",
            "eName": "Wang Xiaoming",
            "deptNo": "CS01",
            "deptName": "计算机学院",
            "email": "wangxm1001@nexchip.com.cn",
            "emailAddress": "wangxm1001@nexchip.com.cn",
            "fabCode": "N1A1",
        },
        {
            "empNo": "2033",
            "cName": "王小明",
            "eName": "Wang Xiaoming",
            "deptNo": "EM01",
            "deptName": "经管学院",
            "email": "wangxm2033@nexchip.com.cn",
            "emailAddress": "wangxm2033@nexchip.com.cn",
            "fabCode": "N1A1",
        },
        {
            "empNo": "3001",
            "cName": "李华",
            "eName": "Li Hua",
            "deptNo": "SW01",
            "deptName": "软件学院",
            "email": "lihua@nexchip.com.cn",
            "emailAddress": "lihua@nexchip.com.cn",
            "fabCode": "N1A1",
        },
        {
            "empNo": "4001",
            "cName": "张老师",
            "eName": "Zhang",
            "deptNo": "SW01",
            "deptName": "软件学院",
            "email": "zhang@nexchip.com.cn",
            "emailAddress": "zhang@nexchip.com.cn",
            "fabCode": "N1A1",
        },
        {
            "empNo": "5001",
            "cName": "王磊",
            "eName": "Wang Lei",
            "deptNo": "EE01",
            "deptName": "电子学院",
            "email": "wanglei@nexchip.com.cn",
            "emailAddress": "wanglei@nexchip.com.cn",
            "fabCode": "N1A1",
        },
        {
            "empNo": "6001",
            "cName": "陈静",
            "eName": "Chen Jing",
            "deptNo": "CS01",
            "deptName": "计算机学院",
            "email": "chenjing@nexchip.com.cn",
            "emailAddress": "chenjing@nexchip.com.cn",
            "fabCode": "N1A1",
        },
    ]
    if not name:
        return []
    hits = [e for e in catalog if name in e["cName"] or e["cName"] in name]
    if hits:
        return hits
    # 未知姓名：返回唯一假人，便于主链跑通
    return [
        {
            "empNo": "9001",
            "cName": name,
            "eName": None,
            "deptNo": "SW01",
            "deptName": "软件学院",
            "email": f"{name}@nexchip.com.cn",
            "emailAddress": f"{name}@nexchip.com.cn",
            "fabCode": "N1A1",
        }
    ]


def _wanglei_busy_slots(start: datetime, end: datetime) -> list[dict]:
    """王磊：每周三 09:00-17:00 忙（demo）。"""
    slots = []
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() == 2:  # Wednesday
            b0 = datetime(day.year, day.month, day.day, 9, 0, 0)
            b1 = datetime(day.year, day.month, day.day, 17, 0, 0)
            if _overlap(start, end, b0, b1):
                slots.append(
                    {
                        "StartTime": int(b0.timestamp()),
                        "EndTime": int(b1.timestamp()),
                        "StartTimeStr": b0.strftime("%Y-%m-%d %H:%M:%S"),
                        "EndTimeStr": b1.strftime("%Y-%m-%d %H:%M:%S"),
                        "Organizer": "王磊",
                    }
                )
        day += timedelta(days=1)
    return slots


def _lihua_busy_slots(start: datetime, end: datetime) -> list[dict]:
    """李华：每个周五 14:00-15:00 忙（demo）。"""
    slots = []
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() == 4:  # Friday
            b0 = datetime(day.year, day.month, day.day, 14, 0, 0)
            b1 = datetime(day.year, day.month, day.day, 15, 0, 0)
            if _overlap(start, end, b0, b1):
                slots.append(
                    {
                        "StartTime": int(b0.timestamp()),
                        "EndTime": int(b1.timestamp()),
                        "StartTimeStr": b0.strftime("%Y-%m-%d %H:%M:%S"),
                        "EndTimeStr": b1.strftime("%Y-%m-%d %H:%M:%S"),
                        "Organizer": "李华",
                    }
                )
        day += timedelta(days=1)
    return slots


def _is_sixteen(start: datetime) -> bool:
    return start.hour == 16


def _room_list(available: bool, min_capacity: int = 0) -> list[dict]:
    if not available:
        return []
    all_rooms = [
        {
            "date": "",
            "beginTime": "",
            "endTime": "",
            "roomName": "A101小会议室",
            "roomId": "R-A101",
            "fab": "N1",
            "location": "A栋1楼",
            "amount": 8,
            "remark": "",
        },
        {
            "date": "",
            "beginTime": "",
            "endTime": "",
            "roomName": "C303圆桌厅",
            "roomId": "R-C303",
            "fab": "N1",
            "location": "C栋3楼",
            "amount": 12,
            "remark": "",
        },
        {
            "date": "",
            "beginTime": "",
            "endTime": "",
            "roomName": "B202大会议室",
            "roomId": "R-B202",
            "fab": "N1",
            "location": "B栋2楼",
            "amount": 20,
            "remark": "大会议室",
        },
    ]
    if min_capacity > 10:
        return [r for r in all_rooms if r.get("amount", 0) >= min_capacity]
    return all_rooms


def _occupied_records() -> list[dict]:
    return [
        {
            "date": "",
            "beginTime": "16:00",
            "endTime": "17:00",
            "meetingRoom": "A101小会议室",
            "roomId": "R-A101",
            "fab": "N1",
            "location": "A栋1楼",
            "goal": "组会",
            "userExt": "张老师",
            "owner": "张老师",
            "meeting": "组会",
            "room": "A101小会议室",
        },
        {
            "date": "",
            "beginTime": "16:00",
            "endTime": "17:00",
            "meetingRoom": "B202大会议室",
            "roomId": "R-B202",
            "fab": "N1",
            "location": "B栋2楼",
            "goal": "答辩",
            "userExt": "李老师",
            "owner": "李老师",
            "meeting": "答辩",
            "room": "B202大会议室",
        },
    ]


# ── routes ──────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "meeting-mock",
        "port": 8888,
        "version": "1.1.0",
        "rules": len(MOCK_RULES),
    }


@app.get("/mock/rules")
def mock_rules():
    return {"version": "1.1.0", "rules": MOCK_RULES}


@app.post("/OAAPI.inner/api/Employee/GetEmployeeByName")
async def get_employee_by_name(
    request: Request,
    pName: str = Query(default=""),
    appno: Optional[str] = Header(default=None),
    appsecret: Optional[str] = Header(default=None),
):
    # 兼容 Query / 误放 Body
    if not pName:
        try:
            payload = await request.json()
            pName = (payload or {}).get("pName") or (payload or {}).get("name") or ""
        except Exception:
            pName = ""
    return _employees_by_name(pName)


@app.post("/AttnDashApi/api/bizAttendanceCheck/isOnLeave")
def is_on_leave(body: LeaveBody):
    if not body.empNoList:
        return {"code": 400, "message": "empNoList不能为空", "result": None}

    when = _parse_attn_date(body.attnDate)
    if when is None:
        return {
            "code": 400,
            "message": "attnDate格式错误，应为YYYY-MM-DD HH:mm",
            "result": None,
        }

    result = []
    for emp in body.empNoList:
        hit = _leave_for_emp(emp, when)
        if hit:
            result.append(hit)
        else:
            result.append(
                {
                    "empNo": emp,
                    "isOnLeave": False,
                    "startTime": None,
                    "endTime": None,
                }
            )
    return {"code": 200, "message": "success", "result": result}


@app.post("/NexOutlook/api/ScheduleSyncInfo/GetFreeBusyStatus")
def get_free_busy(body: FreeBusyBody, x_app_secret: Optional[str] = Header(default=None, alias="X-App-Secret")):
    if x_app_secret not in OUTLOOK_SECRETS:
        return {"state": 1, "msg": "无效的APP密钥", "data": None}
    start = _parse_dt(body.StartDate)
    end = _parse_dt(body.EndDate)
    if not start or not end or start >= end:
        return {"state": 1, "msg": "开始时间必须小于结束时间", "data": None}

    email = (body.Email or "").lower()
    slots: list[dict] = []
    if "lihua" in email or "李华" in body.Email:
        slots = _lihua_busy_slots(start, end)
    elif "wanglei" in email or "王磊" in (body.Email or ""):
        slots = _wanglei_busy_slots(start, end)

    has = bool(slots)
    return {
        "state": 0,
        "msg": "查询成功",
        "data": {
            "HasMeeting": has,
            "TotalMeetingCount": len(slots),
            "HasRequiredMeeting": has,
            "RequiredMeetingCount": len(slots),
            "RequiredMeetingSlots": slots,
            "HasOptionalMeeting": False,
            "OptionalMeetingCount": 0,
            "OptionalMeetingSlots": [],
            "HasOrganizerMeeting": False,
            "OrganizerMeetingCount": 0,
            "OrganizerMeetingSlots": [],
        },
    }


@app.post("/MeetingRoom/api/MeetingRoom/BookableMeetRooms")
def bookable_meet_rooms(body: TimeRangeBody):
    start = _parse_dt(body.startTime) or datetime.now()
    available = not _is_sixteen(start)
    min_cap = body.amount if body.amount and body.amount > 10 else 0
    return {"state": 0, "msg": "ok", "data": _room_list(available, min_cap)}


@app.post("/MeetingRoom/api/MeetingRoom/BookableSuggestRooms")
def bookable_suggest_rooms(body: TimeRangeBody):
    start = _parse_dt(body.startTime) or datetime.now()
    # 建议：避开 16 点，给下一整点有房时段
    suggest_start = start.replace(minute=0, second=0, microsecond=0)
    if _is_sixteen(suggest_start):
        suggest_start = suggest_start + timedelta(hours=1)
    min_cap = body.amount if body.amount and body.amount > 10 else 0
    rooms = _room_list(True, min_cap)
    for r in rooms:
        r["beginTime"] = suggest_start.strftime("%H:%M")
        r["endTime"] = (suggest_start + timedelta(hours=1)).strftime("%H:%M")
        r["date"] = suggest_start.strftime("%Y-%m-%d")
    return {"state": 0, "msg": "ok", "data": {"isSuggest": True, "bookableRooms": rooms}}


@app.post("/MeetingRoom/api/MeetingRoom/RoomBookingRecords")
def room_booking_records(body: TimeRangeBody):
    start = _parse_dt(body.startTime) or datetime.now()
    data = _occupied_records() if _is_sixteen(start) else []
    return {"state": 0, "msg": "ok", "data": data}


@app.post("/MeetingRoom/api/MeetingRoom/SaveBookingInfo")
def save_booking_info(body: SaveBookingBody):
    if (body.roomId or "").upper() in ("R-FAIL", "FAIL"):
        return {"state": 1, "msg": "会议室系统繁忙，请稍后重试", "data": None}
    mid = "M" + datetime.now().strftime("%m%d%H%M%S")
    return {
        "state": 0,
        "msg": "预约成功",
        "data": {
            "meeting_id": mid,
            "meetingId": mid,
            "roomId": body.roomId,
            "title": body.title,
            "startTime": body.startTime,
            "endTime": body.endTime,
        },
    }


@app.post("/notify/email")
def notify_email(body: NotifyBody):
    teachers = (body.draft or {}).get("teachers") or "参会人"
    topic = (body.draft or {}).get("topic") or "会议"
    msg = f"邀请邮件已发送至 {teachers}（主题：{topic}，单号：{body.meeting_id or 'M001'}）。"
    return {"ok": True, "type": body.type or "meeting_invite", "meeting_id": body.meeting_id or "M001", "message": msg}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)
