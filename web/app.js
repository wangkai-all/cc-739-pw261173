"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.errors?.length
      ? data.errors.join("\n") : (data.error || `请求失败 ${res.status}`);
    throw new Error(msg);
  }
  return data;
};

const state = {
  batches: [],
  batchId: null,
  raw: null,
  scenarios: [],
  scenario: null,      // 当前打开的已保存情景 doc
  solution: null,      // 最近一次求解结果（可能未保存）
  config: { mode: "strict", disabled_quote_ids: [], locks: [],
            penalties: {}, default_penalty: 100,
            max_winning_suppliers: null, time_limit_seconds: 15 },
  pendingLock: null,
};

const esc = (s) => String(s ?? "").replace(
  /[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                        '"': "&quot;", "'": "&#39;" }[c]));
const yuan = (v) => (v === null || v === undefined ? "—"
  : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2,
                                        maximumFractionDigits: 2 }));
const toast = (msg, ms = 3500) => {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
};

// ------------------------------------------------------------ 初始化
async function init() {
  bindEvents();
  await refreshBatches();
}

function bindEvents() {
  $("#btn-sample").onclick = loadSample;
  $("#btn-import").onclick = () => $("#file-import").click();
  $("#file-import").onchange = importFile;
  $("#cfg-mode").onchange = (e) => {
    state.config.mode = e.target.value;
    $("#penalty-wrap").hidden = e.target.value !== "penalty";
  };
  $("#cfg-penalty").onchange = (e) =>
    state.config.default_penalty = Number(e.target.value) || 0;
  $("#cfg-winners").onchange = (e) =>
    state.config.max_winning_suppliers =
      e.target.value === "" ? null : Number(e.target.value);
  $("#cfg-time").onchange = (e) =>
    state.config.time_limit_seconds = Number(e.target.value) || 15;
  $("#btn-solve").onclick = () => runSolve(false);
  $("#btn-save-new").onclick = saveAsNewScenario;
  $("#btn-save-scenario").onclick = () => runSolve(true);
  $("#btn-clone").onclick = cloneScenario;
  $("#btn-export").onclick = exportCsv;
  $("#btn-view-input").onclick = openInputDialog;
  $("#btn-update-input").onclick = updateInput;
  $("#btn-confirm-lock").onclick = confirmLock;
}

// ------------------------------------------------------------ 批次
async function refreshBatches() {
  state.batches = await api("/api/batches");
  const ul = $("#batch-list");
  ul.innerHTML = state.batches.length ? "" :
    '<li class="muted">暂无批次</li>';
  for (const b of state.batches) {
    const li = document.createElement("li");
    li.className = b.id === state.batchId ? "active" : "";
    li.innerHTML = `<strong>${esc(b.name)}</strong>
      <div class="row2 muted">${b.id} · ${b.material_count} 物料 ·
      ${b.quote_count} 报价</div>`;
    li.onclick = () => openBatch(b.id);
    ul.appendChild(li);
  }
}

async function loadSample() {
  const sample = await api("/api/sample");
  for (const raw of sample.batches) {
    try {
      await api("/api/batches", { method: "POST", body: raw });
    } catch (e) {
      if (!String(e.message).includes("已存在")) throw e;
    }
  }
  await refreshBatches();
  toast("内置示例已载入（若已存在则跳过）");
  if (state.batches.length) await openBatch("demo");
}

async function importFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const raw = JSON.parse(await file.text());
    await api("/api/batches", { method: "POST", body: raw });
    await refreshBatches();
    toast(`批次 ${raw.id} 已导入`);
    await openBatch(raw.id);
  } catch (err) {
    alert("导入失败：\n" + err.message);
  } finally {
    e.target.value = "";
  }
}

async function openBatch(batchId) {
  state.batchId = batchId;
  state.raw = await api(`/api/batches/${batchId}`);
  state.scenario = null;
  state.solution = null;
  const mw = state.raw.max_winning_suppliers ?? null;
  state.config = {
    mode: "strict", disabled_quote_ids: [], locks: [], penalties: {},
    default_penalty: 100, max_winning_suppliers: mw,
    time_limit_seconds: 15,
  };
  $("#cfg-mode").value = "strict";
  $("#penalty-wrap").hidden = true;
  $("#cfg-winners").value = mw ?? "";
  $("#cfg-time").value = 15;
  $("#empty-hint").hidden = true;
  $("#workspace").hidden = false;
  renderHeader();
  renderQuotes();
  renderLocks();
  $("#result-card").hidden = true;
  $("#btn-export").disabled = true;
  $("#scenario-label").textContent = "未保存的工作配置";
  await refreshScenarios();
  await refreshBatches();
}

