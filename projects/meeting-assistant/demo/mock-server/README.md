# 会议预约本机假接口

本机试跑用的服务，端口 8888。用来测重名、忙碌、满房、请假等路径。

## 启动

```bash
cd mock-server
pip install -r requirements.txt
python app.py
```

健康检查：`GET http://127.0.0.1:8888/health`

## 和平台全局配置的对应

| 键 | 本机默认 | 用途 |
|----|----------|------|
| `base_oa` | `http://127.0.0.1:8888/OAAPI.inner/api` | 员工 |
| `base_attn` | `http://127.0.0.1:8888/AttnDashApi/api` | 请假 |
| `base_meeting` | `http://127.0.0.1:8888/MeetingRoom` | 会议室 |
| `base_outlook` | `http://127.0.0.1:8888/NexOutlook` | 闲忙 |
| `base_notify` | `http://127.0.0.1:8888` | 邮件通知 |
| `oa_appno` / `oa_appsecret` | 试跑占位 | OA 请求头 |
| `outlook_secret` | 试跑占位 | Outlook 请求头 |

OA 用 `appno` / `appsecret`；Outlook 用 `X-App-Secret`；会议室接口用 `Content-Type: application/json-patch+json`。

## 主要路径

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/OAAPI.inner/api/Employee/GetEmployeeByName?pName=` | 查员工 |
| POST | `/AttnDashApi/api/bizAttendanceCheck/isOnLeave` | 请假 |
| POST | `/NexOutlook/api/ScheduleSyncInfo/GetFreeBusyStatus` | 闲忙 |
| POST | `/MeetingRoom/api/MeetingRoom/BookableMeetRooms` | 可约会议室 |
| POST | `/MeetingRoom/api/MeetingRoom/BookableSuggestRooms` | 建议 |
| POST | `/MeetingRoom/api/MeetingRoom/RoomBookingRecords` | 占用记录 |
| POST | `/MeetingRoom/api/MeetingRoom/SaveBookingInfo` | 保存预约 |
| POST | `/notify/email` | 发邮件通知 |

示例：

```bash
curl -s -X POST http://127.0.0.1:8888/MeetingRoom/api/MeetingRoom/BookableMeetRooms \
  -H 'Content-Type: application/json-patch+json' \
  -d '{"startTime":"2026-09-26T16:00:00","endTime":"2026-09-26T17:00:00"}'
```

## 试跑规则

| 规则 | 触发 | 效果 |
|------|------|------|
| 重名 | 王小明 | 返回两个候选 |
| 忙 | 李华 + 周五 14:00 | 该时段有会 |
| 忙 | 王磊 + 周三 | 全天忙 |
| 满房 | 开始时刻是 16:00 | 无空房并带占用记录 |
| 可约 | 其它合法时段 | 返回可用会议室 |
| 查无此人 | 赵敏等 | 返回空列表 |

规则一览：`GET http://127.0.0.1:8888/mock/rules`

接到真实环境时，改平台全局配置里的地址和密钥即可，路径不用改。
