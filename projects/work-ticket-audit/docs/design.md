# 工作票审核 · 详细设计

## 设计思想（先想清楚再拖节点）

1. **交付文档是第一原型**  
   审核等级、报告区块、处理方式文案必须对齐技术方案原文，而不是仓库里旧 MD「差不多」。

2. **语义归 LLM，契约归 Code**  
   填字段 / 模型审核要 JSON；解析节点做提取、枚举校验、空值兜底、时间归一化。

3. **按下游裁剪输入**  
   规则审核拿全量字段；模型审核只拿票种 + 工作内容 + 安全措施，避免规则已审字段干扰。

4. **结论不用 LLM 排版**  
   severity → 通过 / 有条件通过 / 不通过；报告全文 code 拼框线模板。

## 主流程伪代码

```python
def audit(file_bytes):
    parse = mineru(file_bytes)
    if not parse.ok:
        return fail_report("解析失败", parse.error)

    fields = parse_fields_llm(parse.text)   # temperature=0, json_object
    fields = code_parse_fields(fields.text) # 归一化 + is_work_ticket
    if not fields.is_work_ticket:
        return fail_report("非工作票")
    if not fields.ok:
        return fail_report("字段不可审", fields.error)

    rule = rule_engine(fields, DEFAULT_PLAN)          # R-01..R-11
    model_in = pick(fields, ["ticket_type", "work_content", "safety_measures"])
    model = parse_model_json(model_audit_llm(model_in))

    payload = merge_severity(rule, model)  # 含合规清单/违规清单
    return render_report(payload)              # code only
```

## 审核等级约定

| 条件 | 等级 | 处理方式 |
|------|------|----------|
| 无违规 | 通过 | 审核通过，可执行作业 |
| 仅 medium/low | 有条件通过 | 需修正后复审 |
| 存在 high | 不通过 | 审核不通过，需重新签发 |

## 规则与模型分工（示例）

| 规则 | 类型 | 要点 |
|------|------|------|
| R-人数 | 规则 | worker 列表长度 vs 填报人数 |
| R-时间 in_plan | 规则 | 区间包含，不是字符串相等 |
| R-区段/支柱/设备 | 规则 | plan 列表包含；plan 空 → 不通过 |
| 措施冲突 / 语义项 | 模型 | 结构化 JSON 列出问题 |

## 关键经验链接

见仓库 [lessons/agent-playbook.md](../../lessons/agent-playbook.md) 中「短路」「时间归一化」「修 bug 泛化」章节。