function renderHeader() {
  const r = state.raw;
  $("#batch-title").textContent = `${r.name}（${r.id}）`;
  $("#batch-meta").textContent =
    `${r.materials.length} 种物料 · ${r.suppliers.length} 家供应商 · ` +
    `${r.single_quotes.length} 条单项报价 · ${r.combo_quotes.length} 条组合报价` +
    (r.max_winning_suppliers ? ` · 最多中标 ${r.max_winning_suppliers} 家` : "");
}

// ------------------------------------------------------------ 报价表
function allQuotes() {
  const singles = state.raw.single_quotes.map((q) => ({ ...q, kind: "single" }));
  const combos = state.raw.combo_quotes.map((q) => ({ ...q, kind: "combo" }));
  return [...singles, ...combos];
}

function renderQuotes() {
  const tbody = $("#quote-table tbody");
  tbody.innerHTML = "";
  const sup = Object.fromEntries(state.raw.suppliers.map((s) => [s.id, s.name]));
  const disabled = new Set(state.config.disabled_quote_ids);
  const lockedIds = new Set(state.config.locks.map((l) => l.quote_id));
  for (const q of allQuotes()) {
    const tr = document.createElement("tr");
    const withdrawn = q.status === "withdrawn";
    const checked = !disabled.has(q.id) && !withdrawn;
    let content, constraint;
    if (q.kind === "single") {
      content = `${esc(q.material_id)} · 单价 ${yuan(q.unit_price)} 元`;
      constraint = `起订 ${q.moq} / 可供 ${q.max_qty}`;
    } else {
      const lines = q.lines.map((l) => `${esc(l.material_id)}×${l.quantity}`)
        .join(" + ");
      content = `${lines}<br>整包 ${yuan(q.price)} 元`;
      constraint = q.split_rule
        ? `拆包 ${q.split_rule.min_multiplier}–${q.split_rule.divisor} 份` +
          `（步长 ${q.split_rule.step}）`
        : "整包接受，不可拆";
    }
    const lockQty = state.config.locks.find((l) => l.quote_id === q.id)?.quantity;
    tr.innerHTML = `
      <td><input type="checkbox" ${checked ? "checked" : ""}
        ${withdrawn || lockedIds.has(q.id) ? "disabled" : ""}></td>
      <td>${esc(q.id)}</td>
      <td><span class="pill ${q.kind}">${q.kind === "combo" ? "组合" : "单项"}</span></td>
      <td>${esc(sup[q.supplier_id] || q.supplier_id)}</td>
      <td>${content}</td>
      <td>${constraint}</td>
      <td>${withdrawn
        ? '<span class="pill withdrawn">已撤回</span>'
        : '<span class="pill">有效</span>'}</td>
      <td class="ops"></td>`;
    tr.querySelector("input").onchange = (e) => {
      if (e.target.checked) disabled.delete(q.id);
      else disabled.add(q.id);
      state.config.disabled_quote_ids = [...disabled];
    };
    const ops = tr.querySelector(".ops");
    if (!withdrawn) {
      const bLock = document.createElement("button");
      bLock.className = "small ghost";
      bLock.textContent = lockedIds.has(q.id)
        ? `已锁定 ${lockQty}（解除）` : "锁定";
      bLock.onclick = () => toggleLock(q, lockedIds.has(q.id));
      ops.appendChild(bLock);
      if (q.kind === "combo") {
        const bWhy = document.createElement("button");
        bWhy.className = "small ghost";
        bWhy.textContent = "为什么没选它";
        bWhy.style.marginLeft = "6px";
        bWhy.onclick = () => showWhyNot(q.id);
        ops.appendChild(bWhy);
      }
      const bWd = document.createElement("button");
      bWd.className = "small danger";
      bWd.style.marginLeft = "6px";
      bWd.textContent = "撤回";
      bWd.onclick = () => withdraw(q.id);
      ops.appendChild(bWd);
    }
    tbody.appendChild(tr);
  }
}

