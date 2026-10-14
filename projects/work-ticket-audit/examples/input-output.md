# 脱敏样例：输入 → 期望输出

> 以下为**虚构**票面字段与期望结果，用于理解契约，非客户真实票据。

## 共用任务计划（系统内置）

```json
{
  "plan_id": "DEMO-PLAN-001",
  "plan_time_range": "2026-09-14 08:00至2026-09-14 17:00",
  "plan_sections": ["演示线 K120+300~K121+100"],
  "plan_pole_nos": ["#1201", "#1202"],
  "plan_equipment_nos": ["DW-08"]
}
```

## 样例 A：应通过

**输入字段（解析后）**

```json
{
  "is_work_ticket": true,
  "ticket_type": "第一种",
  "ticket_no": "DEMO-001",
  "worker_count_total": 3,
  "workers": ["甲", "乙", "丙"],
  "work_time_range": "2026-09-14 09:00至2026-09-14 12:00",
  "work_section": "演示线 K120+300~K121+100",
  "pole_no": "#1201/#1202",
  "equipment_no": "DW-08",
  "work_content": "接触网设备巡检",
  "safety_measures": "设置防护、停电验电接地"
}
```

**期望**：审核等级 = 通过；报告含【基本信息】【审核统计】【合规项清单】【违规项清单】。

## 样例 B：时间超出计划

`work_time_range` = `2026-09-14 07:00至2026-09-14 12:00`（早于计划 08:00）

**期望**：对应 in_plan 时间规则不通过；等级至少为有条件通过或不通过（按 severity 配置）。

## 样例 C：非工作票

上传普通红头通知 PDF，`is_work_ticket=false`。

**期望**：短路「非工作票」报告，不进入规则/模型审核。
