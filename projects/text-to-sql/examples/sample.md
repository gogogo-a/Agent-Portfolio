# 虚构样例

**用户**：上月华东区预算执行率是多少？

**意图**：预算检索（单意图）

**示意 SQL（只读）**

```sql
SELECT region, executed_amount / budget_amount AS exec_rate
FROM budget_exec
WHERE region = '华东' AND month = '2026-09';
```

**质检**：结果行存在且字段完整 → 通过；空结果 → 触发改写或降级「无该信息」。
