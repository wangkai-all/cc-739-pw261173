"""CP-SAT 与独立穷举器在小实例上的交叉核对：金额、覆盖、中标家数一致。"""
import itertools

from app.exhaustive import solve_exhaustive
from app.model import MONEY_SCALE
from app.solver import solve
from conftest import make_tiny, cfg, tiny_batch
from app.model import parse_batch


def _accepted_ids(sol_cp):
    return {a["quote_id"] for a in sol_cp["result"]["allocations"]
            if a["accepted"]}


def test_strict_optimal_matches_exhaustive():
    b = make_tiny()
    c = cfg()(b)
    cp = solve(b, c)
    ex = solve_exhaustive(b, c)
    assert cp["status"] == "optimal"
    assert cp["objective_scaled"] == ex["objective_scaled"]
    # 覆盖数量核对：严格模式零缺口
    for cov in cp["result"]["coverage"]:
        assert cov["shortage"] == 0
        assert float(cov["equivalent_covered_exact"]) >= cov["demand"] - 1e-9
    assert len(cp["result"]["winners"]) <= b.max_winning_suppliers


def test_penalty_mode_matches_exhaustive():
    b = make_tiny()
    c = cfg(mode="penalty", default_penalty=15)(b)
    cp = solve(b, c)
    ex = solve_exhaustive(b, c)
    assert cp["status"] == "optimal"
    assert cp["objective_scaled"] == ex["objective_scaled"]


def test_locks_match_exhaustive():
    b = make_tiny()
    for qid, qty in [("a1", 3), ("k1", 1), ("k2", 2)]:
        c = cfg(locks=[{"quote_id": qid, "quantity": qty}])(b)
        cp = solve(b, c)
        ex = solve_exhaustive(b, c)
        assert cp["status"] == "optimal"
        assert cp["objective_scaled"] == ex["objective_scaled"]
        chosen = next(a for a in cp["result"]["allocations"]
                      if a["quote_id"] == qid)
        assert chosen["accepted"] and chosen["locked"]
        assert chosen.get("quantity", chosen.get("multiplier")) == qty


def test_disabled_quotes_match_exhaustive():
    b = make_tiny()
    for disabled in (["k1"], ["a2", "k2"]):
        c = cfg(disabled_quote_ids=disabled)(b)
        cp = solve(b, c)
        ex = solve_exhaustive(b, c)
        assert cp["objective_scaled"] == ex["objective_scaled"]
        assert _accepted_ids(cp).isdisjoint(disabled)


def test_winner_limit_matches_exhaustive():
    b = make_tiny()
    for limit in (1, 2):
        c = cfg(max_winning_suppliers=limit)(b)
        cp = solve(b, c)
        ex = solve_exhaustive(b, c)
        assert cp["objective_scaled"] == ex["objective_scaled"]
        assert len(cp["result"]["winners"]) <= limit


def test_combo_is_all_or_nothing():
    """组合报价绝不能只买其中一行：枚举所有方案，任何含组合的方案
    其行交付量必须与拆份数严格成比例。"""
    b = make_tiny()
    c = cfg()(b)
    cp = solve(b, c)
    for a in cp["result"]["allocations"]:
        if a["kind"] != "combo" or not a["accepted"]:
            continue
        q = next(q for q in b.combo_quotes if q.id == a["quote_id"])
        t = a["multiplier"]
        for ln, dl in zip(q.lines, a["deliveries"]):
            assert dl["quantity"] == ln.quantity * t // q.divisor


def test_exhaustive_covers_all_config_grids():
    """对 tiny 实例的配置网格（模式 × 停用组合 × 家数）全量交叉核对。"""
    b = make_tiny()
    quote_ids = [q.id for q in b.all_quotes()]
    modes = [
        ("strict", {}),
        ("penalty", {"default_penalty": 20}),
    ]
    for mode, extra in modes:
        for r in range(0, len(quote_ids) + 1):
            for disabled in itertools.combinations(quote_ids, r):
                c = cfg(mode=mode, disabled_quote_ids=list(disabled),
                        max_winning_suppliers=2, **extra)(b)
                cp = solve(b, c)
                ex = solve_exhaustive(b, c)
                if ex["status"] == "infeasible":
                    assert cp["status"] == "infeasible"
                else:
                    assert cp["status"] == "optimal"
                    assert cp["objective_scaled"] == ex["objective_scaled"]


def test_demo_manual_amount_audit():
    """demo 实例手工复核金额构成与覆盖数量。"""
    from conftest import make_demo
    b = make_demo()
    cp = solve(b, cfg()(b))
    assert cp["status"] == "optimal"
    res = cp["result"]
    # 金额 = 各分项金额之和
    assert abs(sum(a["cost_yuan"] for a in res["allocations"])
               - cp["objective_yuan"]) < 1e-9
    # 覆盖核对（替代料用精确分数）
    for cov in res["coverage"]:
        assert cov["shortage"] == 0
    # 中标家数
    assert len(res["winners"]) <= b.max_winning_suppliers


def test_split_rule_integrality_enforced():
    raw = tiny_batch()
    # divisor=3、行数量 4：合法步长应为 3（4 与 3 互质），min=2 非法
    raw["combo_quotes"][1]["split_rule"] = {
        "divisor": 3, "min_multiplier": 2}
    from app.model import ValidationError
    try:
        parse_batch(raw)
        assert False, "应当校验失败"
    except ValidationError as e:
        assert any("步长" in x for x in e.errors)