// ------------------------------------------------------------ 锁定
function toggleLock(q, remove) {
  state.config.locks = state.config.locks.filter((l) => l.quote_id !== q.id);
  if (remove) {
    renderQuotes(); renderLocks();
    return;
  }
  let desc, min, max, step, val;
  if (q.kind === "single") {
    min = Math.max(1, q.moq); max = q.max_qty; step = 1;
    desc = `单项报价 ${q.id}：可锁定数量 ${q.moq}–${q.max_qty}` +
           (q.moq === 0 ? "（0 起订，最少锁 1）" : "");
    val = Math.max(1, q.moq);
  } else {
    const sr = q.split_rule;
    min = sr ? sr.min_multiplier : 1;
    max = sr ? sr.divisor : 1;
    step = sr ? sr.step : 1;
    desc = `组合报价 ${q.id}：拆份数范围 ${min}–${max}` +
           (step > 1 ? `，且为 ${step} 的倍数` : "");
    val = min;
  }
  state.pendingLock = { quote_id: q.id, min, max, step };
  $("#lock-desc").textContent = desc;
  const input = $("#lock-qty");
  input.min = min; input.max = max; input.step = step; input.value = val;
  $("#dlg-lock").showModal();
}

function confirmLock(e) {
  e.preventDefault();
  const pl = state.pendingLock;
  const v = Number($("#lock-qty").value);
  if (!Number.isInteger(v) || v < pl.min || v > pl.max ||
      (v - pl.min) % pl.step !== 0) {
    alert(`锁定值必须是 ${pl.min}–${pl.max} 内步长 ${pl.step} 的整数`);
    return;
  }
  state.config.locks = state.config.locks.filter(
    (l) => l.quote_id !== pl.quote_id);
  state.config.locks.push({ quote_id: pl.quote_id, quantity: v });
  // 锁定的报价强制启用
  state.config.disabled_quote_ids =
    state.config.disabled_quote_ids.filter((id) => id !== pl.quote_id);
  $("#dlg-lock").close();
  renderQuotes(); renderLocks();
}

