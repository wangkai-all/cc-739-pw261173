"""标准库 HTTP 服务：REST API + 静态前端 + CSV 导出。"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import ValidationError, MONEY_SCALE, parse_config
from .solver import solve, why_not_combo, SolveInputError
from .storage import Storage, StorageError

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")

_MIME = {".html": "text/html; charset=utf-8",
         ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    storage: Storage = None  # 由 main 注入

    # -------------------------------------------------- 基础工具
    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, errors=None):
        self._json({"error": message, "errors": errors or []}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _HttpError(400, f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象")
        return data

    # -------------------------------------------------- 路由
    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/batches":
                return self._json(self.storage.list_batches())
            if path == "/api/sample":
                from .sample import build_sample
                return self._json(build_sample())
            m = re.fullmatch(r"/api/batches/([^/]+)", path)
            if m:
                return self._json(self.storage.load_batch_raw(m.group(1)))
            m = re.fullmatch(r"/api/batches/([^/]+)/scenarios", path)
            if m:
                return self._json(
                    self.storage.list_scenarios(m.group(1)))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)", path)
            if m:
                return self._json(self.storage.load_scenario(*m.groups()))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)/export\.csv", path)
            if m:
                return self._export_csv(*m.groups())
            return self._static(path)
        except (StorageError, ValidationError, SolveInputError) as exc:
            return self._error(400, str(exc),
                               getattr(exc, "errors", None))
        except _HttpError as exc:
            return self._error(exc.status, exc.message)

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            body = self._read_json()
            if path == "/api/batches":
                batch = self.storage.save_batch(body)
                return self._json({"id": batch.id}, 201)
            m = re.fullmatch(r"/api/batches/([^/]+)/withdraw", path)
            if m:
                return self._json(
                    self.storage.withdraw_quote(m.group(1),
                                                body.get("quote_id", "")))
            m = re.fullmatch(r"/api/batches/([^/]+)/scenarios", path)
            if m:
                res = self.storage.create_scenario(
                    m.group(1), str(body.get("name", "")),
                    body.get("config", {}))
                return self._json(res, 201)
            m = re.fullmatch(r"/api/batches/([^/]+)/solve", path)
            if m:
                batch = self.storage.load_batch(m.group(1))
                cfg = parse_config(body.get("config", {}), batch)
                return self._json(solve(batch, cfg))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/why-not", path)
            if m:
                batch = self.storage.load_batch(m.group(1))
                cfg = parse_config(body.get("config", {}), batch)
                return self._json(why_not_combo(
                    batch, cfg, body.get("quote_id", "")))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)/solve", path)
            if m:
                return self._solve_and_save(m.group(1), m.group(2), body)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)/clone", path)
            if m:
                return self._json(self.storage.clone_scenario(
                    m.group(1), m.group(2),
                    str(body.get("name", ""))), 201)
            return self._error(404, f"未知接口 {path}")
        except ValidationError as exc:
            return self._error(400, "输入校验失败", exc.errors)
        except (StorageError, SolveInputError) as exc:
            return self._error(400, str(exc), getattr(exc, "errors", None))
        except _HttpError as exc:
            return self._error(exc.status, exc.message)

    def do_PUT(self):
        try:
            path = self.path.split("?", 1)[0]
            body = self._read_json()
            m = re.fullmatch(r"/api/batches/([^/]+)", path)
            if m:
                self.storage.update_batch_input(m.group(1), body)
                return self._json({"id": m.group(1)})
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)/rename", path)
            if m:
                self.storage.rename_scenario(
                    m.group(1), m.group(2), str(body.get("name", "")))
                return self._json({"ok": True})
            return self._error(404, f"未知接口 {path}")
        except ValidationError as exc:
            return self._error(400, "输入校验失败", exc.errors)
        except StorageError as exc:
            return self._error(400, str(exc))

    def do_DELETE(self):
        try:
            path = self.path.split("?", 1)[0]
            m = re.fullmatch(
                r"/api/batches/([^/]+)/scenarios/([^/]+)", path)
            if m:
                self.storage.delete_scenario(*m.groups())
                return self._json({"ok": True})
            return self._error(404, f"未知接口 {path}")
        except StorageError as exc:
            return self._error(400, str(exc))

    # -------------------------------------------------- 业务
    def _solve_and_save(self, batch_id: str, scenario_id: str, body: dict):
        doc = self.storage.load_scenario(batch_id, scenario_id)
        if doc.get("stale"):
            raise _HttpError(409,
                             f"情景已失效：{doc.get('stale_reason')}；"
                             "请解除失效锁定后克隆为新情景再求解")
        batch = self.storage.load_batch(batch_id)
        config_raw = body.get("config", doc["config"])
        cfg = parse_config(config_raw, batch)
        solution = solve(batch, cfg)
        # 可行但未证最优的解也允许保存，但状态字段原样保留，界面不得标最优
        saved = self.storage.save_solution(
            batch_id, scenario_id, config_raw, solution)
        return self._json({"scenario": saved, "solution": solution})

    def _export_csv(self, batch_id: str, scenario_id: str):
        doc = self.storage.load_scenario(batch_id, scenario_id)
        batch = self.storage.load_batch(batch_id)
        sol = doc.get("solution")
        if not sol or not sol.get("result"):
            raise _HttpError(400, "该情景尚无求解结果，无法导出")
        sup_name = {s.id: s.name for s in batch.suppliers}
        mat_name = {m.id: m.name for m in batch.materials}

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["# 采购明细", f"批次={batch.name}",
                    f"情景={doc['name']}", f"状态={sol['status']}",
                    f"目标金额(元)={sol.get('objective_yuan')}",
                    f"下界(元)={sol.get('best_bound_yuan')}"])
        w.writerow(["报价ID", "类型", "供应商", "是否接受", "拆份数/交付量",
                    "物料", "交付数量", "单价/整包价(元)", "本项金额(元)",
                    "锁定", "报价状态", "备注"])
        for a in sol["result"]["allocations"]:
            sname = sup_name.get(a["supplier_id"], a["supplier_id"])
            price = (a.get("unit_price_yuan")
                     if a["kind"] == "single"
                     else a.get("package_price_yuan"))
            if a["kind"] == "combo":
                mult = f"{a.get('multiplier', 0)}/{a.get('divisor', 1)} 份"
            else:
                mult = f"{a.get('quantity', 0)}"
            deliveries = a.get("deliveries") or [
                {"material_id": a.get("material_id", ""), "quantity": 0}]
            for i, dl in enumerate(deliveries):
                w.writerow([
                    a["quote_id"] if i == 0 else "",
                    a["kind"] if i == 0 else "",
                    sname if i == 0 else "",
                    ("是" if a["accepted"] else "否") if i == 0 else "",
                    mult if i == 0 else "",
                    f"{dl['material_id']} {mat_name.get(dl['material_id'], '')}",
                    dl["quantity"],
                    price if i == 0 else "",
                    a.get("cost_yuan", 0) if i == 0 else "",
                    ("是" if a.get("locked") else "") if i == 0 else "",
                    a.get("status", "") if i == 0 else "",
                    (a.get("reason") or "") if i == 0 else "",
                ])
        w.writerow([])
        w.writerow(["# 需求覆盖核对"])
        w.writerow(["物料", "需求量", "等价覆盖量(精确分数)", "缺口", "来源明细"])
        for c in sol["result"]["coverage"]:
            sources = "; ".join(
                f"{x['material_id']}:{x['quantity']}" for x in c["sources"])
            w.writerow([
                f"{c['material_id']} {mat_name.get(c['material_id'], '')}",
                c["demand"], c["equivalent_covered_exact"], c["shortage"],
                sources])
        w.writerow([])
        w.writerow(["# 中标供应商", ", ".join(sol["result"]["winners"])])
        if sol.get("shadow"):
            w.writerow([])
            w.writerow(["# 约束影子分析（离散边际，仅最优解有效）"])
            w.writerow(["物料", "放松1单位节省(元)", "收紧1单位增加(元)",
                        "收紧是否可行", "备注"])
            for r in sol["shadow"]["demand_marginals"]:
                w.writerow([r["material_id"],
                            r["relax_one_unit_saves_yuan"],
                            r["tighten_one_unit_costs_yuan"],
                            r["tighten_feasible"], r["note"]])

        data = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=procurement_{batch_id}_{scenario_id}.csv")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------- 静态
    def _static(self, path: str):
        if path == "/":
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self._error(404, f"无此文件 {path}")
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def make_server(host: str, port: int, data_dir: str) -> ThreadingHTTPServer:
    Handler.storage = Storage(data_dir)
    return ThreadingHTTPServer((host, port), Handler)
