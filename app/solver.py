"""CP-SAT 组合报价求解。

设计要点：
- 全部整数变量；替代料通过 LCM 去分母后进入需求约束。
- 组合报价用整包/拆份数 t 表示，t=step*u，只能整体接受或拒绝，
  拆包按 model.SplitRule 的明确规则执行，不存在按行拆分。
- 严格模式缺口固定为 0；罚金模式允许缺口并在目标中计费。
- 求解状态如实上报：FEASIBLE 绝不标成最优，界、缺口、耗时全部带回。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from fractions import Fraction

from ortools.sat.python import cp_model

from .model import (
    Batch, ComboQuote, Lock, SingleQuote, SolveConfig,
    demand_constraint_coeffs, MONEY_SCALE,
)

BIG_PENALTY = 10 ** 12  # 诊断模式下的单位缺口罚金（分），远高于任何采购成本


class SolveInputError(Exception):
    pass


# ------------------------------------------------------------- 预处理


def aggregate_capacities(batch: Batch):
    """返回 (per_supplier_material, supplier_total)，重复行求和。"""
    per: dict[tuple[str, str], int] = {}
    total: dict[str, int] = {}
    for c in batch.capacities:
        if c.material_id is None:
            total[c.supplier_id] = total.get(c.supplier_id, 0) + c.quantity
        else:
            key = (c.supplier_id, c.material_id)
            per[key] = per.get(key, 0) + c.quantity
    return per, total


def _eligible(batch: Batch, config: SolveConfig):
    """返回参与建模的报价；撤回/停用报价剔除并记录原因。"""
    disabled = set(config.disabled_quote_ids)
    locked = {lk.quote_id for lk in config.locks}
    singles, combos, excluded = [], [], []
    for q in batch.all_quotes():
        if q.withdrawn:
            excluded.append((q.id, "报价已撤回"))
        elif q.id in disabled:
            excluded.append((q.id, "手工停用"))
        else:
            if isinstance(q, SingleQuote):
                singles.append(q)
            else:
                combos.append(q)
    for qid in locked:
        q = batch.quote(qid)
        if q is None:
            raise SolveInputError(f"锁定的报价 {qid} 不存在")
        if q.withdrawn:
            raise SolveInputError(
                f"锁定的报价 {qid} 已被撤回，锁定无法继续；请先解除该锁定")
        if qid in disabled:
            raise SolveInputError(
                f"报价 {qid} 同时被停用与锁定，配置冲突")
    return singles, combos, excluded


# ------------------------------------------------------------- 主求解


@dataclass
class _Built:
    model: cp_model.CpModel
    x: dict          # single id -> IntVar 交付量
    z: dict          # quote id -> BoolVar 接受标记
    t: dict          # combo id -> IntVar 拆份数
    u: dict          # combo id -> IntVar t/step
    shortage: dict   # material id -> IntVar
    y: dict          # supplier id -> BoolVar 中标标记
    combo_delivery: dict  # combo id -> {material_id: linear expr}


def _build_model(batch: Batch, config: SolveConfig,
                 demand_override: dict[str, int] | None = None,
                 force_on: set[str] | None = None,
                 diagnostic: bool = False) -> _Built:
    singles, combos, excluded = _eligible(batch, config)
    force_on = set(force_on or ())
    m = cp_model.CpModel()

    per_cap, total_cap = aggregate_capacities(batch)
    coeff_table = demand_constraint_coeffs(batch)
    demand_map = {d.material_id: d.quantity for d in batch.demand}
    if demand_override:
        demand_map.update(demand_override)

    x, z, t, u = {}, {}, {}, {}
    combo_delivery: dict[str, dict[str, object]] = {}

    # ---- 单项报价
    for q in singles:
        x[q.id] = m.NewIntVar(0, q.max_qty, f"x_{q.id}")
        z[q.id] = m.NewBoolVar(f"z_{q.id}")
        if q.moq > 0:
            m.Add(x[q.id] >= q.moq).OnlyEnforceIf(z[q.id])
            m.Add(x[q.id] == 0).OnlyEnforceIf(z[q.id].Not())
        else:
            # 无起订量：z 与“x>0”互为充要条件
            m.Add(x[q.id] >= 1).OnlyEnforceIf(z[q.id])
            m.Add(x[q.id] == 0).OnlyEnforceIf(z[q.id].Not())

    # ---- 组合报价（整体接受/拒绝 + 明确拆包）
    for q in combos:
        d, kmin, step = q.divisor, q.min_multiplier, (
            q.split_rule.step if q.split_rule else 1)
        uu = m.NewIntVar(0, d // step, f"u_{q.id}")
        tt = m.NewIntVar(0, d, f"t_{q.id}")
        m.Add(tt == step * uu)
        zz = m.NewBoolVar(f"z_{q.id}")
        m.Add(tt >= kmin).OnlyEnforceIf(zz)
        m.Add(tt == 0).OnlyEnforceIf(zz.Not())
        u[q.id], t[q.id], z[q.id] = uu, tt, zz
        deliveries = {}
        for ln in q.lines:
            per_u = ln.quantity * step // d  # 整数（解析期已保证）
            deliveries[ln.material_id] = per_u * uu
        combo_delivery[q.id] = deliveries

    # ---- 锁定（重算时份额不得被替换）
    for lk in config.locks:
        q = batch.quote(lk.quote_id)
        if isinstance(q, SingleQuote):
            m.Add(x[lk.quote_id] == lk.quantity)
        else:
            step = q.split_rule.step if q.split_rule else 1
            if lk.quantity % step != 0:
                raise SolveInputError(
                    f"锁定拆份数 {lk.quantity} 不是步长 {step} 的倍数")
            m.Add(t[lk.quote_id] == lk.quantity)

    # ---- 强制接受（why-not 分析用）
    for qid in force_on:
        q = batch.quote(qid)
        if q is None or q.withdrawn or qid in config.disabled_quote_ids:
            raise SolveInputError(f"无法强制接受报价 {qid}")
        m.Add(z[qid] == 1)

    # ---- 供应商产能
    def _supplier_terms(sid: str, material_id: str | None):
        terms = []
        for q in singles:
            if q.supplier_id != sid:
                continue
            if material_id is None or q.material_id == material_id:
                terms.append(x[q.id])
        for q in combos:
            if q.supplier_id != sid:
                continue
            for mid, expr in combo_delivery[q.id].items():
                if material_id is None or mid == material_id:
                    terms.append(expr)
        return terms

    for (sid, mid), cap in per_cap.items():
        terms = _supplier_terms(sid, mid)
        if terms:
            m.Add(sum(terms) <= cap)
    for sid, cap in total_cap.items():
        terms = _supplier_terms(sid, None)
        if terms:
            m.Add(sum(terms) <= cap)

    # ---- 需求覆盖（替代料 LCM 整数化）与缺口
    shortage = {}
    for n in batch.material_ids:
        denom, coeffs = coeff_table[n]
        need_qty = demand_map.get(n, 0)
        terms = []
        for source, c in coeffs.items():
            for q in singles:
                if q.material_id == source:
                    terms.append(c * x[q.id])
            for q in combos:
                expr = combo_delivery[q.id].get(source)
                if expr is not None:
                    terms.append(c * expr)
        sh = m.NewIntVar(0, max(need_qty, 0), f"short_{n}")
        shortage[n] = sh
        lhs = sum(terms) + denom * sh if terms else denom * sh
        m.Add(lhs >= denom * need_qty)
        if config.mode == "strict" and not diagnostic:
            m.Add(sh == 0)

    # ---- 最多中标家数
    y: dict[str, cp_model.IntVar] = {}
    limit = config.max_winning_suppliers
    if limit is not None:
        for s in batch.suppliers:
            yy = m.NewBoolVar(f"y_{s.id}")
            y[s.id] = yy
        for q in singles + combos:
            m.Add(z[q.id] <= y[q.supplier_id])
        m.Add(sum(y.values()) <= limit)

    # ---- 目标
    cost_terms = []
    for q in singles:
        cost_terms.append(q.unit_price_scaled * x[q.id])
    for q in combos:
        per_t_price = q.price_scaled // q.divisor
        cost_terms.append(per_t_price * t[q.id])
    for n, sh in shortage.items():
        if diagnostic:
            pen = BIG_PENALTY
        else:
            pen = config.penalties_scaled.get(
                n, config.default_penalty_scaled)
        cost_terms.append(pen * sh)
    m.Minimize(sum(cost_terms))

    return _Built(m, x, z, t, u, shortage, y, combo_delivery)


def _run_solver(model: cp_model.CpModel, config: SolveConfig,
                time_limit: float | None = None):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = (
        config.time_limit_seconds if time_limit is None else time_limit)
    solver.parameters.num_workers = config.num_workers
    solver.parameters.random_seed = config.random_seed
    t0 = time.perf_counter()
    status = solver.Solve(model)
    elapsed = time.perf_counter() - t0
    return solver, status, elapsed


_STATUS_NAME = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.MODEL_INVALID: "model_invalid",
    cp_model.UNKNOWN: "unknown",
}


# ------------------------------------------------------- 结果提取


def _extract(batch: Batch, config: SolveConfig, built: _Built,
             solver: cp_model.CpSolver, excluded: list,
             diagnostic: bool) -> dict:
    x, z, t, shortage = built.x, built.z, built.t, built.shortage
    locked_ids = {lk.quote_id for lk in config.locks}
    allocations = []

    purchase_cost = 0
    for q in batch.single_quotes:
        if q.withdrawn or q.id in config.disabled_quote_ids:
            reason = "报价已撤回" if q.withdrawn else "手工停用"
            allocations.append({
                "quote_id": q.id, "kind": "single",
                "supplier_id": q.supplier_id, "material_id": q.material_id,
                "accepted": False, "quantity": 0, "deliveries": [],
                "cost_yuan": 0.0, "locked": False,
                "unit_price_yuan": q.unit_price_scaled / MONEY_SCALE,
                "reason": reason, "status": q.status,
            })
            continue
        qv = solver.Value(x[q.id])
        cost = qv * q.unit_price_scaled
        purchase_cost += cost
        allocations.append({
            "quote_id": q.id, "kind": "single",
            "supplier_id": q.supplier_id, "material_id": q.material_id,
            "accepted": qv > 0, "quantity": qv,
            "deliveries": [{"material_id": q.material_id, "quantity": qv}],
            "cost_yuan": round(cost / MONEY_SCALE, 2),
            "locked": q.id in locked_ids,
            "unit_price_yuan": q.unit_price_scaled / MONEY_SCALE,
            "moq": q.moq, "max_qty": q.max_qty, "status": q.status,
            "reason": None,
        })

    for q in batch.combo_quotes:
        if q.withdrawn or q.id in config.disabled_quote_ids:
            reason = "报价已撤回" if q.withdrawn else "手工停用"
            allocations.append({
                "quote_id": q.id, "kind": "combo",
                "supplier_id": q.supplier_id, "accepted": False,
                "multiplier": 0, "deliveries": [], "cost_yuan": 0.0,
                "locked": False, "package_price_yuan":
                    q.price_scaled / MONEY_SCALE,
                "reason": reason, "status": q.status,
            })
            continue
        tv = solver.Value(t[q.id])
        d = q.divisor
        deliveries = []
        for ln in q.lines:
            deliveries.append({
                "material_id": ln.material_id,
                "quantity": ln.quantity * tv // d,
            })
        cost = q.price_scaled * tv // d
        purchase_cost += cost
        allocations.append({
            "quote_id": q.id, "kind": "combo",
            "supplier_id": q.supplier_id,
            "accepted": tv > 0, "multiplier": tv, "divisor": d,
            "deliveries": deliveries,
            "cost_yuan": round(cost / MONEY_SCALE, 2),
            "locked": q.id in locked_ids,
            "package_price_yuan": q.price_scaled / MONEY_SCALE,
            "status": q.status, "reason": None,
        })

    # ---- 覆盖与缺口（用 Fraction 精确折算）
    cover = demand_constraint_coeffs(batch)
    coverage = []
    penalty_cost = 0
    total_short: dict[str, int] = {}
    for n in batch.material_ids:
        denom, coeffs = cover[n]
        eq_covered = Fraction(0)
        sources_detail: dict[str, int] = {}
        for source, c in coeffs.items():
            supplied = 0
            for al in allocations:
                if not al["accepted"]:
                    continue
                for dl in al["deliveries"]:
                    if dl["material_id"] == source:
                        supplied += dl["quantity"]
            if supplied:
                sources_detail[source] = supplied
                # c = num * denom / den  => 等价量 = supplied * num / den
                eq_covered += Fraction(supplied * c, denom)
        sh = solver.Value(shortage[n])
        total_short[n] = sh
        demand_qty = next((d.quantity for d in batch.demand
                           if d.material_id == n), 0)
        if not diagnostic:
            pen = config.penalties_scaled.get(
                n, config.default_penalty_scaled)
            penalty_cost += sh * pen
        coverage.append({
            "material_id": n, "demand": demand_qty,
            "equivalent_covered": float(eq_covered),
            "equivalent_covered_exact": str(eq_covered),
            "shortage": sh,
            "sources": [{"material_id": s, "quantity": v}
                        for s, v in sorted(sources_detail.items())],
        })

    winners = sorted({al["supplier_id"] for al in allocations
                      if al["accepted"]})

    return {
        "allocations": allocations,
        "coverage": coverage,
        "shortages": [{"material_id": n, "quantity": v}
                      for n, v in total_short.items() if v > 0],
        "winners": winners,
        "purchase_cost_scaled": purchase_cost,
        "penalty_cost_scaled": penalty_cost,
        "objective_scaled": purchase_cost + penalty_cost,
    }


# ------------------------------------------------------- 影子分析


def _shadow_analysis(batch: Batch, config: SolveConfig,
                     base_obj: int, time_limit: float) -> dict:
    """离散边际：把需求/家数约束的 RHS 各扰动 1 个单位后重求。
    任一重求未证最优，则该项标 N/A 并说明原因——绝不拿可行解凑数。"""
    demand_shadow = []
    for d in batch.demand:
        if d.quantity <= 0:
            continue
        row = {"material_id": d.material_id,
               "relax_one_unit_saves_yuan": None,
               "tighten_one_unit_costs_yuan": None,
               "tighten_feasible": True, "note": ""}
        # 放松 1 单位（需求 -1）
        r_minus = solve(batch, config,
                        demand_override={d.material_id: d.quantity - 1},
                        time_limit=time_limit, quiet=True)
        if r_minus["status"] == "optimal":
            row["relax_one_unit_saves_yuan"] = round(
                (base_obj - r_minus["objective_scaled"]) / MONEY_SCALE, 4)
        else:
            row["note"] = "需求-1 重求未在时限内证最优，边际值不可用；"
        # 收紧 1 单位（需求 +1）
        r_plus = solve(batch, config,
                       demand_override={d.material_id: d.quantity + 1},
                       time_limit=time_limit, quiet=True)
        if r_plus["status"] == "infeasible":
            row["tighten_feasible"] = False
            row["note"] += "再增加 1 单位需求后无可行方案；"
        elif r_plus["status"] == "optimal":
            row["tighten_one_unit_costs_yuan"] = round(
                (r_plus["objective_scaled"] - base_obj) / MONEY_SCALE, 4)
        else:
            row["note"] += "需求+1 重求未在时限内证最优，边际值不可用；"
        demand_shadow.append(row)

    supplier_shadow = None
    if config.max_winning_suppliers is not None:
        cfg_loose = SolveConfig(**{**config.__dict__,
                                   "max_winning_suppliers":
                                   config.max_winning_suppliers + 1})
        r_loose = solve(batch, cfg_loose, time_limit=time_limit, quiet=True)
        if r_loose["status"] == "optimal":
            supplier_shadow = {
                "limit": config.max_winning_suppliers,
                "relax_one_more_supplier_saves_yuan": round(
                    (base_obj - r_loose["objective_scaled"]) / MONEY_SCALE, 2),
                "relaxed_winners": r_loose["result"]["winners"],
            }
        else:
            supplier_shadow = {"limit": config.max_winning_suppliers,
                               "note": "放宽一家重求未证最优，影子值不可用"}
    return {"demand_marginals": demand_shadow,
            "supplier_limit": supplier_shadow}


# ------------------------------------------------------- why-not


def why_not_combo(batch: Batch, config: SolveConfig, quote_id: str,
                  time_limit: float | None = None) -> dict:
    """强制接受某个未中选组合，看目标恶化多少、挤掉了谁、降价多少才入选。"""
    q = batch.quote(quote_id)
    if not isinstance(q, ComboQuote):
        raise SolveInputError(f"{quote_id} 不是组合报价")
    if q.withdrawn:
        return {"quote_id": quote_id, "blocked": True,
                "reasons": ["报价已撤回，无法参与分配"]}
    if quote_id in config.disabled_quote_ids:
        return {"quote_id": quote_id, "blocked": True,
                "reasons": ["报价在当前情景中被手工停用"]}

    base = solve(batch, config, time_limit=time_limit, quiet=True)
    forced = solve(batch, config, force_on={quote_id},
                   time_limit=time_limit, quiet=True)
    tl = time_limit or config.time_limit_seconds
    out = {"quote_id": quote_id, "blocked": False, "reasons": []}

    if forced["status"] == "infeasible":
        out["blocked"] = True
        diag = solve(batch, config, force_on={quote_id},
                     time_limit=tl, quiet=True, diagnostic=True)
        if diag["status"] in ("optimal", "feasible_only"):
            sh = diag["result"]["shortages"]
            out["reasons"].append(
                "强制接受后严格口径下不可行；即使允许缺口，最小缺口为 "
                + "、".join(f"{x['material_id']} 缺 {x['quantity']}"
                           for x in sh))
        else:
            out["reasons"].append("强制接受后模型不可行（产能/锁定/家数约束冲突）")
        return out

    if base["status"] not in ("optimal", "feasible_only"):
        out["reasons"].append("基准方案未求出可行解，无法做对比分析")
        out["forced"] = forced
        return out

    base_ids = {a["quote_id"] for a in base["result"]["allocations"]
                if a["accepted"]}
    forced_ids = {a["quote_id"] for a in forced["result"]["allocations"]
                  if a["accepted"]}
    out["dropped_quotes"] = sorted(base_ids - forced_ids)
    out["added_quotes"] = sorted(forced_ids - base_ids)
    delta = forced["objective_scaled"] - base["objective_scaled"]
    out["extra_cost_yuan"] = round(delta / MONEY_SCALE, 2)
    out["forced_objective_yuan"] = round(
        forced["objective_scaled"] / MONEY_SCALE, 2)
    if delta > 0:
        per_t = delta // max(q.min_multiplier, 1)
        if base["status"] == "optimal" and forced["status"] == "optimal":
            out["reasons"].append(
                f"强制接受使总成本增加 {delta / MONEY_SCALE:g} 元；在该强制分配下，"
                f"整包价至少需下降约 {per_t / MONEY_SCALE:g} 元/份才可能入选")
        else:
            out["reasons"].append(
                f"按当前可行解估算，强制接受使成本增加 "
                f"{delta / MONEY_SCALE:g} 元（约合 {per_t / MONEY_SCALE:g} "
                "元/份）；由于基准或强制方案未在时限内证明最优，"
                "该门槛是估计值而非严格证明值")
    elif delta == 0 and base["status"] == "optimal":
        out["reasons"].append("存在并列最优；当前求解器返回了另一组同成本方案，"
                              "强制接受该组合不会增加成本")
    if config.max_winning_suppliers is not None:
        out["base_winners"] = base["result"]["winners"]
        out["forced_winners"] = forced["result"]["winners"]
    out["forced"] = {
        "status": forced["status"],
        "allocations": forced["result"]["allocations"],
        "coverage": forced["result"]["coverage"],
    }
    return out


# ------------------------------------------------------- 入口


def solve(batch: Batch, config: SolveConfig, *,
          demand_override: dict[str, int] | None = None,
          force_on: set[str] | None = None,
          time_limit: float | None = None,
          quiet: bool = False,
          diagnostic: bool = False) -> dict:
    tl = config.time_limit_seconds if time_limit is None else time_limit
    built = _build_model(batch, config, demand_override=demand_override,
                         force_on=force_on, diagnostic=diagnostic)
    solver, status, elapsed = _run_solver(built.model, config, tl)
    name = _STATUS_NAME.get(status, "unknown")
    singles, combos, excluded = _eligible(batch, config)

    out: dict = {
        "mode": config.mode,
        "status": name,
        "solved_in_seconds": round(elapsed, 4),
        "time_limit_seconds": tl,
        "result": None,
        "objective_yuan": None,
        "objective_scaled": None,
        "best_bound_yuan": None,
        "gap_pct": None,
        "excluded": [{"quote_id": qid, "reason": why}
                     for qid, why in excluded],
        "warnings": [],
        "diagnostic": diagnostic,
    }

    if name == "infeasible":
        out["status"] = "infeasible"
        if not diagnostic and config.mode == "strict":
            diag = solve(batch, config, force_on=force_on,
                         time_limit=tl, quiet=True, diagnostic=True)
            if diag["status"] in ("optimal", "feasible_only"):
                out["infeasibility"] = {
                    "minimum_shortage": diag["result"]["shortages"],
                    "note": "严格满足需求不可行；以上为允许缺口时的最小缺口"
                            "（按超大罚金最小化得到，仅作诊断，不产生费用）",
                }
        return out

    if name == "unknown" or name == "model_invalid":
        out["status"] = "no_solution"
        bound = solver.BestObjectiveBound()
        if bound is not None:
            out["best_bound_yuan"] = round(bound / MONEY_SCALE, 2)
        out["warnings"].append(
            f"时限 {tl:g}s 内未找到任何可行解，不能给出分配方案；"
            "下界仅用于判断离最优还有多远")
        return out

    # OPTIMAL 或 FEASIBLE
    result = _extract(batch, config, built, solver, excluded, diagnostic)
    out["result"] = result
    out["objective_scaled"] = result["objective_scaled"]
    out["objective_yuan"] = round(result["objective_scaled"] / MONEY_SCALE, 2)
    bound = solver.BestObjectiveBound()
    if bound is not None:
        out["best_bound_yuan"] = round(bound / MONEY_SCALE, 2)
        if result["objective_scaled"] > 0:
            out["gap_pct"] = round(
                100.0 * (result["objective_scaled"] - bound)
                / result["objective_scaled"], 4)

    if name == "feasible":
        out["status"] = "feasible_only"
        out["warnings"].append(
            f"到达时间限制 {tl:g}s，当前是可行解而非最优解；"
            f"目标 {out['objective_yuan']} 元，已证明的下界 "
            f"{out['best_bound_yuan']} 元，差距 {out['gap_pct']}%。"
            "本方案不得标记为最优")
    else:
        out["status"] = "optimal"

    # 影子分析只在严格证最优的基准上做
    if not quiet and not diagnostic and name == "optimal" and not force_on \
            and not demand_override:
        shadow_tl = min(max(tl, 5.0), 15.0)
        try:
            out["shadow"] = _shadow_analysis(
                batch, config, result["objective_scaled"], shadow_tl)
        except SolveInputError as exc:
            out["warnings"].append(f"影子分析跳过：{exc}")
    return out