function renderLocks() {
  const ul = $("#locks-list");
  ul.innerHTML = "";
  if (!state.config.locks.length) {
    ul.innerHTML = '<li class="muted">无锁定。锁定后重算不会替换该份额。</li>';
    return;
  }
  for (const lk of state.config.locks) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${esc(lk.quote_id)} 锁定值 = <strong>${lk.quantity}</strong></span>`;
    const b = document.createElement("button");
    b.className = "small ghost"; b.textContent = "解除";
    b.onclick = () => {
      state.config.locks = state.config.locks.filter(
        (l) => l.quote_id !== lk.quote_id);
      renderQuotes(); renderLocks();
    };
    li.appendChild(b);
    ul.appendChild(li);
  }
}

// ------------------------------------------------------------ 撤回
async function withdraw(quoteId) {
  if (!confirm(`确认撤回报价 ${quoteId}？\n撤回后该批次所有已保存情景将标记失效，` +
      "依赖该报价的锁定必须解除后才能重算。")) return;
  try {
    const info = await api(`/api/batches/${state.batchId}/withdraw`, {
      method: "POST", body: { quote_id: quoteId },
    });
    toast(`已撤回 ${quoteId}，影响情景：` +
      (info.affected_scenarios.length ? info.affected_scenarios.join(", ") : "无"));
    state.raw = await api(`/api/batches/${state.batchId}`);
    state.config.locks = state.config.locks.filter(
      (l) => l.quote_id !== quoteId);
    renderQuotes(); renderLocks();
    await refreshScenarios();
  } catch (err) {
    alert("撤回失败：" + err.message);
  }
}

// ------------------------------------------------------------ 求解
function configPayload() {
  const c = state.config;
  const payload = {
    mode: c.mode,
    disabled_quote_ids: c.disabled_quote_ids,
    locks: c.locks,
    max_winning_suppliers: c.max_winning_suppliers,
    time_limit_seconds: c.time_limit_seconds,
  };
  if (c.mode === "penalty") {
    payload.default_penalty = c.default_penalty;
    if (Object.keys(c.penalties).length) payload.penalties = c.penalties;
  }
  return payload;
}

async function runSolve(saveToScenario) {
  const body = { config: configPayload() };
  try {
    let solution;
    if (saveToScenario && state.scenario) {
      const data = await api(
        `/api/batches/${state.batchId}/scenarios/${state.scenario.id}/solve`,
        { method: "POST", body });
      solution = data.solution;
      state.scenario = data.scenario;
      await refreshScenarios();
      toast("已求解并保存到当前情景");
    } else {
      solution = await api(`/api/batches/${state.batchId}/solve`,
        { method: "POST", body });
    }
    state.solution = solution;
    renderResult(solution);
  } catch (err) {
    alert("求解失败：\n" + err.message);
  }
}

function renderResult(sol) {
  $("#result-card").hidden = false;
  const banner = $("#status-banner");
  const map = {
    optimal: ["ok", "已证明最优（OPTIMAL）"],
    feasible_only: ["warn", "仅找到可行解，未证明最优 —— 严禁按最优执行"],
    infeasible: ["err", "无可行方案（INFEASIBLE）"],
    no_solution: ["err", "时限内未找到任何可行解"],
  };
  const [cls, text] = map[sol.status] || ["info", sol.status];
  banner.className = `banner ${cls}`;
  banner.textContent = text +
    ` · 求解耗时 ${sol.solved_in_seconds}s / 时限 ${sol.time_limit_seconds}s`;

  $("#warnings").hidden = !sol.warnings?.length;
  if (sol.warnings) $("#warnings").innerHTML = sol.warnings.map(esc).join("<br>");

  const kpi = $("#kpi-row");
  kpi.innerHTML = "";
  const addKpi = (k, v) => {
    const d = document.createElement("div");
    d.className = "kpi";
    d.innerHTML = `<div class="v">${v}</div><div class="k">${k}</div>`;
    kpi.appendChild(d);
  };
  if (sol.result) {
    addKpi("总成本(元)", yuan(sol.objective_yuan));
    addKpi("采购金额(元)", yuan(sol.result.purchase_cost_scaled / 100));
    addKpi("缺口罚金(元)", yuan(sol.result.penalty_cost_scaled / 100));
    addKpi("中标供应商", sol.result.winners.length
      ? esc(sol.result.winners.join(", ")) : "无");
  }
  if (sol.best_bound_yuan !== null && sol.best_bound_yuan !== undefined)
    addKpi("已证下界(元)", yuan(sol.best_bound_yuan));
  if (sol.gap_pct !== null && sol.gap_pct !== undefined)
    addKpi("最优差距(%)", Number(sol.gap_pct).toFixed(3));

  renderAllocations(sol);
  renderCoverage(sol);
  renderShadow(sol);
  renderInfeasible(sol);
  $("#btn-export").disabled = !sol.result || !state.scenario;
  if (!state.scenario) {
    $("#btn-export").disabled = true;
    $("#btn-export").title = "先把结果保存为情景后才能导出";
  } else {
    $("#btn-export").title = "";
  }
}

function renderAllocations(sol) {
  const tbody = $("#alloc-table tbody");
  tbody.innerHTML = "";
  if (!sol.result) return;
  const sup = Object.fromEntries(state.raw.suppliers.map((s) => [s.id, s.name]));
  for (const a of sol.result.allocations) {
    const tr = document.createElement("tr");
    if (!a.accepted) tr.className = "excluded";
    else tr.className = "accepted";
    const qty = a.kind === "combo"
      ? `${a.multiplier || 0}/${a.divisor || 1} 份` : a.quantity;
    const del = (a.deliveries || [])
      .filter((d) => d.quantity > 0)
      .map((d) => `${esc(d.material_id)}×${d.quantity}`).join("<br>") || "—";
    tr.innerHTML = `
      <td>${esc(a.quote_id)}</td>
      <td><span class="pill ${a.kind}">${a.kind === "combo" ? "组合" : "单项"}</span></td>
      <td>${esc(sup[a.supplier_id] || a.supplier_id)}</td>
      <td>${a.accepted ? "✅ 接受" : "—"}</td>
      <td>${qty}</td>
      <td>${del}</td>
      <td>${yuan(a.cost_yuan)}</td>
      <td>${a.locked ? "🔒 锁定" : ""}</td>
      <td>${esc(a.reason || "")}</td>`;
    tbody.appendChild(tr);
  }
}

function renderCoverage(sol) {
  const tbody = $("#coverage-table tbody");
  tbody.innerHTML = "";
  if (!sol.result) return;
  for (const c of sol.result.coverage) {
    const tr = document.createElement("tr");
    const sources = c.sources.map((s) =>
      `${esc(s.material_id)}×${s.quantity}`).join(", ") || "—";
    tr.innerHTML = `
      <td>${esc(c.material_id)}</td>
      <td>${c.demand}</td>
      <td>${esc(c.equivalent_covered_exact)}</td>
      <td>${c.shortage ? `<strong style="color:var(--danger)">${c.shortage}</strong>` : 0}</td>
      <td>${sources}</td>`;
    tbody.appendChild(tr);
  }
}

function renderShadow(sol) {
  const box = $("#shadow-box");
  if (!sol.shadow) { box.hidden = true; return; }
  box.hidden = false;
  const tbody = $("#shadow-table tbody");
  tbody.innerHTML = "";
  for (const r of sol.shadow.demand_marginals) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>需求 ${esc(r.material_id)}</td>
      <td>${r.relax_one_unit_saves_yuan ?? "N/A（重求未证最优）"}</td>
      <td>${r.tighten_feasible
        ? (r.tighten_one_unit_costs_yuan ?? "N/A（重求未证最优）")
        : "不可行（再加 1 单位无方案）"}</td>
      <td>${esc(r.note || "")}</td>`;
    tbody.appendChild(tr);
  }
  const sl = sol.shadow.supplier_limit;
  if (sl) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>最多中标家数 = ${sl.limit}</td>
      <td>${sl.relax_one_more_supplier_saves_yuan
        ?? "N/A（重求未证最优）"}</td><td>—</td>
      <td>${esc(sl.note || (sl.relaxed_winners
        ? "放宽后中标：" + sl.relaxed_winners.join(", ") : ""))}</td>`;
    tbody.appendChild(tr);
  }
}

function renderInfeasible(sol) {
  const box = $("#infeasible-box");
  if (sol.status !== "infeasible" || !sol.infeasibility) {
    box.hidden = true; return;
  }
  box.hidden = false;
  const sh = sol.infeasibility.minimum_shortage
    .map((x) => `${esc(x.material_id)} 最少缺 ${x.quantity}`).join("；");
  box.innerHTML = `<div class="banner err">
    <strong>不可行诊断：</strong>${sh}<br>${esc(sol.infeasibility.note)}<br>
    可切换到「允许缺口（按罚金计费）」口径获得带罚金的最优分配。</div>`;
}

// ------------------------------------------------------------ why-not
async function showWhyNot(quoteId) {
  const body = $("#why-body");
  body.textContent = "分析中…";
  $("#dlg-why").showModal();
  try {
    const wn = await api(`/api/batches/${state.batchId}/why-not`, {
      method: "POST", body: { quote_id: quoteId, config: configPayload() },
    });
    let html = `<ul>${wn.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
    if (wn.blocked) { body.innerHTML = html; return; }
    html += `<table class="data-table">
      <tr><th>强制接受后多花(元)</th><td>${yuan(wn.extra_cost_yuan)}</td></tr>
      <tr><th>强制方案总成本(元)</th><td>${yuan(wn.forced_objective_yuan)}</td></tr>
      <tr><th>被挤掉的报价</th><td>${esc((wn.dropped_quotes || []).join(", ") || "无")}</td></tr>
      <tr><th>新进入的报价</th><td>${esc((wn.added_quotes || []).join(", ") || "无")}</td></tr>`;
    if (wn.base_winners)
      html += `<tr><th>中标家数</th><td>${esc(wn.base_winners.join(", "))} → ${esc(wn.forced_winners.join(", "))}</td></tr>`;
    html += "</table>";
    if (wn.forced?.allocations) {
      html += "<h3>强制接受时的分配</h3><table class='data-table'><thead><tr>" +
        "<th>报价</th><th>接受</th><th>数量/拆份</th><th>交付</th><th>金额</th>" +
        "</tr></thead><tbody>";
      for (const a of wn.forced.allocations) {
        if (!a.accepted) continue;
        const qty = a.kind === "combo"
          ? `${a.multiplier}/${a.divisor} 份` : a.quantity;
        const del = (a.deliveries || []).map((d) =>
          `${esc(d.material_id)}×${d.quantity}`).join(", ");
        html += `<tr><td>${esc(a.quote_id)}</td><td>✅</td><td>${qty}</td>
          <td>${del}</td><td>${yuan(a.cost_yuan)}</td></tr>`;
      }
      html += "</tbody></table>";
    }
    body.innerHTML = html;
  } catch (err) {
    body.textContent = "分析失败：" + err.message;
  }
}

