# fresh-swe-bench

一个"模型没见过"的小型代码修复基准（contamination-free mini SWE benchmark）。

## 动机

主流基准（SWE-bench 等）的实例早已进入各大模型的训练数据，测出的分数无法区分"背过题"和"真会做"。本项目从活跃维护的开源仓库中收割模型知识截止日期之后的 merged PR，构建小规模、可自动判卷、无数据污染的代码修复任务集。

## 设计

- **数据**：20~30 个实例，每个 = base commit + 任务描述（来自 issue/PR）+ fail-to-pass 测试
- **判卷**：Docker 内 checkout → 应用 patch → 跑测试 → 自动判定（复用 SWE-bench 方法论）
- **Agent**：基于 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 的极简循环
- **基线**：本地 Qwen3-8B（RTX 5060 Ti 16GB）+ DeepSeek API，报告解决率 / 成本 / 步数

## 借了什么 / 我们做了什么

- **借**：SWE-bench 的实例构造与判卷方法论；mini-swe-agent 的 agent 循环与环境执行
- **自建**：实例收割管线（近期 PR → 实例）、判卷适配、全部基线数据与失败模式分析

## 状态

- [x] **Day 1 冒烟通过**：mini-swe-agent × `deepseek-flash` 解出 SWE-bench Lite 首个实例（marshmallow-code__marshmallow-1343），自建判卷脚本 [`scripts/evaluate.py`](scripts/evaluate.py) 判定 **RESOLVED**（F2P 1/1，P2P 24/24），agent 总成本 $0.0045（14 次 API 调用）。完整轨迹见 [`examples/`](examples/)
- [ ] Day 2-3：实例收割管线（click / fastapi / pydantic 2026-08 之后的 merged PR）
- [ ] Day 4-5：基线评测跑（本地 Qwen3-8B + API 模型对照）
- [ ] Day 6-7：失败分析 + 博客
