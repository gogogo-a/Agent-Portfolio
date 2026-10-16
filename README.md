# 智能体开发笔记

把做过的智能体项目记下来：怎么想需求、怎么搭流程、怎么改提示词、怎么测、怎么交出去。

## 项目列表

| 项目 | 做什么 | 有什么不一样 |
|------|--------|--------------|
| [工作票智能审核](./projects/work-ticket-audit/) | 自动查工作票哪里不合规 | 固定规则和大模型一起审；3 种票、13 条规则；一张票大约从 3 分钟降到半分钟；准确率大概提高一成多 |
| [知识库问答](./projects/enterprise-rag/) | 按规章制度等文档回答问题 | 一万多份文档入库；滑动窗口切分；召回率从 74% 提升到 91% |
| [会议预约助手](./projects/meeting-assistant/) | 多轮对话约会议室 | 用状态记住聊到哪一步；47 个场景反复测；完成率大约九成六；改了信息会作废旧结果再查 |
| [智能问数](./projects/text-to-sql/) | 用白话查数据库 | 先认清要查什么；只读执行；不对就重写最多三次；查不到就老实说没有，不瞎编数字 |

另外还有 [审批催办](./projects/approval-reminder/) 和 [报税自动填表](./projects/tax-filing-rpa/)。  
自己写的小项目：[MiniCode](https://github.com/gogogo-a/MiniCode)。

## 流程图

### 会议预约

![会议预约](./assets/diagrams/meeting-workflow.svg)

### 知识库问答

![知识库问答](./assets/diagrams/rag-workflow.svg)

### 滑动窗口切段

![滑动窗口切段](./assets/diagrams/rag-chunk-workflow.svg)

### 工作票审核

![工作票审核](./assets/diagrams/ticket-workflow.svg)

## 平时怎么做项目

1. 先搞清楚要解决什么、输入输出是什么、有哪些限制。
2. 再拆流程：哪一步用规则、哪一步用大模型、哪一步调接口。
3. 在平台上搭节点，写提示词，约定数据怎么往下传。
4. 准备测试题，出错就改，改完再跑一遍。
5. 配环境、验收、写文档，交给对方。

## 提交记录

开发过程用 Git 管理。这里整理了按主题看过哪些提交：

→ [提交记录](./HISTORY.md)

## 本地联调

会议预约联调服务：

```bash
cd projects/meeting-assistant/demo/mock-server
pip install -r requirements.txt
python app.py
curl -s http://127.0.0.1:8888/health
```

知识库切段脚本：

```bash
cd projects/enterprise-rag/src
python semantic_chunk_md.py --help
```

## 说明

设计思路、问题排查、测试和经验总结。  
通用做法见 [lessons/agent-playbook.md](./lessons/agent-playbook.md)。

仓库地址：https://github.com/gogogo-a/Agent-Portfolio
