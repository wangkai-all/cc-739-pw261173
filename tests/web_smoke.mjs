// 前端逻辑冒烟：轻量 DOM 桩 + 真实后端（需先启动 app.main，端口 8080）。
// 不依赖 jsdom；验证 app.js 状态流、渲染与 API 对接无引用错误。
import { readFileSync } from "node:fs";

let seq = 0;
class FakeEl {
  constructor(tag = "div", parent = null) {
    this.tag = tag; this.parent = parent;
    this._id = ++seq;
    this.children = []; this.listeners = {};
    this._attrs = {}; this.style = {};
    this.hidden = false; this.value = ""; this.textContent = "";
    this._html = ""; this.className = ""; this.title = "";
    this.files = [];
  }
  set innerHTML(v) {
    this._html = v;
    this.children = [];
    // 解析出标签骨架，供 querySelector("input"/"button") 等使用
    const tagRe = /<(\w+)([^>]*)>/g;
    let m;
    while ((m = tagRe.exec(v))) {
      const child = new FakeEl(m[1], this);
      const attrs = m[2];
      const cls = attrs.match(/class="([^"]*)"/);
      if (cls) child._attrs.class = cls[1];
      const idm = attrs.match(/id="([^"]*)"/);
      if (idm) child._attrs.id = idm[1];
      child._html = attrs;
      this.children.push(child);
    }
  }
  get innerHTML() { return this._html; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  addEventListener(t, fn) { this.listeners[t] = fn; }
  set onclick(fn) { this.listeners.click = fn; }
  get onclick() { return this.listeners.click; }
  click() { this.listeners.click && this.listeners.click({}); }
  showModal() { this._open = true; }
  close() { this._open = false; }
  // 仅支持测试用到的选择器
  querySelector(sel) {
    if (sel === ".ops" || sel.startsWith(".ops")) {
      return this.children.find((c) => c._attrs.class === "ops")
        || walkFind(this, (e) => e._attrs.class === "ops");
    }
    if (!sel.startsWith("#") && !sel.startsWith(".")) {
      // 标签选择器（input/button 等）
      return walkFind(this, (e) => e.tag === sel);
    }
    return walkFind(this, (e) => e._matches && e._matches(sel));
  }
}
function walkFind(root, pred) {
  for (const c of root.children) {
    if (pred(c)) return c;
    const hit = walkFind(c, pred);
    if (hit) return hit;
  }
  return null;
}
const rootById = {};
function byId(id) {
  if (!rootById[id]) rootById[id] = new FakeEl("div");
  return rootById[id];
}
globalThis.document = {
  querySelector(sel) {
    const parts = sel.trim().split(/\s+/);
    const fm = parts[0].match(/^#([\w-]+)$/);
    let el = fm ? byId(fm[1]) : byId("__anon__" + parts[0]);
    for (const p of parts.slice(1)) {
      let next;
      if (p.startsWith("#")) next = byId(p.slice(1));
      else if (p.startsWith("."))
        next = walkFind(el, (e) =>
          (e._attrs.class || "").split(/\s+/).includes(p.slice(1)));
      else next = walkFind(el, (e) => e.tag === p);
      if (!next) { next = new FakeEl(p, el); el.appendChild(next); }
      el = next;
    }
    return el;
  },
  createElement(tag) { return new FakeEl(tag); },
};
globalThis.window = { open() {} };
const nativeFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (path, opts) =>
  nativeFetch(new URL(path, "http://127.0.0.1:8080"), opts);
const alerts = [];
globalThis.alert = (m) => alerts.push(m);
globalThis.confirm = () => true;
globalThis.prompt = (_, def) => def || "测试情景";

const appSrc = readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
await import("data:text/javascript," + encodeURIComponent(appSrc));
await new Promise((r) => setTimeout(r, 700));

const $ = (s) => document.querySelector(s);
const assert = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } };
const findAll = (root, pred) => {
  const out = [];
  for (const c of root.children) {
    if (pred(c)) out.push(c);
    out.push(...findAll(c, pred));
  }
  return out;
};
const btnWithText = (root, text) =>
  findAll(root, (e) => (e.textContent || "").includes(text)).pop();

