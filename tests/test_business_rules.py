"""业务口径、超时诚实性、撤回失效、锁定保护、影子分析测试。"""
import time

from app.model import parse_batch, parse_config, ValidationError, MONEY_SCALE
from app.solver import solve, why_not_combo, SolveInputError
from app.storage import Storage, StorageError
from conftest import make_demo, make_conflict, make_tiny, cfg, tiny_batch


# -------------------------------------------------- 严格 vs 罚金
def test_strict_infeasible_reports_minimum_shortage():
    b = make_conflict()
    r = solve(b, cfg()(b))
    assert r["status"] == "infeasible"
    short = {x["material_id"]: x["quantity"]
             for x in r["infeasibility"]["minimum_shortage"]}
    assert short == {"X": 30, "Y": 30}
    # 不可行时不得返回任何分配
    assert r["result"] is None


def test_penalty_mode_buys_what_it_can_and_pays_fine():
    b = make_conflict()
    r = solve(b, cfg(mode="penalty", default_penalty=100)(b))
    assert r["status"] == "optimal"
    res = r["result"]
    # 采购成本：X 买满 70（40*5+30*6=380），Y 买满 20（20*7=140）= 520
    assert res["purchase_cost_scaled"] == 520 * MONEY_SCALE
    # 罚金：60 单位 * 100 = 6000
    assert res["penalty_cost_scaled"] == 6000 * MONEY_SCALE
    assert r["objective_yuan"] == 6520.0
    assert {x["material_id"]: x["quantity"]
            for x in res["shortages"]} == {"X": 30, "Y": 30}


def test_penalty_requires_penalty_input():
    b = make_conflict()
    try:
        parse_config({"mode": "penalty"}, b)
        assert False
    except ValidationError as e:
        assert any("罚金" in x for x in e.errors)


# -------------------------------------------------- 超时诚实性
def test_feasible_solution_never_labeled_optimal():
    """用极小时间限制逼出 FEASIBLE/UNKNOWN，检查状态与界的诚实标注。"""
    b = make_demo()
    c = parse_config({"mode": "strict", "time_limit_seconds": 0.0005,
                      "num_workers": 1}, b)
    r = solve(b, c)
    assert r["status"] in ("feasible_only", "no_solution", "optimal")
    if r["status"] == "feasible_only":
        # 关键红线：可行解不得标最优，且必须带下界与差距
        assert any("不得标记为最优" in w for w in r["warnings"])
        assert r["best_bound_yuan"] is not None
        assert r["gap_pct"] is not None and r["gap_pct"] >= 0
        assert r["best_bound_yuan"] <= r["objective_yuan"] + 1e-9
    elif r["status"] == "no_solution":
        assert r["result"] is None
        assert any("不能给出分配" in w for w in r["warnings"])


def test_shadow_analysis_absent_when_not_proven_optimal():
    b = make_demo()
    c = parse_config({"mode": "strict", "time_limit_seconds": 0.0005,
                      "num_workers": 1}, b)
    r = solve(b, c)
    if r["status"] != "optimal":
        assert "shadow" not in r


def test_shadow_marginals_on_optimal_solution():
    b = make_demo()
    r = solve(b, cfg(time_limit_seconds=10)(b))
    assert r["status"] == "optimal"
    shadow = r["shadow"]
    for row in shadow["demand_marginals"]:
        # 边际值必须成对自洽：放松省下的 <= 收紧增加的（整数凸性）
        if row["relax_one_unit_saves_yuan"] is not None \
                and row["tighten_one_unit_costs_yuan"] is not None:
            assert row["relax_one_unit_saves_yuan"] \
                <= row["tighten_one_unit_costs_yuan"] + 1e-9
    assert shadow["supplier_limit"]["limit"] == 2
    assert shadow["supplier_limit"]["relax_one_more_supplier_saves_yuan"] >= 0


