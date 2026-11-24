"""pytest 夹具。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.model import parse_batch, parse_config  # noqa: E402
from app.sample import demo_batch, conflict_batch  # noqa: E402


def tiny_batch() -> dict:
    """专门给穷举交叉核对用的小域实例（枚举空间约几万）。"""
    return {
        "id": "tiny",
        "name": "穷举核对小实例",
        "materials": [
            {"id": "A", "name": "A"},
            {"id": "B", "name": "B"},
        ],
        "suppliers": [
            {"id": "S1", "name": "一"},
            {"id": "S2", "name": "二"},
        ],
        "demand": [
            {"material_id": "A", "quantity": 5},
            {"material_id": "B", "quantity": 3},
        ],
        "substitutes": [
            {"need_material_id": "B", "source_material_id": "A",
             "num": 1, "den": 2}
        ],
        "capacities": [
            {"supplier_id": "S1", "material_id": None, "quantity": 8},
        ],
        "max_winning_suppliers": 2,
        "single_quotes": [
            {"id": "a1", "supplier_id": "S1", "material_id": "A",
             "unit_price": 10, "moq": 0, "max_qty": 6},
            {"id": "a2", "supplier_id": "S2", "material_id": "A",
             "unit_price": 8, "moq": 2, "max_qty": 5},
            {"id": "b1", "supplier_id": "S1", "material_id": "B",
             "unit_price": 12, "moq": 1, "max_qty": 3},
        ],
        "combo_quotes": [
            {"id": "k1", "supplier_id": "S2", "price": 60,
             "lines": [{"material_id": "A", "quantity": 3},
                       {"material_id": "B", "quantity": 2}]},
            {"id": "k2", "supplier_id": "S1", "price": 50,
             "lines": [{"material_id": "A", "quantity": 4},
                       {"material_id": "B", "quantity": 2}],
             "split_rule": {"divisor": 2, "min_multiplier": 2}},
        ],
    }


def make_tiny():
    return parse_batch(tiny_batch())


def make_demo():
    return parse_batch(demo_batch())


def make_conflict():
    return parse_batch(conflict_batch())


def cfg(mode="strict", **kw):
    base = {"mode": mode, "time_limit_seconds": 10, "num_workers": 4}
    base.update(kw)

    def _build(batch):
        return parse_config(base, batch)

    return _build
