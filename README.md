# 采购组合报价分析

解决「这几种材料一起买才有这个价格」的采购分配问题：导入需求、单项报价、
**整体接受/拒绝的组合报价**、供应商产能与最多中标家数，求满足需求的
最低总成本分配。组合报价不允许逐行拆买；部分交付只能走报价上明确声明的
拆包规则。缺货、最低起订量（MOQ）、替代料换算共同进入约束。

## 业务口径

| 口径 | 需求约束 | 结果 |
| --- | --- | --- |
| 严格满足 `strict` | 缺口必须为 0 | 不可行时给出**最小缺口诊断**（超大罚金最小化，仅诊断不计费） |
| 缺口罚金 `penalty` | 允许缺口，按单位罚金计费 | 目标 = 采购金额 + 缺口罚金 |

求解状态如实区分 **optimal（已证明最优）/ feasible_only（仅可行解，
到达时限未证最优，界面与导出均明确标注，严禁按最优执行）/ infeasible /
no_solution**，并同时给出已证明的下界（best bound）与最优差距 gap。

## 核心建模约定（全部整数运算）

- 金额解析时 ×100 转为分；价格超过两位小数直接校验拒绝。
- 替代料 `1 单位需求物料 = num/den 单位源物料`，用 LCM 去分母后写入
  整数需求约束；覆盖量用 `Fraction` 精确展示。
- 单项报价：`x` 为交付量，MOQ 用布尔指示变量做「0 或 ≥ MOQ」。
- 组合报价：拆份数 `t ∈ {0} ∪ [min, divisor]`，且 `t` 必须是整数步长
  `step` 的倍数（解析期由各行数量与 divisor 自动算出，保证拆包后每行
  交付量为整数）；`t=0` 即整包拒绝，不存在按行接受。
- 产能支持「供应商×物料」与「供应商总量（material_id=null）」两类，重复行求和。
- 锁定：单项锁交付量、组合锁拆份数；重算时是硬约束，**不会被偷偷替换**；
  锁定已撤回报价会直接报错而不是静默换供应商。
- 影子分析：整数规划没有 LP 对偶，采用 RHS 扰动 ±1 重求的离散边际；
  任一重求未证最优，该项标 N/A，绝不拿可行解凑影子价格。

## 数据失效与情景

- 同一批需求的不同配置（口径、停用、锁定、罚金）保存为互相独立的情景。
- 报价撤回 / 批次输入变更：输入指纹变化，所有已解情景标记 `stale`，
  旧结果保留可查但禁止覆盖保存；依赖被撤回报价的情景会被列出，
  必须先解除失效锁定，再克隆为新情景重算。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main --host 127.0.0.1 --port 8080 --data data
# 浏览器打开 http://127.0.0.1:8080/
```

无外部 Web 框架，后端仅用标准库 `http.server`；前端为原生 HTML/JS，
无构建步骤；持久化为 `data/<batch_id>/` 下的 JSON 文件。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 内置示例（可穷举小实例 + 冲突实例） |
| POST/PUT | `/api/batches`、`/api/batches/{id}` | 导入 / 整体更新输入（更新使情景失效） |
| POST | `/api/batches/{id}/withdraw` | 撤回报价，级联失效 |
| POST | `/api/batches/{id}/solve` | 求解（不保存） |
| POST | `/api/batches/{id}/why-not` | 强制接受某组合，解释为何未中选及降价门槛 |
| POST | `/api/batches/{id}/scenarios[/{sid}/solve]` | 情景创建 / 求解并保存 |
| GET | `/api/batches/{id}/scenarios/{sid}/export.csv` | 导出采购明细+覆盖+影子 |

## 测试与核对

```bash
cd tests && ../.venv/bin/pytest -q          # 26 项：穷举交叉核对/口径/失效/校验
../.venv/bin/python tests/e2e_api.py        # 对运行中的服务做 32 项 HTTP 端到端核对
```

- `app/exhaustive.py` 是与 CP-SAT **独立实现**的纯 Python 有界穷举器，
  在小实例的配置网格（模式 × 停用子集 × 中标家数 × 锁定）上逐方案对账，
  金额、覆盖数量、中标家数必须完全一致。
- 冲突实例核对：严格口径不可行且最小缺口为 X=30、Y=30；罚金口径
  采购 520 元 + 罚金 6000 元 = 6520 元，与穷举器一致。
- 超时诚实性测试用 0.0005s 时限逼出 feasible_only/no_solution，
  验证状态标注、下界与「不得标记为最优」告警。

## 目录

```
app/        model.py 模型与校验 · solver.py CP-SAT · exhaustive.py 穷举器
            storage.py 情景持久化 · server.py HTTP · sample.py 示例
web/        原生单页（index.html / app.js / styles.css）
tests/      pytest 交叉核对 + HTTP 端到端脚本
data/       运行期生成的批次与情景 JSON
```
