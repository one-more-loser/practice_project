# SQL-R1 路线核对（主执行直接核对，子代理 A 未交付）

> 2026-10-24 12:10 +08:00。子代理 A 运行约 1 小时未写文件，已中断；本文件由主执行直接读取官方仓库/论文核实，作为 `参考项目复用与修改清单.md` 的证据。未核实项标注“未核实”。

## 1. 仓库与版本

| 项 | 值 | 证据 |
|---|---|---|
| URL | https://github.com/DataArcTech/SQL-R1 | GitHub |
| commit | `ccfb511e81fdb3a8e87bd3da7f3d71882cedd532`（2025-11-20） | GitHub API `/commits/main` |
| 许可证 | Apache-2.0 | GitHub API `/repos/...` license |
| 论文 | arXiv:2504.08600v2；NeurIPS 2025 | README |
| 模型 | SQL-R1-3B/7B/14B，底座 Qwen2.5-Coder-Instruct | README 模型表 |

## 2. 训练路线

- **SFT 冷启动 → GRPO**；RL 数据为 SynSQL-Complex-5K（复杂样本），输入不含原 CoT。
- GRPO：同一问题采样 G 个 SQL 候选，组内相对优势更新；无 value model。
- 推理：生成多个候选，执行后按 self-consistency 投票选最终 SQL。

## 3. 奖励（论文 §2.3.3）

| 分量 | 取值 | 说明 |
|---|---|---|
| Format `S_f` | +1 / −1 | 必须有 `think`、`<answer>`、```sql``` 结构 |
| Execution `S_e` | +2 可执行 / 0 格式错 / −2 不可执行 | 不可执行不得后续奖励 |
| Result `S_r` | +3 结果正确 / 0 格式错或不可执行 / −3 结果错 | EX |
| Length `S_l` | 仅结果正确时给；`0.5*Stl+Sal`；超长有惩罚 | `Stl=(len_think+len_answer)/MAX`；`Sal=len_sql/len_answer` |

## 4. 资源与依赖

- 官方要求：**8×80GB+ GPU** 训练；2×40GB 推理。
- Python 3.9+、CUDA 12.0+、torch 2.4.0、vllm 0.6.3、verl、flash-attn。
- 结论：单卡 AutoDL 不能原样复现；本项目需重写训练栈/缩小规模，且单卡可行性未实测。

## 5. 对本项目的影响

- 可复用：阶段顺序、奖励分层、执行结果评测、多候选选择思想。
- 必须重写：底座（Qwen3-8B-Base）、单卡训练栈、奖励（格式/长度不能压过语义；加契约/安全/未知值/反作弊）、数据（业务 Schema/指标/权限）、执行（03 隔离沙箱）。
- 不可复用：权重、SynSQL/Spider/BIRD 数据、8×80GB 配置、论文成绩作为本项目成绩。

## 6. 未核实

- `verl` 具体版本与单卡兼容性；
- 代码是否包含完整 GRPO 入口/自定义 reward 实现；
- 许可证对“复制代码片段”的 NOTICE 要求细节（Apache-2.0 常规要求保留版权/NOTICE）。