# -------------------------------------------------- why-not
def test_why_not_combo_explains_rejection():
    b = make_demo()
    c = cfg()(b)
    base = solve(b, c)
    wn = why_not_combo(b, c, "K3")
    assert wn["blocked"] is False
    assert wn["extra_cost_yuan"] > 0
    assert "K3" in wn["added_quotes"]
    # 强制方案本身必须仍然可行且覆盖完整
    for cov in wn["forced"]["coverage"]:
        assert cov["shortage"] == 0
    assert base["objective_yuan"] + wn["extra_cost_yuan"] \
        == wn["forced_objective_yuan"]


def test_why_not_withdrawn_combo():
    b = make_demo()
    for q in b.combo_quotes:
        if q.id == "K1":
            q.status = "withdrawn"
    wn = why_not_combo(b, cfg()(b), "K1")
    assert wn["blocked"] is True
    assert any("撤回" in x for x in wn["reasons"])


# -------------------------------------------------- 撤回与失效
def _fresh_store(tmp_path):
    return Storage(str(tmp_path / "data"))


def test_withdraw_invalidates_scenarios_and_blocks_locked_recompute(tmp_path):
    st = _fresh_store(tmp_path)
    raw = tiny_batch()
    st.save_batch(raw)
    # 建情景并求解保存
    st.create_scenario("tiny", "情景1", {"mode": "strict",
                                         "time_limit_seconds": 10})
    sid = st.list_scenarios("tiny")[0]["id"]
    b = st.load_batch("tiny")
    from app.solver import solve as _solve
    sol = _solve(b, parse_config({"mode": "strict",
                                  "time_limit_seconds": 10}, b))
    st.save_solution("tiny", sid, {"mode": "strict",
                                   "time_limit_seconds": 10}, sol)

    # 先建一个锁定 k1 的情景
    st.create_scenario("tiny", "锁定情景",
                       {"mode": "strict", "locks": [
                           {"quote_id": "k1", "quantity": 1}],
                        "time_limit_seconds": 10})
    lock_sid = [s["id"] for s in st.list_scenarios("tiny")
                if s["name"] == "锁定情景"][0]

    # 撤回 k1
    info = st.withdraw_quote("tiny", "k1")
    assert lock_sid in info["affected_scenarios"]

    # 已保存方案被标 stale，但仍可查看
    doc = st.load_scenario("tiny", sid)
    assert doc["stale"] is True and "撤回" in doc["stale_reason"]
    assert doc["solution"] is not None

    # stale 情景拒绝覆盖保存（不能偷偷换方案）
    try:
        st.save_solution("tiny", sid, doc["config"], sol)
        assert False
    except StorageError as e:
        assert "失效" in str(e)

    # 锁定被撤回报价：求解直接报错，而不是静默换供应商
    b2 = st.load_batch("tiny")
    try:
        solve(b2, parse_config(
            {"mode": "strict", "locks": [
                {"quote_id": "k1", "quantity": 1}],
             "time_limit_seconds": 10}, b2))
        assert False
    except SolveInputError as e:
        assert "撤回" in str(e)


def test_locked_share_is_preserved_on_recompute(tmp_path):
    """锁定单项报价数量后，重算结果中该分配原封不动。"""
    st = _fresh_store(tmp_path)
    st.save_batch(tiny_batch())
    b = st.load_batch("tiny")
    c_raw = {"mode": "strict",
             "locks": [{"quote_id": "a1", "quantity": 4}],
             "time_limit_seconds": 10}
    r1 = solve(b, parse_config(c_raw, b))
    a1 = next(a for a in r1["result"]["allocations"] if a["quote_id"] == "a1")
    assert a1["quantity"] == 4 and a1["locked"]

    # 停用别的报价后重算，锁定项数量不变、仍然中标
    c_raw2 = dict(c_raw, disabled_quote_ids=["k1"])
    r2 = solve(b, parse_config(c_raw2, b))
    a1b = next(a for a in r2["result"]["allocations"] if a["quote_id"] == "a1")
    assert a1b["quantity"] == 4 and a1b["locked"]


