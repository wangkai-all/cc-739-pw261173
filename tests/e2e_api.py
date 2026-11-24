"""端到端 API 流程核对（对运行中的服务发真实 HTTP 请求）。"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8080"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name, cond, detail=""):
    assert cond, f"FAIL {name} {detail}"
    print(f"PASS {name} {detail}")


# 1. 静态页
with urllib.request.urlopen(BASE + "/") as r:
    check("index.html", r.status == 200 and "组合报价" in r.read().decode())

# 2. 载入示例批次
st, sample = call("GET", "/api/sample")
demo_raw = sample["batches"][0]
conflict_raw = sample["batches"][1]
st, _ = call("POST", "/api/batches", demo_raw)
check("create demo", st == 201, str(st))
st, _ = call("POST", "/api/batches", conflict_raw)
check("create conflict", st == 201)
st, _ = call("POST", "/api/batches", demo_raw)
check("duplicate rejected", st == 400)

# 3. 严格求解 demo
st, sol = call("POST", "/api/batches/demo/solve",
               {"config": {"mode": "strict", "time_limit_seconds": 10}})
check("demo strict optimal", sol["status"] == "optimal",
      f"obj={sol['objective_yuan']} bound={sol['best_bound_yuan']}")
check("demo obj 3350", sol["objective_yuan"] == 3350.0)
check("zero gap", sol["gap_pct"] == 0.0)
check("shadow present", "shadow" in sol and len(
    sol["shadow"]["demand_marginals"]) == 3)
# 金额勾稽
line_sum = round(sum(a["cost_yuan"] for a in sol["result"]["allocations"]), 2)
check("amount audit", line_sum == sol["objective_yuan"], str(line_sum))
# 覆盖勾稽
for c in sol["result"]["coverage"]:
    check(f"coverage {c['material_id']}", c["shortage"] == 0,
          c["equivalent_covered_exact"])
check("winner limit", len(sol["result"]["winners"]) <= 2,
      str(sol["result"]["winners"]))

# 4. why-not K3
st, wn = call("POST", "/api/batches/demo/why-not",
              {"quote_id": "K3",
               "config": {"mode": "strict", "time_limit_seconds": 10}})
check("why-not K3", st == 200 and wn["extra_cost_yuan"] > 0,
      f"extra={wn['extra_cost_yuan']}")
check("why-not forced feasible",
      all(c["shortage"] == 0 for c in wn["forced"]["coverage"]))

# 5. 建情景并求解保存
st, created = call("POST", "/api/batches/demo/scenarios",
                   {"name": "基准情景",
                    "config": {"mode": "strict", "time_limit_seconds": 10}})
sid = created["id"]
check("create scenario", st == 201)
st, saved = call("POST", f"/api/batches/demo/scenarios/{sid}/solve",
                 {"config": {"mode": "strict", "time_limit_seconds": 10}})
check("solve+save", saved["solution"]["status"] == "optimal")

# 6. 停用一个报价 + 锁定一项，另存情景
st, created2 = call("POST", "/api/batches/demo/scenarios",
                    {"name": "锁定情景",
                     "config": {"mode": "strict",
                                "disabled_quote_ids": ["SA2"],
                                "locks": [{"quote_id": "K1", "quantity": 1}],
                                "time_limit_seconds": 10}})
sid2 = created2["id"]
st, saved2 = call("POST", f"/api/batches/demo/scenarios/{sid2}/solve",
                  {"config": {"mode": "strict",
                              "disabled_quote_ids": ["SA2"],
                              "locks": [{"quote_id": "K1", "quantity": 1}],
                              "time_limit_seconds": 10}})
check("locked K1 kept",
      any(a["quote_id"] == "K1" and a["locked"] and a["accepted"]
          for a in saved2["solution"]["result"]["allocations"]))
check("SA2 excluded",
      next(a for a in saved2["solution"]["result"]["allocations"]
           if a["quote_id"] == "SA2")["reason"] == "手工停用")

# 7. 撤回 K1 -> 情景失效，锁定重算被拒
st, info = call("POST", "/api/batches/demo/withdraw", {"quote_id": "K1"})
check("withdraw", st == 200 and sid2 in info["affected_scenarios"],
      str(info))
st, doc = call("GET", f"/api/batches/demo/scenarios/{sid2}")
check("scenario stale", doc["stale"] is True and "撤回" in doc["stale_reason"])
st, err = call("POST", f"/api/batches/demo/scenarios/{sid2}/solve",
               {"config": {"mode": "strict",
                           "locks": [{"quote_id": "K1", "quantity": 1}],
                           "time_limit_seconds": 10}})
check("stale save rejected 409", st == 409, str(st))
st, err2 = call("POST", "/api/batches/demo/solve",
                {"config": {"mode": "strict",
                            "locks": [{"quote_id": "K1", "quantity": 1}],
                            "time_limit_seconds": 10}})
check("locked withdrawn rejected", st == 400 and "撤回" in err2["error"])

# 8. 冲突批次：strict 不可行 + 诊断；penalty 给方案
st, inf = call("POST", "/api/batches/conflict/solve",
               {"config": {"mode": "strict", "time_limit_seconds": 10}})
check("conflict infeasible", inf["status"] == "infeasible")
sh = {x["material_id"]: x["quantity"]
      for x in inf["infeasibility"]["minimum_shortage"]}
check("minimum shortage", sh == {"X": 30, "Y": 30}, str(sh))
st, pen = call("POST", "/api/batches/conflict/solve",
               {"config": {"mode": "penalty", "default_penalty": 100,
                           "time_limit_seconds": 10}})
check("penalty solved", pen["status"] == "optimal"
      and pen["objective_yuan"] == 6520.0, str(pen["objective_yuan"]))

# 9. 罚金口径缺罚金参数应 400
st, err3 = call("POST", "/api/batches/conflict/solve",
                {"config": {"mode": "penalty", "time_limit_seconds": 10}})
check("penalty requires input", st == 400 and "罚金" in str(err3["errors"]))

# 10. CSV 导出
with urllib.request.urlopen(
        f"{BASE}/api/batches/demo/scenarios/{sid}/export.csv") as r:
    csv_text = r.read().decode("utf-8-sig")
check("csv header", "采购明细" in csv_text and "需求覆盖核对" in csv_text)
check("csv amount", "3350" in csv_text)

# 11. 情景列表与克隆
st, lst = call("GET", "/api/batches/demo/scenarios")
check("list scenarios", len(lst) == 2 and any(s["stale"] for s in lst),
      str([(s["name"], s["stale"]) for s in lst]))
st, cl = call("POST", f"/api/batches/demo/scenarios/{sid2}/clone",
              {"name": "克隆-去锁重算"})
check("clone", st == 201)

# 12. 校验错误定位
bad = json.loads(json.dumps(demo_raw))
bad["single_quotes"][0]["unit_price"] = 10.123
bad["combo_quotes"][0]["lines"][0]["material_id"] = "GHOST"
st, err4 = call("POST", "/api/batches", bad)
check("validation locates fields", st == 400 and len(err4["errors"]) >= 2,
      " | ".join(err4["errors"]))

print("\n全部端到端检查通过")