// 1. init 后批次列表有渲染（demo/conflict 已由 e2e 导入）
assert($("#batch-list").children.length >= 2, "batch list rendered");

// 2. 打开 demo
const demoLi = $("#batch-list").children.find(
  (li) => li.innerHTML.includes("demo"));
assert(demoLi, "demo li exists");
demoLi.click();
await new Promise((r) => setTimeout(r, 700));
assert($("#workspace").hidden === false, "workspace shown");
const quoteRows = $("#quote-table tbody").children;
// 撤回报价行照常渲染（withdrawn 状态），共 8 行
assert(quoteRows.length === 8, "8 quotes rendered, got " + quoteRows.length);

// 3. 求解
$("#btn-solve").click();
await new Promise((r) => setTimeout(r, 1800));
const banner = $("#status-banner").textContent;
assert(banner.includes("OPTIMAL"), "optimal banner: " + banner);
const allocRows = $("#alloc-table tbody").children;
assert(allocRows.length === 8, "alloc rows");
assert(allocRows.some((tr) => tr.className === "accepted"), "accepted rows");
assert($("#coverage-table tbody").children.length === 3, "coverage rows");
assert($("#shadow-box").hidden === false, "shadow shown");
// KPI 总成本必须与后端独立求解的目标金额一致（不写死金额，适配数据状态）
const sol0 = await (await fetch(new URL(
  "/api/batches/demo/solve", "http://127.0.0.1:8080"), {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ config: { mode: "strict",
    time_limit_seconds: 10 } }),
})).json();
const expectYuan = Number(sol0.objective_yuan).toLocaleString("zh-CN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
assert($("#kpi-row").children.some((k) =>
  k.innerHTML.includes(expectYuan)), "KPI amount " + expectYuan);
assert(alerts.length === 0, "no alerts: " + alerts.join("|"));

// 4. why-not K3
const k3row = quoteRows.find((tr) => tr.innerHTML.includes("K3"));
const why = btnWithText(k3row, "为什么没选它");
assert(why, "why-not button exists");
why.click();
await new Promise((r) => setTimeout(r, 1200));
const whyHtml = $("#why-body").innerHTML;
// 与直接调 API 的结果交叉核对，不写死金额（适配数据状态）
const wn0 = await (await fetch(new URL(
  "/api/batches/demo/why-not", "http://127.0.0.1:8080"), {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ quote_id: "K3",
    config: { mode: "strict", time_limit_seconds: 10 } }),
})).json();
const extraTxt = Number(wn0.extra_cost_yuan).toLocaleString("zh-CN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
assert(whyHtml.includes("强制") && whyHtml.includes(extraTxt),
  "why-not content " + extraTxt + ": " + whyHtml.slice(0, 150));

// 5. 另存新情景并求解保存
$("#btn-save-new").click();
await new Promise((r) => setTimeout(r, 2200));
assert($("#scenario-list").children.length >= 1, "scenario saved");
assert($("#scenario-list").children.some((li) =>
  li.innerHTML.includes("最优")), "scenario tagged optimal");

// 6. 撤回一条仍有效的报价，验证情景出现「已失效」标签
const targetRow = quoteRows.find((tr) => btnWithText(tr, "撤回"));
assert(targetRow, "an active quote row with withdraw button");
btnWithText(targetRow, "撤回").click();
await new Promise((r) => setTimeout(r, 900));
assert($("#scenario-list").children.some((li) =>
  li.innerHTML.includes("已失效")), "stale tag shown after withdraw");
assert(alerts.length === 0, "no alerts after withdraw: " + alerts.join("|"));

console.log("前端逻辑冒烟全部通过");