// ------------------------------------------------------------ 情景
async function refreshScenarios() {
  state.scenarios = await api(
    `/api/batches/${state.batchId}/scenarios`);
  const ul = $("#scenario-list");
  ul.innerHTML = state.scenarios.length ? "" :
    '<li class="muted">暂无情景</li>';
  for (const s of state.scenarios) {
    const li = document.createElement("li");
    li.className = state.scenario?.id === s.id ? "active" : "";
    const tag = s.stale ? '<span class="tag stale">已失效</span>'
      : `<span class="tag ${s.status}">${statusLabel(s.status)}</span>`;
    li.innerHTML = `<strong>${esc(s.name)}</strong> ${tag}
      <div class="row2">${esc(s.mode)} · 目标 ${s.objective_yuan ?? "—"} 元</div>
      <div class="row2 muted">${s.stale ? esc(s.stale_reason) : s.id}</div>`;
    li.onclick = () => openScenario(s.id);
    ul.appendChild(li);
  }
}

function statusLabel(s) {
  return { optimal: "最优", feasible_only: "仅可行",
           infeasible: "不可行", unsolved: "未求解" }[s] || s;
}

async function openScenario(sid) {
  const doc = await api(
    `/api/batches/${state.batchId}/scenarios/${sid}`);
  state.scenario = doc;
  state.config = {
    mode: doc.config.mode || "strict",
    disabled_quote_ids: doc.config.disabled_quote_ids || [],
    locks: doc.config.locks || [],
    penalties: doc.config.penalties || {},
    default_penalty: doc.config.default_penalty ?? 100,
    max_winning_suppliers: doc.config.max_winning_suppliers ?? null,
    time_limit_seconds: doc.config.time_limit_seconds ?? 15,
  };
  $("#cfg-mode").value = state.config.mode;
  $("#penalty-wrap").hidden = state.config.mode !== "penalty";
  $("#cfg-penalty").value = state.config.default_penalty;
  $("#cfg-winners").value = state.config.max_winning_suppliers ?? "";
  $("#cfg-time").value = state.config.time_limit_seconds;
  renderQuotes(); renderLocks();
  $("#scenario-label").textContent = `当前情景：${doc.name}` +
    (doc.stale ? "（已失效，结果仅供查看）" : "");
  const staleBanner = $("#stale-banner");
  if (doc.stale) {
    staleBanner.hidden = false;
    staleBanner.textContent = "⚠ " + doc.stale_reason +
      "。旧结果保留可查，但不能覆盖保存；请处理撤回/变更后克隆为新情景重算。";
    $("#btn-save-scenario").hidden = true;
  } else {
    staleBanner.hidden = true;
    $("#btn-save-scenario").hidden = false;
  }
  $("#btn-clone").hidden = false;
  if (doc.solution) {
    state.solution = doc.solution;
    renderResult(doc.solution);
  }
  await refreshScenarios();
}

