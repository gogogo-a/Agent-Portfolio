# 会议预约助手

用对话约会议：问清老师和时间，查忙闲，分会议室，确认后再预订。

## 有什么不一样

- 以前要来回问谁有空、哪间房空，现在走对话流程，一次大约能少半小时以上
- 草稿、校验结果、候选时段分开存；改了信息就作废旧校验再查，避免旧结果盖住新选择
- 意图用规则加大模型一起判；预订前再核对一遍；主体改了、说不清、答非所问会按规定处理；完成率大约多 6 个百分点
- 只有核对通过后才真正下单，成功后收束会话，避免点两次订两次
- 准备了 47 个多轮场景，覆盖重名、满房、请假、改口、时间边界，完成率约 96%

## 背景

分支很多。如果全塞进一个大脚本，画布难看，接口也难一个个替换和验收。

## 我做了什么

- 设计会话状态：聊到哪一步、缺什么信息
- 把大模型、代码、接口节点拆开，一个接口一个节点
- 按线上真实接口搭本机模拟，并已在线上测过；写场景化测试并跑通

## 流程大概这样

```text
用户每说一句，整张图从头跑一遍
  → 判断意图 → 抽出本轮信息 → 状态机决定下一步
  → 查老师 / 看空闲 / 查忙闲 / 分会议室 / 预订
  → 把状态写回去再回复
```

平台画布不能画成圈，所以「循环」靠多轮对话和会话变量完成。  
详细设计：[docs/design.md](./docs/design.md)  
状态机代码：[src/code_state_machine.py](./src/code_state_machine.py)

流程图：[diagrams/workflow.md](./diagrams/workflow.md) · [meeting-workflow.svg](../../assets/diagrams/meeting-workflow.svg)

## 本机怎么试跑

按线上真实接口在本地模拟（路径一致；业务已在线上测过）：

```bash
cd demo/mock-server
pip install -r requirements.txt
python app.py
curl -s http://127.0.0.1:8888/health
```

对话例子：[examples/dialog.md](./examples/dialog.md)  
测试摘要：[evaluation/report.md](./evaluation/report.md)
