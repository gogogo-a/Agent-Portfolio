# 消息催办 · 详细设计

## 节点顺序

| 顺序 | 节点 | 作用 |
|------|------|------|
| 1 | 定时触发 | 云端周期运行 |
| 2 | 数据库查询 | 传入超时阈值分钟数 |
| 3 | 条件判断 | 无数据则结束 |
| 4 | 按人员合并 | 同一邮箱一组 |
| 5 | 按实例合并 | 人员内按流程实例 |
| 6 | 组装事项 | 实例下审批节点列表 |
| 7 | 列表循环 | 逐人处理 |
| 8 | 生成提醒 | 模板正文 |
| 9 | 发送消息 | 按邮箱投递 |
| 10 | 结束 | 留存结果 |

## 超时口径

```text
超时分钟数 = 当前时间 - 待办人员创建时间
进入提醒 = 超时分钟数 >= timeout_minutes
```

## 示意 SQL（PostgreSQL 形态，表名已通用化）

```sql
SELECT
    aa.assignee_name,
    aa.assignee_email,
    i.id AS instance_id,
    i.process_instance_name,
    a.id AS approval_id,
    a.activity_name,
    a.activity_descr,
    aa.created_time AS assignee_created_time,
    FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP::timestamp - aa.created_time)) / 60)::integer
        AS overdue_minutes
FROM bpm_instance AS i
INNER JOIN bpm_approval AS a ON a.business_id = i.business_id
INNER JOIN bpm_approval_assignee AS aa ON aa.approval_id = a.id
WHERE i.process_status = 'RUNNING'
  AND a.approval_status = 'PENDING'
  AND aa.executed = 0
  AND FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP::timestamp - aa.created_time)) / 60) >= $1;
```

> 公开文档使用通用表名；生产实现以客户库为准。
