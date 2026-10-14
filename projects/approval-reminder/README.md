# 审批催办

定时查出超时还没办的审批，按人合成一条提醒发出去。不管审批规则本身怎么定。

## 有什么不一样

- 超时按「这个人待办是什么时候生成的」算，不按流程什么时候结束算
- 一个人有很多待办时合成一条，避免刷屏
- 智能体只管查待办和发消息，审批怎么过仍由原来的系统决定

## 流程

```text
定时启动 → 查出超时待办 → 按人合并 → 一条条发送 → 记下结果
```

- 详细说明：[docs/design.md](./docs/design.md)
- 流程图：[diagrams/workflow.md](./diagrams/workflow.md)
- 假消息例子：[examples/message.md](./examples/message.md)