def test_input_change_marks_scenarios_stale(tmp_path):
    st = _fresh_store(tmp_path)
    st.save_batch(tiny_batch())
    st.create_scenario("tiny", "s1", {"mode": "strict",
                                      "time_limit_seconds": 10})
    raw = tiny_batch()
    raw["demand"][0]["quantity"] = 6
    st.update_batch_input("tiny", raw)
    s = st.list_scenarios("tiny")[0]
    assert s["stale"] is True


# -------------------------------------------------- 输入校验
def test_validation_reports_all_conflicts():
    raw = tiny_batch()
    raw["single_quotes"][0]["max_qty"] = 3
    raw["single_quotes"][0]["moq"] = 5  # moq > max
    raw["combo_quotes"][0]["lines"][0]["material_id"] = "NOPE"
    raw["substitutes"][0]["num"] = 0
    try:
        parse_batch(raw)
        assert False
    except ValidationError as e:
        joined = " | ".join(e.errors)
        assert "起订量" in joined
        assert "未知物料" in joined
        assert "正数" in joined
        assert len(e.errors) >= 3


def test_duplicate_quote_and_material_ids_rejected():
    raw = tiny_batch()
    raw["single_quotes"][1]["id"] = raw["single_quotes"][0]["id"]
    try:
        parse_batch(raw)
        assert False
    except ValidationError as e:
        assert any("重复" in x for x in e.errors)


def test_money_scale_rejects_more_than_two_decimals():
    raw = tiny_batch()
    raw["single_quotes"][0]["unit_price"] = 10.123
    try:
        parse_batch(raw)
        assert False
    except ValidationError as e:
        assert any("小数位" in x for x in e.errors)


def test_lock_conflicts_with_disabled():
    b = make_tiny()
    c = parse_config(
        {"mode": "strict",
         "disabled_quote_ids": ["k1"],
         "locks": [{"quote_id": "k1", "quantity": 1}],
         "time_limit_seconds": 10}, b)
    try:
        solve(b, c)
        assert False
    except SolveInputError as e:
        assert "冲突" in str(e)


# -------------------------------------------------- 金额与覆盖核对
def test_objective_equals_line_costs_plus_penalty():
    b = make_conflict()
    r = solve(b, cfg(mode="penalty", default_penalty=100)(b))
    res = r["result"]
    line_sum = sum(a["cost_yuan"] for a in res["allocations"])
    assert round(line_sum, 2) == round(
        res["purchase_cost_scaled"] / MONEY_SCALE, 2)
    assert r["objective_scaled"] == res["purchase_cost_scaled"] \
        + res["penalty_cost_scaled"]


def test_substitute_conversion_is_exact():
    """1 单位 B 需求由 1/2 单位 A 覆盖：给 2 单位 A 应恰好覆盖 1 单位 B。"""
    raw = {
        "id": "sub", "name": "替代",
        "materials": [{"id": "A"}, {"id": "B"}],
        "suppliers": [{"id": "S1", "name": "s"}],
        "demand": [{"material_id": "A", "quantity": 0},
                   {"material_id": "B", "quantity": 1}],
        "substitutes": [{"need_material_id": "B",
                         "source_material_id": "A", "num": 1, "den": 2}],
        "capacities": [],
        "single_quotes": [
            {"id": "a", "supplier_id": "S1", "material_id": "A",
             "unit_price": 1, "moq": 0, "max_qty": 10}],
        "combo_quotes": [],
    }
    b = parse_batch(raw)
    r = solve(b, cfg()(b))
    assert r["status"] == "optimal"
    cov = {c["material_id"]: c for c in r["result"]["coverage"]}
    assert cov["B"]["equivalent_covered_exact"] == "1"
    assert cov["B"]["shortage"] == 0
    # 只买 2 件 A（而非更多）
    alloc = next(a for a in r["result"]["allocations"] if a["quote_id"] == "a")
    assert alloc["quantity"] == 2
