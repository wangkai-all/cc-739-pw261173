"""纯 Python 穷举求解器，只用于测试中与 CP-SAT 交叉核对。

通过有界 DFS 枚举所有单项报价交付量与组合拆份数（均为整数），
逐方案校验约束并取目标最小。实现刻意独立于 solver.py，避免同源错误。
仅适合小规模实例（报价数 <= 8、变量域较小）。
"""
from __future__ import annotations

from itertools import product

from .model import Batch, SolveConfig, demand_constraint_coeffs
from .solver import aggregate_capacities, _eligible, SolveInputError, BIG_PENALTY


def _useful_upper_bounds(batch: Batch) -> dict[str, int]:
    """每种物料“可能有用”的最大采购量：自身需求 + 作为替代源能覆盖的需求。
    超过这个量的采购只会浪费钱，可安全从枚举域剔除。"""
    bounds = {m.id: 0 for m in batch.materials}
    for d in batch.demand:
        bounds[d.material_id] = d.quantity
    for s in batch.substitutes:
        # 覆盖 num/den 单位 need 需要 1 单位 source
        need_qty = next((d.quantity for d in batch.demand
                         if d.material_id == s.need_material_id), 0)
        bounds[s.source_material_id] += -(-need_qty * s.den // s.num)
    return bounds


def _enumerate_feasible(batch: Batch, config: SolveConfig,
                        diagnostic: bool = False):
    singles, combos, _ = _eligible(batch, config)
    lock_map = {lk.quote_id: lk.quantity for lk in config.locks}
    per_cap, total_cap = aggregate_capacities(batch)
    coeff_table = demand_constraint_coeffs(batch)
    demand_map = {d.material_id: d.quantity for d in batch.demand}
    useful = _useful_upper_bounds(batch)

    # 构建变量列表：(quote, 可取值列表)
    domains: list[tuple[str, str, list[int], object]] = []
    for q in singles:
        hi = min(q.max_qty, useful.get(q.material_id, q.max_qty))
        hi = min(hi, per_cap.get((q.supplier_id, q.material_id), hi))
        hi = min(hi, total_cap.get(q.supplier_id, hi))
        if q.id in lock_map:
            vals = [lock_map[q.id]]
        elif hi <= 0:
            vals = [0]
        else:
            vals = [0] + [v for v in range(q.moq, hi + 1) if v >= q.moq]
        domains.append(("single", q.id, vals, q))
    for q in combos:
        step = q.split_rule.step if q.split_rule else 1
        possible = [0] + list(range(q.min_multiplier, q.divisor + 1, step))
        if q.id in lock_map:
            if lock_map[q.id] not in possible:
                raise SolveInputError(
                    f"锁定拆份数 {lock_map[q.id]} 对 {q.id} 不可行")
            possible = [lock_map[q.id]]
        domains.append(("combo", q.id, possible, q))

    best = None
    for combo_values in product(*[d[2] for d in domains]):
        single_qty: dict[str, int] = {}
        combo_mult: dict[str, int] = {}
        for (kind, qid, _, q), val in zip(domains, combo_values):
            if kind == "single":
                single_qty[qid] = val
            else:
                combo_mult[qid] = val

        # ---- 供应商产能
        per_used: dict[tuple[str, str], int] = {}
        total_used: dict[str, int] = {}
        for q in singles:
            v = single_qty[q.id]
            if v:
                per_used[(q.supplier_id, q.material_id)] = \
                    per_used.get((q.supplier_id, q.material_id), 0) + v
                total_used[q.supplier_id] = \
                    total_used.get(q.supplier_id, 0) + v
        for q in combos:
            tv = combo_mult[q.id]
            if tv:
                for ln in q.lines:
                    qv = ln.quantity * tv // q.divisor
                    per_used[(q.supplier_id, ln.material_id)] = \
                        per_used.get((q.supplier_id, ln.material_id), 0) + qv
                    total_used[q.supplier_id] = \
                        total_used.get(q.supplier_id, 0) + qv
        if any(v > per_cap.get(k, 10 ** 18) for k, v in per_used.items()):
            continue
        if any(v > total_cap.get(sid, 10 ** 18)
               for sid, v in total_used.items()):
            continue

        # ---- 需求覆盖（整数化系数）
        shortage = {}
        feasible_strict = True
        for n in batch.material_ids:
            denom, coeffs = coeff_table[n]
            lhs = 0
            for source, c in coeffs.items():
                supplied = 0
                for q in singles:
                    if q.material_id == source:
                        supplied += single_qty[q.id]
                for q in combos:
                    tv = combo_mult[q.id]
                    if tv:
                        for ln in q.lines:
                            if ln.material_id == source:
                                supplied += ln.quantity * tv // q.divisor
                lhs += c * supplied
            need_scaled = denom * demand_map.get(n, 0)
            gap_scaled = max(0, need_scaled - lhs)
            if gap_scaled % denom != 0:
                # 等价覆盖非整数：缺口按 ceil 计，分数部分不满足需求
                sh = -(-gap_scaled // denom)
            else:
                sh = gap_scaled // denom
            shortage[n] = sh
            if sh:
                feasible_strict = False

        if config.mode == "strict" and not diagnostic and feasible_strict is False:
            continue

        # ---- 最多中标家数
        winners = set()
        for q in singles:
            if single_qty[q.id] > 0:
                winners.add(q.supplier_id)
        for q in combos:
            if combo_mult[q.id] > 0:
                winners.add(q.supplier_id)
        if config.max_winning_suppliers is not None and \
                len(winners) > config.max_winning_suppliers:
            continue

        # ---- 目标
        cost = 0
        for q in singles:
            cost += q.unit_price_scaled * single_qty[q.id]
        for q in combos:
            cost += q.price_scaled * combo_mult[q.id] // q.divisor
        for n, sh in shortage.items():
            if diagnostic:
                cost += BIG_PENALTY * sh
            else:
                cost += config.penalties_scaled.get(
                    n, config.default_penalty_scaled) * sh

        candidate = (cost, dict(single_qty), dict(combo_mult),
                     dict(shortage), sorted(winners))
        if best is None or cost < best[0]:
            best = candidate
    return best


def solve_exhaustive(batch: Batch, config: SolveConfig, *,
                     diagnostic: bool = False) -> dict:
    best = _enumerate_feasible(batch, config, diagnostic=diagnostic)
    if best is None:
        return {"status": "infeasible", "objective_scaled": None,
                "single_quantities": {}, "combo_multipliers": {},
                "shortage": {}, "winners": []}
    cost, sq, cm, sh, winners = best
    return {
        "status": "optimal",
        "objective_scaled": cost,
        "single_quantities": sq,
        "combo_multipliers": cm,
        "shortage": {k: v for k, v in sh.items() if v > 0},
        "winners": winners,
    }
