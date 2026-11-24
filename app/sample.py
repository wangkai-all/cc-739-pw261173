"""内置示例实例。

demo：3 种物料、3 家供应商，含单项报价、组合报价、拆包、替代料、
产能与最多中标家数约束，规模小到可以用穷举器逐方案核对。
conflict：严格口径下不可行（总可供量 < 需求 + 起订量冲突），
用于核对不可行诊断与罚金口径的最小缺口。
"""
from __future__ import annotations


def build_sample() -> dict:
    return {"batches": [demo_batch(), conflict_batch()]}


def demo_batch() -> dict:
    return {
        "id": "demo",
        "name": "演示批次（可穷举核对）",
        "materials": [
            {"id": "A", "name": "材料A", "unit": "件"},
            {"id": "B", "name": "材料B", "unit": "件"},
            {"id": "C", "name": "材料C", "unit": "件"},
        ],
        "suppliers": [
            {"id": "S1", "name": "供应商一"},
            {"id": "S2", "name": "供应商二"},
            {"id": "S3", "name": "供应商三"},
        ],
        "demand": [
            {"material_id": "A", "quantity": 100},
            {"material_id": "B", "quantity": 60},
            {"material_id": "C", "quantity": 40},
        ],
        # 替代料：1 单位 B 的需求可由 1/2 单位 C 覆盖（即 2 件 C 顶 1 件 B）
        "substitutes": [
            {"need_material_id": "B", "source_material_id": "C",
             "num": 1, "den": 2}
        ],
        "capacities": [
            {"supplier_id": "S2", "material_id": None, "quantity": 80},
            {"supplier_id": "S3", "material_id": "A", "quantity": 120},
        ],
        "max_winning_suppliers": 2,
        "single_quotes": [
            {"id": "SA1", "supplier_id": "S1", "material_id": "A",
             "unit_price": 10.0, "moq": 0, "max_qty": 100},
            {"id": "SA2", "supplier_id": "S2", "material_id": "A",
             "unit_price": 8.0, "moq": 50, "max_qty": 60},
            {"id": "SB1", "supplier_id": "S1", "material_id": "B",
             "unit_price": 20.0, "moq": 10, "max_qty": 60},
            {"id": "SB2", "supplier_id": "S2", "material_id": "B",
             "unit_price": 18.0, "moq": 20, "max_qty": 30},
            {"id": "SC1", "supplier_id": "S3", "material_id": "C",
             "unit_price": 50.0, "moq": 0, "max_qty": 60},
        ],
        "combo_quotes": [
            # 整包：A50+B30 卖 850 元；对应逐项最低价 50*8+30*18=940 元，
            # 组合更便宜，但会与 S2 的总产能、中标家数约束相互挤压。
            {"id": "K1", "supplier_id": "S3", "price": 850.0,
             "lines": [{"material_id": "A", "quantity": 50},
                       {"material_id": "B", "quantity": 30}]},
            # 可拆包组合：A100+C40 整包 2600，可拆 4 份、最少 2 份、步长 2
            # （每份 A25+C10，650 元）。逐行挑便宜的人会误以为能只买 A 那部分。
            {"id": "K2", "supplier_id": "S3", "price": 2600.0,
             "lines": [{"material_id": "A", "quantity": 100},
                       {"material_id": "C", "quantity": 40}],
             "split_rule": {"divisor": 4, "min_multiplier": 2}},
            # 看似很便宜但强制整包超需求：A120+B80 只要 1800，
            # 接受就会造成大量过量，why-not 应解释为何不选它。
            {"id": "K3", "supplier_id": "S1", "price": 1800.0,
             "lines": [{"material_id": "A", "quantity": 120},
                       {"material_id": "B", "quantity": 80}]},
        ],
    }


def conflict_batch() -> dict:
    return {
        "id": "conflict",
        "name": "冲突输入（严格口径不可行）",
        "materials": [
            {"id": "X", "name": "物料X", "unit": "个"},
            {"id": "Y", "name": "物料Y", "unit": "个"},
        ],
        "suppliers": [
            {"id": "P1", "name": "供应商甲"},
            {"id": "P2", "name": "供应商乙"},
        ],
        "demand": [
            {"material_id": "X", "quantity": 100},
            {"material_id": "Y", "quantity": 50},
        ],
        "capacities": [
            {"supplier_id": "P1", "material_id": "X", "quantity": 40},
            {"supplier_id": "P2", "material_id": "Y", "quantity": 20},
        ],
        "single_quotes": [
            # X 总可供量 40+30=70 < 需求 100
            {"id": "QX1", "supplier_id": "P1", "material_id": "X",
             "unit_price": 5.0, "moq": 10, "max_qty": 40},
            {"id": "QX2", "supplier_id": "P2", "material_id": "X",
             "unit_price": 6.0, "moq": 0, "max_qty": 30},
            # Y 只有 20 可供 < 需求 50；另在测试中覆盖“起订量>可供量”的
            # 输入级冲突（解析器会直接拒绝并定位字段）。
            {"id": "QY1", "supplier_id": "P2", "material_id": "Y",
             "unit_price": 7.0, "moq": 0, "max_qty": 20},
        ],
        "combo_quotes": [],
    }
