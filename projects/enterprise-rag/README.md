# 知识库问答

根据公司规章制度、红头文件等文档回答问题。常见问题可以直接答；更复杂的会选库、改写问题、检索、检查质量，再根据片段作答。

## 有什么不一样

- 文档从版式文件转到可检索内容，再进知识库；大约一万多份，支持断点续跑、失败重试、旧版停用
- 向量模型长度有限、规章又经常没标题，用滑动窗口切分，减少一段里混好几个主题
- 做了 100 道测试题；看标准片段能不能进前 5；加上质量判断和实体关系后，召回率从 74% 提升到 91%，切段带来的提升最大
- 向量和关键词一起用，再排序；最多检查三轮，不行就改写问题再搜
- 出错时按「选错库 / 片段不对 / 答案不对」分层查，口语题单独回归，不为某一题硬堆例子

## 背景

文档多、专有名词多。只靠相似度容易搜偏；只靠关键词又怕口语换说法搜不到。需要能测、能改的一整套问答。

## 我做了什么

- 参与知识库建设和问答流程
- 落地滑动窗口切分和评测回归
- 整理出包、画布排布和安装时的坑

## 流程大概这样

```text
提问 → 是不是常见问题 → 是就直接答
     → 不是就选库、改写
     → 检索 → 质量够不够 → 不够就改写再搜
     → 根据有用片段回答
```

详细设计：[docs/design.md](./docs/design.md)  
切段说明：[docs/semantic-chunking.md](./docs/semantic-chunking.md)

流程图：[diagrams/workflow.md](./diagrams/workflow.md) · [rag-workflow.svg](../../assets/diagrams/rag-workflow.svg) · [rag-chunk-workflow.svg](../../assets/diagrams/rag-chunk-workflow.svg)

## 自己电脑上可以试

```bash
cd src
python semantic_chunk_md.py --help
```

## 测试摘要

见 [evaluation/report.md](./evaluation/report.md)。
