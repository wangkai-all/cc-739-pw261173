"""采购组合报价分析 —— 领域模型与校验。

所有金额在解析时放大 100 倍转为整数（分），所有替代换算通过 LCM
去分母转为整数系数，保证求解器内部全部是整数运算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

MONEY_SCALE = 100  # 元 -> 分


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------- 数据结构


@dataclass
class Material:
    id: str
    name: str
    unit: str = ""


@dataclass
class Demand:
    material_id: str
    quantity: int


@dataclass
class Supplier:
    id: str
    name: str


@dataclass
class Capacity:
    supplier_id: str
    material_id: str | None  # None = 该供应商总产能（跨料合计）
    quantity: int


@dataclass
class Substitute:
    """1 单位 source_material 可覆盖 num/den 单位 need_material 的需求。"""
    need_material_id: str
    source_material_id: str
    num: int
    den: int


@dataclass
class SingleQuote:
    id: str
    supplier_id: str
    material_id: str
    unit_price_scaled: int  # 分/单位
    moq: int
    max_qty: int
    status: str = "active"  # active | withdrawn
    note: str = ""

    @property
    def withdrawn(self) -> bool:
        return self.status == "withdrawn"


@dataclass
class ComboLine:
    material_id: str
    quantity: int


@dataclass
class SplitRule:
    """明确拆包规则：接受份数 t ∈ {0} ∪ [min_multiplier, divisor]，
    每行交付 quantity * t / divisor。step 是保证所有行交付量为整数的
    最小份数步长（t 必须是 step 的倍数）。"""
    divisor: int
    min_multiplier: int
    step: int = 1


@dataclass
class ComboQuote:
    id: str
    supplier_id: str
    price_scaled: int  # 整包价格（分）
    lines: list[ComboLine]
    split_rule: SplitRule | None = None
    status: str = "active"
    note: str = ""

    @property
    def withdrawn(self) -> bool:
        return self.status == "withdrawn"

    @property
    def divisor(self) -> int:
        return self.split_rule.divisor if self.split_rule else 1

    @property
    def min_multiplier(self) -> int:
        return self.split_rule.min_multiplier if self.split_rule else 1


@dataclass
class Batch:
    id: str
    name: str
    materials: list[Material]
    demand: list[Demand]
    suppliers: list[Supplier]
    capacities: list[Capacity]
    substitutes: list[Substitute]
    single_quotes: list[SingleQuote]
    combo_quotes: list[ComboQuote]
    max_winning_suppliers: int | None = None

    # ---- 便捷索引
    @property
    def material_ids(self) -> list[str]:
        return [m.id for m in self.materials]

    def quote(self, quote_id: str) -> SingleQuote | ComboQuote | None:
        for q in self.single_quotes + self.combo_quotes:
            if q.id == quote_id:
                return q
        return None

    def all_quotes(self) -> list[SingleQuote | ComboQuote]:
        return list(self.single_quotes) + list(self.combo_quotes)


@dataclass
class Lock:
    """锁定一项分配，重算时不得替换。
    单项报价锁数量；组合报价锁拆份数 t（无拆包时 t=1）。"""
    quote_id: str
    quantity: int  # 单项=交付数量；组合=拆份数 t


@dataclass
class SolveConfig:
    mode: str = "strict"  # strict | penalty
    disabled_quote_ids: list[str] = field(default_factory=list)
    locks: list[Lock] = field(default_factory=list)
    # 每种缺料的单位罚金（分/单位），缺省用 default_penalty_scaled
    penalties_scaled: dict[str, int] = field(default_factory=dict)
    default_penalty_scaled: int = 0
    max_winning_suppliers: int | None = None
    time_limit_seconds: float = 30.0
    num_workers: int = 8
    random_seed: int = 20261124


# ---------------------------------------------------------------- 解析


def _as_int(obj: dict, key: str, errors: list[str], path: str,
            positive: bool = False, nonneg: bool = False) -> int | None:
    if key not in obj:
        errors.append(f"{path}: 缺少字段 {key}")
        return None
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, int):
        errors.append(f"{path}.{key}: 必须是整数，实际为 {v!r}")
        return None
    if positive and v <= 0:
        errors.append(f"{path}.{key}: 必须为正数，实际为 {v}")
    if nonneg and v < 0:
        errors.append(f"{path}.{key}: 不能为负数，实际为 {v}")
    return v


def _money(obj: dict, key: str, errors: list[str], path: str) -> int | None:
    if key not in obj:
        errors.append(f"{path}: 缺少字段 {key}")
        return None
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errors.append(f"{path}.{key}: 必须是数字，实际为 {v!r}")
        return None
    if v < 0:
        errors.append(f"{path}.{key}: 价格不能为负，实际为 {v}")
        return None
    scaled = round(v * MONEY_SCALE)
    if abs(scaled / MONEY_SCALE - v) > 1e-9:
        errors.append(f"{path}.{key}: 价格小数位超过 2 位（{v}）")
    return scaled


def parse_batch(raw: dict[str, Any]) -> Batch:
    errors: list[str] = []
    path = "batch"
    if not isinstance(raw, dict):
        raise ValidationError(["输入必须是 JSON 对象"])
    batch_id = str(raw.get("id") or "").strip()
    if not batch_id:
        errors.append("batch.id: 必填")
    name = str(raw.get("name") or batch_id)

    materials: list[Material] = []
    mat_ids: set[str] = set()
    for i, m in enumerate(raw.get("materials", [])):
        p = f"materials[{i}]"
        mid = m.get("id")
        if not mid:
            errors.append(f"{p}.id: 必填")
            continue
        if mid in mat_ids:
            errors.append(f"{p}.id: 物料 {mid} 重复")
        mat_ids.add(mid)
        materials.append(Material(id=mid, name=str(m.get("name") or mid),
                                  unit=str(m.get("unit") or "")))

    suppliers: list[Supplier] = []
    sup_ids: set[str] = set()
    for i, s in enumerate(raw.get("suppliers", [])):
        p = f"suppliers[{i}]"
        sid = s.get("id")
        if not sid:
            errors.append(f"{p}.id: 必填")
            continue
        if sid in sup_ids:
            errors.append(f"{p}.id: 供应商 {sid} 重复")
        sup_ids.add(sid)
        suppliers.append(Supplier(id=sid, name=str(s.get("name") or sid)))

    demand: list[Demand] = []
    demand_map: dict[str, int] = {}
    for i, d in enumerate(raw.get("demand", [])):
        p = f"demand[{i}]"
        mid = d.get("material_id")
        qty = _as_int(d, "quantity", errors, p, nonneg=True)
        if mid not in mat_ids:
            errors.append(f"{p}.material_id: 未知物料 {mid!r}")
        if mid in demand_map:
            errors.append(f"{p}: 物料 {mid} 的需求重复，请合并为一行")
        if qty is not None and mid is not None:
            demand_map[mid] = qty
            demand.append(Demand(material_id=mid, quantity=qty))

    capacities: list[Capacity] = []
    for i, c in enumerate(raw.get("capacities", [])):
        p = f"capacities[{i}]"
        sid = c.get("supplier_id")
        mid = c.get("material_id")
        qty = _as_int(c, "quantity", errors, p, positive=True)
        if sid not in sup_ids:
            errors.append(f"{p}.supplier_id: 未知供应商 {sid!r}")
        if mid is not None and mid not in mat_ids:
            errors.append(f"{p}.material_id: 未知物料 {mid!r}")
        if qty is not None:
            capacities.append(Capacity(supplier_id=sid, material_id=mid,
                                       quantity=qty))

    substitutes: list[Substitute] = []
    for i, s in enumerate(raw.get("substitutes", [])):
        p = f"substitutes[{i}]"
        need = s.get("need_material_id")
        source = s.get("source_material_id")
        num = _as_int(s, "num", errors, p, positive=True)
        den = _as_int(s, "den", errors, p, positive=True)
        if need not in mat_ids:
            errors.append(f"{p}.need_material_id: 未知物料 {need!r}")
        if source not in mat_ids:
            errors.append(f"{p}.source_material_id: 未知物料 {source!r}")
        if need == source:
            errors.append(f"{p}: 替代料不能是物料自身（{need}）")
        if num is not None and den is not None:
            g = math.gcd(num, den)
            substitutes.append(Substitute(need, source, num // g, den // g))

    single_quotes: list[SingleQuote] = []
    combo_quotes: list[ComboQuote] = []
    quote_ids: set[str] = set()

    def _check_qid(qid: str, p: str) -> None:
        if not qid:
            errors.append(f"{p}.id: 必填")
        elif qid in quote_ids:
            errors.append(f"{p}.id: 报价 {qid} 重复")
        else:
            quote_ids.add(qid)

    for i, q in enumerate(raw.get("single_quotes", [])):
        p = f"single_quotes[{i}]"
        qid = q.get("id")
        _check_qid(qid, p)
        sid, mid = q.get("supplier_id"), q.get("material_id")
        price = _money(q, "unit_price", errors, p)
        moq = _as_int(q, "moq", errors, p, nonneg=True)
        max_qty = _as_int(q, "max_qty", errors, p, nonneg=True)
        if sid not in sup_ids:
            errors.append(f"{p}.supplier_id: 未知供应商 {sid!r}")
        if mid not in mat_ids:
            errors.append(f"{p}.material_id: 未知物料 {mid!r}")
        if moq is not None and max_qty is not None and max_qty > 0 and moq > max_qty:
            errors.append(f"{p}: 起订量 {moq} 大于可供量 {max_qty}（等于缺货）")
        status = q.get("status", "active")
        if status not in ("active", "withdrawn"):
            errors.append(f"{p}.status: 非法值 {status!r}")
        if None not in (price, moq, max_qty) and qid:
            single_quotes.append(SingleQuote(
                id=qid, supplier_id=sid, material_id=mid,
                unit_price_scaled=price, moq=moq, max_qty=max_qty,
                status=status, note=str(q.get("note", ""))))

    for i, q in enumerate(raw.get("combo_quotes", [])):
        p = f"combo_quotes[{i}]"
        qid = q.get("id")
        _check_qid(qid, p)
        sid = q.get("supplier_id")
        price = _money(q, "price", errors, p)
        if sid not in sup_ids:
            errors.append(f"{p}.supplier_id: 未知供应商 {sid!r}")
        lines: list[ComboLine] = []
        line_mats: set[str] = set()
        for j, ln in enumerate(q.get("lines", [])):
            lp = f"{p}.lines[{j}]"
            mid = ln.get("material_id")
            qty = _as_int(ln, "quantity", errors, lp, positive=True)
            if mid not in mat_ids:
                errors.append(f"{lp}.material_id: 未知物料 {mid!r}")
            if mid in line_mats:
                errors.append(f"{lp}: 组合内物料 {mid} 重复，请合并行")
            line_mats.add(mid)
            if qty is not None:
                lines.append(ComboLine(material_id=mid, quantity=qty))
        if not lines:
            errors.append(f"{p}: 组合报价至少要有一行物料")

        split_rule = None
        sr = q.get("split_rule")
        if sr is not None:
            d = _as_int(sr, "divisor", errors, f"{p}.split_rule", positive=True)
            kmin = _as_int(sr, "min_multiplier", errors,
                           f"{p}.split_rule", positive=True)
            if d is not None and kmin is not None:
                if kmin > d:
                    errors.append(f"{p}.split_rule: 最小份数 {kmin} 大于"
                                  f"总份数 {d}")
                else:
                    step = 1
                    for ln in lines:
                        g = math.gcd(d, ln.quantity)
                        step = lcm(step, d // g)
                    if kmin % step != 0:
                        errors.append(
                            f"{p}.split_rule: 最小份数 {kmin} 不是整数步长 "
                            f"{step} 的倍数，无法保证拆包交付量为整数")
                    else:
                        split_rule = SplitRule(
                            divisor=d, min_multiplier=kmin, step=step)
        if price is not None and split_rule is not None:
            d = split_rule.divisor
            if price % d != 0:
                errors.append(f"{p}: 整包价 {price / MONEY_SCALE} 元无法被"
                              f" divisor={d} 整除，请调整价格或拆包规则")
        status = q.get("status", "active")
        if status not in ("active", "withdrawn"):
            errors.append(f"{p}.status: 非法值 {status!r}")
        if price is not None and lines and qid:
            combo_quotes.append(ComboQuote(
                id=qid, supplier_id=sid, price_scaled=price, lines=lines,
                split_rule=split_rule, status=status,
                note=str(q.get("note", ""))))

    max_winners = raw.get("max_winning_suppliers")
    if max_winners is not None:
        if not isinstance(max_winners, int) or max_winners <= 0:
            errors.append("max_winning_suppliers: 必须是正整数或留空")
            max_winners = None

    if errors:
        raise ValidationError(errors)

    return Batch(
        id=batch_id, name=name, materials=materials, demand=demand,
        suppliers=suppliers, capacities=capacities, substitutes=substitutes,
        single_quotes=single_quotes, combo_quotes=combo_quotes,
        max_winning_suppliers=max_winners)


def parse_config(raw: dict[str, Any], batch: Batch) -> SolveConfig:
    errors: list[str] = []
    mode = raw.get("mode", "strict")
    if mode not in ("strict", "penalty"):
        errors.append(f"mode: 非法值 {mode!r}")

    disabled = list(raw.get("disabled_quote_ids", []))
    for qid in disabled:
        if batch.quote(qid) is None:
            errors.append(f"disabled_quote_ids: 未知报价 {qid!r}")

    locks: list[Lock] = []
    locked_ids: set[str] = set()
    for i, lk in enumerate(raw.get("locks", [])):
        p = f"locks[{i}]"
        qid = lk.get("quote_id")
        qty = _as_int(lk, "quantity", errors, p, positive=True)
        q = batch.quote(qid) if qid else None
        if qid and q is None:
            errors.append(f"{p}.quote_id: 未知报价 {qid!r}")
        if qid in locked_ids:
            errors.append(f"{p}: 报价 {qid} 被重复锁定")
        locked_ids.add(qid)
        if q is not None and qty is not None:
            if isinstance(q, SingleQuote):
                if qty > 0 and qty < q.moq:
                    errors.append(f"{p}: 锁定数量 {qty} 低于起订量 {q.moq}")
                if qty > q.max_qty:
                    errors.append(f"{p}: 锁定数量 {qty} 超过可供量 {q.max_qty}")
            else:
                d = q.divisor
                if qty < q.min_multiplier or qty > d:
                    errors.append(f"{p}: 组合拆份数须在 "
                                  f"[{q.min_multiplier}, {d}] 内")
            locks.append(Lock(quote_id=qid, quantity=qty))

    penalties: dict[str, int] = {}
    for mid, v in (raw.get("penalties") or {}).items():
        if mid not in batch.material_ids:
            errors.append(f"penalties: 未知物料 {mid!r}")
            continue
        if not isinstance(v, (int, float)) or v < 0:
            errors.append(f"penalties[{mid}]: 必须是非负数字")
            continue
        penalties[mid] = round(v * MONEY_SCALE)

    default_penalty = _money(raw, "default_penalty", errors, "config") \
        if "default_penalty" in raw else 0
    if mode == "penalty" and default_penalty == 0 and not penalties:
        errors.append("penalty 模式下必须提供 default_penalty 或按物料罚金")

    mw = raw.get("max_winning_suppliers", batch.max_winning_suppliers)
    if mw is not None and (not isinstance(mw, int) or mw <= 0):
        errors.append("max_winning_suppliers: 必须是正整数或留空")
        mw = None

    tl = raw.get("time_limit_seconds", 30.0)
    if not isinstance(tl, (int, float)) or tl <= 0:
        errors.append("time_limit_seconds: 必须是正数")
        tl = 30.0
    workers = raw.get("num_workers", 8)
    if not isinstance(workers, int) or workers < 1:
        errors.append("num_workers: 必须是 >=1 的整数")
        workers = 8

    if errors:
        raise ValidationError(errors)
    return SolveConfig(
        mode=mode, disabled_quote_ids=disabled, locks=locks,
        penalties_scaled=penalties, default_penalty_scaled=default_penalty or 0,
        max_winning_suppliers=mw, time_limit_seconds=float(tl),
        num_workers=workers, random_seed=int(raw.get("random_seed", 20261124)))


# ------------------------------------------------------- 覆盖关系整数化


@dataclass
class CoverageCoeff:
    """某报价变量对某物料需求约束的整数系数（已去分母）。"""
    material_id: str
    numerator: int       # 乘在报价交付量上
    denominator_lcm: int  # 该需求约束的统一分母


def build_coverage_map(batch: Batch):
    """返回 need_material -> list[(source_material, num, den)]，含恒等关系。"""
    cover: dict[str, list[tuple[str, int, int]]] = {
        m.id: [(m.id, 1, 1)] for m in batch.materials
    }
    for s in batch.substitutes:
        cover[s.need_material_id].append(
            (s.source_material_id, s.num, s.den))
    return cover


def lcm(a: int, b: int) -> int:
    return a // math.gcd(a, b) * b


def demand_constraint_coeffs(batch: Batch):
    """计算每个需求约束的 LCM 分母，以及 (源物料 -> 整数系数) 表。

    约束形态（去分母后）:
        sum_source coeff[s] * supplied_qty[s] + LCM * shortage >= LCM * demand
    """
    cover = build_coverage_map(batch)
    result: dict[str, tuple[int, dict[str, int]]] = {}
    for need, sources in cover.items():
        denom = 1
        for _, _, den in sources:
            denom = lcm(denom, den)
        coeffs = {}
        for source, num, den in sources:
            coeffs[source] = num * (denom // den)
        result[need] = (denom, coeffs)
    return result
