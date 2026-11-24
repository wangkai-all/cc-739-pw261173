"""情景持久化（JSON 文件）。

目录结构：
  data/
    <batch_id>/
      input.json              批次输入快照（报价撤回会原地更新 status）
      scenarios/
        <scenario_id>.json    同一批需求下的独立情景

正确性约定：
- 报价撤回时，input.json 中该报价 status=withdrawn，并给该批次所有已解
  情景打 stale 标记，记录失效原因；旧分配仍可查看，但明确标注已失效，
  且重算会拒绝保留依赖被撤回报价的锁定。
- 情景保存求解时输入指纹；加载时与当前 input 指纹比对，不一致即标 stale，
  防止在偷偷变化过的输入上复用旧结论。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict
from typing import Any

from .model import Batch, SolveConfig, parse_batch, parse_config, ValidationError


class StorageError(Exception):
    pass


def _fingerprint(batch: Batch) -> str:
    payload = {
        "materials": [asdict(m) for m in batch.materials],
        "demand": [asdict(d) for d in batch.demand],
        "suppliers": [asdict(s) for s in batch.suppliers],
        "capacities": [asdict(c) for c in batch.capacities],
        "substitutes": [asdict(s) for s in batch.substitutes],
        "single_quotes": [asdict(q) for q in batch.single_quotes],
        "combo_quotes": [
            {**asdict(q), "split_rule": asdict(q.split_rule)
             if q.split_rule else None}
            for q in batch.combo_quotes
        ],
        "max_winning_suppliers": batch.max_winning_suppliers,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Storage:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.RLock()

    # -------------------------------------------------- 路径
    def _batch_dir(self, batch_id: str) -> str:
        d = os.path.join(self.root, batch_id)
        if os.sep in batch_id or "/" in batch_id or batch_id in ("", ".", ".."):
            raise StorageError(f"非法批次 id：{batch_id!r}")
        return d

    def _input_path(self, batch_id: str) -> str:
        return os.path.join(self._batch_dir(batch_id), "input.json")

    def _scenario_path(self, batch_id: str, scenario_id: str) -> str:
        if scenario_id in ("", ".", "..") or "/" in scenario_id:
            raise StorageError(f"非法情景 id：{scenario_id!r}")
        return os.path.join(self._batch_dir(batch_id), "scenarios",
                            f"{scenario_id}.json")

    # -------------------------------------------------- 批次
    def list_batches(self) -> list[dict]:
        out = []
        with self._lock:
            for name in sorted(os.listdir(self.root)):
                p = self._input_path(name)
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as f:
                        raw = json.load(f)
                    out.append({"id": name, "name": raw.get("name", name),
                                "material_count": len(raw.get("materials", [])),
                                "quote_count": len(raw.get("single_quotes", []))
                                              + len(raw.get("combo_quotes", []))})
        return out

    def save_batch(self, raw: dict) -> Batch:
        batch = parse_batch(raw)
        with self._lock:
            d = self._batch_dir(batch.id)
            if os.path.exists(d):
                raise StorageError(
                    f"批次 {batch.id} 已存在；修改输入请走“更新输入”接口，"
                    "它会使依赖旧输入的情景失效")
            os.makedirs(os.path.join(d, "scenarios"), exist_ok=True)
            with open(self._input_path(batch.id), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        return batch

    def load_batch(self, batch_id: str) -> Batch:
        with self._lock:
            p = self._input_path(batch_id)
            if not os.path.isfile(p):
                raise StorageError(f"批次 {batch_id} 不存在")
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
        return parse_batch(raw)

    def load_batch_raw(self, batch_id: str) -> dict:
        with self._lock:
            p = self._input_path(batch_id)
            if not os.path.isfile(p):
                raise StorageError(f"批次 {batch_id} 不存在")
            with open(p, encoding="utf-8") as f:
                return json.load(f)

    def update_batch_input(self, batch_id: str, raw: dict) -> Batch:
        """整体替换输入（保持批次 id），并使所有情景失效。"""
        batch_new = parse_batch(raw)
        if batch_new.id != batch_id:
            raise StorageError("替换输入的批次 id 必须与原批次一致")
        with self._lock:
            old_fp = _fingerprint(self.load_batch(batch_id))
            with open(self._input_path(batch_id), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            new_fp = _fingerprint(batch_new)
            if new_fp != old_fp:
                self._mark_all_stale(batch_id,
                                     f"批次输入已变更（指纹 {old_fp} → {new_fp}）")
        return batch_new

    # -------------------------------------------------- 报价撤回
    def withdraw_quote(self, batch_id: str, quote_id: str) -> dict:
        with self._lock:
            raw = self.load_batch_raw(batch_id)
            target = None
            for key in ("single_quotes", "combo_quotes"):
                for q in raw.get(key, []):
                    if q.get("id") == quote_id:
                        target = q
            if target is None:
                raise StorageError(f"报价 {quote_id} 不存在")
            if target.get("status") == "withdrawn":
                return {"already_withdrawn": True, "quote_id": quote_id}
            target["status"] = "withdrawn"
            with open(self._input_path(batch_id), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            self._mark_all_stale(
                batch_id,
                f"报价 {quote_id} 被撤回，依赖该报价的分配与锁定已失效")
            affected = self._scenarios_locking(batch_id, quote_id)
        return {"quote_id": quote_id, "affected_scenarios": affected}

    def _scenarios_locking(self, batch_id: str, quote_id: str) -> list[str]:
        ids = []
        sdir = os.path.join(self._batch_dir(batch_id), "scenarios")
        if not os.path.isdir(sdir):
            return ids
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(sdir, fn), encoding="utf-8") as f:
                sc = json.load(f)
            if any(lk.get("quote_id") == quote_id
                   for lk in sc.get("config", {}).get("locks", [])):
                ids.append(sc["id"])
        return ids

    def _mark_all_stale(self, batch_id: str, reason: str) -> None:
        sdir = os.path.join(self._batch_dir(batch_id), "scenarios")
        if not os.path.isdir(sdir):
            return
        for fn in os.listdir(sdir):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(sdir, fn)
            with open(p, encoding="utf-8") as f:
                sc = json.load(f)
            if sc.get("solution") is not None:
                sc["stale"] = True
                sc["stale_reason"] = reason
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(sc, f, ensure_ascii=False, indent=2)

    # -------------------------------------------------- 情景
    def list_scenarios(self, batch_id: str) -> list[dict]:
        sdir = os.path.join(self._batch_dir(batch_id), "scenarios")
        out = []
        with self._lock:
            if not os.path.isdir(sdir):
                return out
            batch = self.load_batch(batch_id)
            current_fp = _fingerprint(batch)
            for fn in sorted(os.listdir(sdir)):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(sdir, fn), encoding="utf-8") as f:
                    sc = json.load(f)
                sol = sc.get("solution")
                out.append({
                    "id": sc["id"],
                    "name": sc.get("name", sc["id"]),
                    "mode": sc.get("config", {}).get("mode"),
                    "status": sol.get("status") if sol else "unsolved",
                    "objective_yuan": sol.get("objective_yuan") if sol else None,
                    "stale": sc.get("stale", False)
                             or sc.get("input_fingerprint") != current_fp,
                    "stale_reason": sc.get("stale_reason", ""),
                    "created_at": sc.get("created_at"),
                })
        return out

    def create_scenario(self, batch_id: str, name: str,
                        config_raw: dict) -> dict:
        with self._lock:
            batch = self.load_batch(batch_id)
            config = parse_config(config_raw, batch)
            sid = uuid.uuid4().hex[:12]
            doc = {
                "id": sid,
                "name": name or sid,
                "batch_id": batch_id,
                "config": config_raw,
                "input_fingerprint": _fingerprint(batch),
                "stale": False,
                "stale_reason": "",
                "solution": None,
                "history": [],
                "created_at": _now(),
            }
            self._write_scenario(batch_id, doc)
            return {"id": sid, "scenario": doc}

    def load_scenario(self, batch_id: str, scenario_id: str) -> dict:
        with self._lock:
            p = self._scenario_path(batch_id, scenario_id)
            if not os.path.isfile(p):
                raise StorageError(f"情景 {scenario_id} 不存在")
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
            batch = self.load_batch(batch_id)
            fp = _fingerprint(batch)
            if doc.get("input_fingerprint") != fp and not doc.get("stale"):
                doc["stale"] = True
                doc["stale_reason"] = "输入指纹与当前批次输入不一致"
                self._write_scenario(batch_id, doc)
            doc["current_fingerprint"] = fp
            return doc

    def save_solution(self, batch_id: str, scenario_id: str,
                      config_raw: dict, solution: dict) -> dict:
        with self._lock:
            doc = self.load_scenario(batch_id, scenario_id)
            if doc.get("stale"):
                raise StorageError(
                    f"情景已失效（{doc.get('stale_reason')}）；"
                    "请先检查撤回的报价与锁定，再另存或重算为新情景")
            batch = self.load_batch(batch_id)
            parse_config(config_raw, batch)  # 重新校验
            if doc.get("solution") is not None:
                doc["history"].append({
                    "config": doc["config"],
                    "solution_summary": _summary(doc["solution"]),
                    "saved_at": _now(),
                })
            doc["config"] = config_raw
            doc["input_fingerprint"] = _fingerprint(batch)
            doc["solution"] = solution
            doc["solved_at"] = _now()
            self._write_scenario(batch_id, doc)
            return doc

    def rename_scenario(self, batch_id: str, scenario_id: str,
                        name: str) -> None:
        with self._lock:
            doc = self.load_scenario(batch_id, scenario_id)
            doc["name"] = name
            self._write_scenario(batch_id, doc)

    def delete_scenario(self, batch_id: str, scenario_id: str) -> None:
        with self._lock:
            p = self._scenario_path(batch_id, scenario_id)
            if os.path.isfile(p):
                os.remove(p)

    def clone_scenario(self, batch_id: str, scenario_id: str,
                       new_name: str) -> dict:
        with self._lock:
            doc = self.load_scenario(batch_id, scenario_id)
            sid = uuid.uuid4().hex[:12]
            clone = dict(doc)
            clone["id"] = sid
            clone["name"] = new_name
            clone["stale"] = False
            clone["stale_reason"] = ""
            clone["created_at"] = _now()
            clone["solution"] = None
            clone["history"] = []
            self._write_scenario(batch_id, clone)
            return {"id": sid}

    def _write_scenario(self, batch_id: str, doc: dict) -> None:
        p = self._scenario_path(batch_id, doc["id"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)


def _summary(solution: dict) -> dict:
    return {
        "status": solution.get("status"),
        "objective_yuan": solution.get("objective_yuan"),
        "best_bound_yuan": solution.get("best_bound_yuan"),
    }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
