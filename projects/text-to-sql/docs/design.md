# NL2SQL · 详细设计

## 稳定性怎么保证（实践总结）

1. **意图识别**：区分检索往来 / 预算等；多意图做成列表，循环送入子工作流。  
2. **子工作流只接单一意图**：构造该意图对应的「数据库包」（表、字段、口径说明）+ 示例，作为 Prompt 上下文。  
3. **生成后执行**：使用**只读**账号。  
4. **质量检验器**：按当前意图判断结果是否符合数据标准；不合格把错误/修改意见回传，最多 3 次。  
5. **失败降级**：最终仍不行 → 回答该意图「数据库没有该信息」，确保不胡乱回答。  
6. **融合回复**：子工作流全部完成后，汇总给回复模型。

## 伪代码

```python
def ask(question, history):
    intents = detect_intents(question)  # list
    answers = []
    for intent in intents:
        schema_pack = build_schema_pack(intent)
        sql, rows, ok = None, None, False
        feedback = ""
        for _ in range(3):
            sql = llm_sql(intent, schema_pack, feedback)
            if not is_readonly_safe(sql):
                feedback = "拒绝非只读 SQL"
                continue
            rows, err = exec_readonly(sql)
            ok, feedback = quality_check(intent, sql, rows, err)
            if ok:
                break
        answers.append(rows if ok else {"miss": True, "intent": intent})
    return fuse_answer(question, answers)
```

## 建议的安全加固（作品集增强项）

- 仅允许 `SELECT`；禁 `INSERT/UPDATE/DELETE/DDL`  
- 表名白名单  
- LIMIT 强制  
- 超时与行数上限