async function saveAsNewScenario() {
  const name = prompt("新情景名称：",
    `情景 ${new Date().toLocaleString("zh-CN")}`);
  if (!name) return;
  try {
    const created = await api(
      `/api/batches/${state.batchId}/scenarios`, {
        method: "POST", body: { name, config: configPayload() },
      });
    const sid = created.id;
    state.scenario = await api(
      `/api/batches/${state.batchId}/scenarios/${sid}`);
    await runSolve(true);
    await refreshScenarios();
  } catch (err) {
    alert("保存失败：\n" + err.message);
  }
}

async function cloneScenario() {
  if (!state.scenario) return;
  const name = prompt("克隆情景名称：", state.scenario.name + "（副本）");
  if (!name) return;
  try {
    await api(
      `/api/batches/${state.batchId}/scenarios/${state.scenario.id}/clone`,
      { method: "POST", body: { name } });
    await refreshScenarios();
    toast("已克隆（不含旧结果），打开后求解即可");
  } catch (err) {
    alert("克隆失败：" + err.message);
  }
}

function exportCsv() {
  if (!state.scenario) return;
  window.open(`/api/batches/${state.batchId}/scenarios/` +
    `${state.scenario.id}/export.csv`, "_blank");
}

// ------------------------------------------------------------ 输入编辑
function openInputDialog() {
  $("#input-json").value = JSON.stringify(state.raw, null, 2);
  $("#dlg-input").showModal();
}

async function updateInput(e) {
  e.preventDefault();
  let raw;
  try {
    raw = JSON.parse($("#input-json").value);
  } catch (err) {
    alert("JSON 解析失败：" + err.message);
    return;
  }
  if (!confirm("更新输入会使该批次所有已保存情景标记失效，确认继续？"))
    return;
  try {
    await api(`/api/batches/${state.batchId}`,
      { method: "PUT", body: raw });
    $("#dlg-input").close();
    toast("输入已更新，旧情景已标记失效");
    await openBatch(state.batchId);
  } catch (err) {
    alert("校验失败：\n" + err.message);
  }
}

init();
