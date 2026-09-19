# Physical AI 前沿雷达 · Physical AI Radar

[![daily radar](https://github.com/noteflowai/physical-ai-radar/actions/workflows/daily.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/daily.yml)
[![CI](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml)
[![code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE)
[![content: CC BY 4.0](https://img.shields.io/badge/content-CC%20BY%204.0-lightgrey.svg)](LICENSE-CONTENT)
[![updated daily 01:30 UTC](https://img.shields.io/badge/updated-daily%2001%3A30%20UTC-7300e5.svg)](.github/workflows/daily.yml)

**语言：中文 · [English](README.en.md) · [日本語](README.ja.md)**

每天自动更新的具身智能（Physical AI）前沿雷达。它只做三件事：**按八条主线归类**、**给每条结论标注证据等级**、**用中英日三语写清"为什么重要"**。

与"论文列表"或"新闻聚合"的区别：

- **证据可查，口径不混**：`[O]` 一手官方 / `[R]` 论文预印本 / `[M]` 媒体二手，厂商自报与同行评议不混为一谈。
- **只留可检验的数字**：自动抽取成功率、延迟、Hz、参数量、数据小时数等量化指标，没有数字的条目不会被吹成突破。
- **前瞻但不玄学**：雷达覆盖自我改进闭环、推理时计算、世界模型评测、运行时安全、合规排期与可靠性经济学——每一条都挂原始链接。
- **零依赖可复现**：整条流水线只用 Python 标准库，图表是手写 SVG，任何人都能在本地跑出同样结果。

<!-- RADAR:START -->
### 今日雷达 · 2026-09-19

`生成时间: 2026-09-19 08:12 UTC` ｜ `统计窗口: 2026-09-16 → 2026-09-19 (UTC)`

- `[R]` **[GeoAAC: Geometry-Based Adaptive Action Chunking from Denoising Trajectories in VLA Policies](http://arxiv.org/abs/2609.20776v1)** — 基座模型（VLA / WAM）
  - 基座模型层的变化会直接决定下游所有任务的起点：它影响你需要多少自有数据、能不能跨本体迁移。
- `[R]` **[rMuscle: Robotic Muscle Memory for Efficient Vision-Language-Action Model Inference](http://arxiv.org/abs/2609.19104v1)** — 边缘与实时性（端侧推理 / 控制频率）
  - 端侧延迟与控制频率是硬约束：模型再强，超出实时预算就不能进入控制回路。
- `[R]` **[PASSAGE: Scaling Scene-Aligned Motion Learning for Perceptive Humanoid Traversal in Cluttered Environments](http://arxiv.org/abs/2609.18732v1)** — 边缘与实时性（端侧推理 / 控制频率） ｜ `50 Hz` · `48.1%`
  - 端侧延迟与控制频率是硬约束：模型再强，超出实时预算就不能进入控制回路。
- `[R]` **[StageGuard: Learning Stage Transitions for Long-Horizon Robot Tasks via Agentic Distillation](http://arxiv.org/abs/2609.20791v1)** — 系统与编排（长程 / 多策略）
  - 长程任务的系统形态是编排器加策略池，运维对象从单个模型变成一组能力边界。
- `[R]` **[Agile-WAM: An Agile Tactile World Action Model for Contact-Rich Robot Control](http://arxiv.org/abs/2609.20761v1)** — 基座模型（VLA / WAM） ｜ `11.9 ms`
  - 基座模型层的变化会直接决定下游所有任务的起点：它影响你需要多少自有数据、能不能跨本体迁移。

[今日雷达 ›](radar/daily/2026-09-19.zh.md) · [历史归档 ›](radar/INDEX.md)

![lane distribution](assets/lane-distribution.svg)

![cadence](assets/cadence.svg)
<!-- RADAR:END -->

## 八条主线

| 主线 | 关注什么 | 为什么值得单独看 |
| --- | --- | --- |
| 基座模型（VLA / WAM） | 视觉-语言-动作模型、世界动作模型、开放权重 | 决定下游任务的起点与跨本体迁移能力 |
| 数据引擎 | 遥操作、第一人称人类视频、清洗与 scaling | 决定单位新任务的数据成本 |
| 训练与自我改进 | RL 后训练、advantage conditioning、车队级回流 | 决定策略部署后还能不能继续变强 |
| 仿真与评测 | sim2real、real-to-sim、闭环成功率、评测平台 | 离线指标不等于闭环成功率 |
| 边缘与实时性 | 端侧推理、量化、异步推理、控制频率 | 超出实时预算的模型进不了控制回路 |
| 安全、权限与合规 | 安全过滤器、CBF、ISO / 法规时间表 | 认证空窗期里安全靠架构而非合格证 |
| 系统与编排 | 长程任务、编排器、记忆、多策略路由 | 运维对象从"一个模型"变成"一组能力边界" |
| 本体与供应链 | 人形、执行器、触觉、BOM 与产线 | 成本曲线与交付节奏的实际限速器 |

## 怎么读

1. **先看证据标注再看结论**。`[R]` 的数字是作者自报，`[M]` 的出货量常有口径冲突，`[O]` 也要区分"平台可用"和"客户已部署"。
2. **关键数字优先于形容词**。每条尽量给出成功率、延迟、数据量；没有数字说明这条还在叙事阶段。
3. **合规看日期**。标准与法规条目直接给生效或阶段日期，方便抄进项目计划。

## 自动更新机制

```
GitHub Actions (cron 01:30 UTC / 09:30 CST / 10:30 JST)
  └─ fetch    arXiv API（六组 cs.RO 查询）+ 官方 Atom/RSS（AWS / NVIDIA / DeepMind / HF / IEEE）
  └─ distill  归类到八条主线 → 抽取量化指标 → 可解释打分 → 每主线限额筛选
  └─ charts   手写 SVG：主线分布 / 每日节奏 / 证据构成
  └─ render   生成三语日报 + 注入三份 README + 更新归档索引与 latest.json
  └─ commit   有变化才提交（[skip ci]）
```

时间点选在 arXiv 当日公告之后，保证早上第一眼看到的是最新一批。

## 本地运行

```bash
git clone https://github.com/noteflowai/physical-ai-radar.git
cd physical-ai-radar

python3 -m pairadar --offline --out /tmp/radar   # 不联网，产物写到别处，不改动仓库
python3 -m pairadar --offline                   # 不联网，直接改写仓库内的日报与 README
python3 -m pairadar                             # 联网抓取当日新内容
python3 -m unittest discover -s tests           # 测试
```

无需 pip install，无需虚拟环境，Python 3.10+ 即可。

不带 `--out` 的运行会**就地改写**三份 README 的雷达区块、`radar/` 与 `assets/`
（这正是每日任务要做的事）；想先看看产出，用 `--out` 写到别处。
两种方式都会从仓库读取运行日志，所以"七天内不重复"仍然生效。

## 目录结构

```
data/        sources.json（源与权重）· taxonomy.json（主线与信号）
             glossary.json（三语 UI 与术语）· baseline.json（人工策展基线）
pairadar/    fetch / distill / charts / render / cli —— 全部标准库
radar/       daily/YYYY-MM-DD.{zh,en,ja}.md · INDEX.md · latest.json · history.json
assets/      自动生成的 SVG 图
docs/        METHODOLOGY.md（方法论、翻译策略与已知局限）
```

## 已知局限（先说清楚）

- 摘要是**抽取式**的：英文原文摘录会标为引用块，不做机器翻译；中日文的分析层由模板与术语表生成，不是逐句翻译。
- 图表标签保持英文：渲染 CJK 字形需要在 CI 内置字体，会牺牲可复现性。
- 排序是**可解释的加权和**，不是学习到的相关性；权重都写在 `data/` 里，可以 fork 后自行调整。
- 源限定为公开的机器可读端点，不抓取正文、不绕过付费墙。

## 贡献

欢迎 PR：新增源（需机器可读）、修正证据标注、补充策展基线、改进三语措辞。请先读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)。

## 许可

- 代码：[MIT](LICENSE)
- 内容（策展文字与生成页面）：[CC BY 4.0](LICENSE-CONTENT)

---

*本仓库为技术观察，不构成投资建议、产品承诺或安全认证结论。论文与厂商结论均以其自报为准，引用前请回到原始链接核对。*
