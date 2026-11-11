// BeatSocial review desk front end. Talks only to the local server's /api/*.
// People see channels as "日文 X" / "英文 Threads"; internal ids stay in small print for AI hand-offs.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v) => (v == null || v === "" ? "—" : typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1)) : esc(v));
let META = null;
let ACC = {};
const state = { tab: "today", filters: { status: "review,rejected" }, selected: new Set(), statsBy: "account", statsWeek: "" };

function save() { try { localStorage.setItem("beatsocial", JSON.stringify({ tab: state.tab, filters: state.filters, statsBy: state.statsBy })); } catch (e) {} }
function load() { try { Object.assign(state, JSON.parse(localStorage.getItem("beatsocial") || "{}")); } catch (e) {} }

async function api(path, body) {
  const res = await fetch("/api/" + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.className = "toast hidden"), err ? 6000 : 2500);
}
async function run(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, true); throw e; }
}

// ------------------------------------------------------------ naming
// Market -> how people say it when reviewing ("日文 X", "英文 Threads").
const MARKET = { GLOBAL: "英文", JP: "日文", KR: "韩文", TW: "繁中", RU: "俄语", CN: "中文" };
const MARKET_ORDER = ["GLOBAL", "JP", "KR", "TW", "RU"]; // CN is run outside BeatSocial
const platform = (p) => esc(META.platforms[p] || p);
const country = (c) => esc(META.countries[c] || c);
function chan(id) {
  const a = ACC[id];
  if (!a) return esc(id || "");
  return `${esc(MARKET[a.country] || a.country)} ${platform(a.platform)}`;
}
function chanChip(id) {
  const a = ACC[id] || {};
  return `<span class="chip m-${esc((a.country || "x").toLowerCase())}"><i>${esc(MARKET[a.country] || a.country || "")}</i>${platform(a.platform || "")}</span>`;
}
const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
function dayKey(slot) { return (slot || "").slice(0, 10); }
function dayLabel(slot) {
  if (!slot) return "";
  const d = new Date(slot.slice(0, 10) + "T00:00:00");
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const diff = Math.round((d - t) / 864e5);
  const rel = { "-1": "昨天", 0: "今天", 1: "明天", 2: "后天" }[diff];
  return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAY[d.getDay()]}${rel ? ` · ${rel}` : ""}`;
}
function when(slot) { return slot ? `${dayLabel(slot)} ${slot.slice(11, 16)}` : ""; }
function title(p) {
  const first = (p.text || "").trim().split("\n")[0];
  return esc(p.topic || first.slice(0, 60) || p.type || "（还没写）");
}
function scopeLabel(s) {
  if (s === "global") return "全部渠道";
  const [k, v] = s.split(":");
  if (k === "account") return chan(v);
  if (k === "platform") return `所有 ${platform(v)}`;
  if (k === "country") return `${esc(MARKET[v] || v)}市场全部`;
  if (k === "type") return `内容类型：${esc(v)}`;
  return esc(s);
}
const statusBadge = (s, label) => `<span class="badge ${{ approved: "blue", published: "ok", measured: "ok", rejected: "bad", review: "warn" }[s] || ""}">${esc(label || META.statuses[s] || s)}</span>`;
const mediaUrl = (f) => "/media?path=" + encodeURIComponent(f);
// Only http(s) links are ever rendered as clickable; anything else (javascript:, data:) becomes "#".
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "").trim()) ? esc(String(u).trim()) : "#");
const HASHES = {};
const options = (obj, sel, blank) => (blank != null ? `<option value="">${esc(blank)}</option>` : "") + Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}" ${k === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
function weekName(w) {
  if (!META) return w;
  const tw = META.this_week;
  const rel = w === tw ? "本周" : w === shiftWeek(tw, -1) ? "上周" : w === shiftWeek(tw, 1) ? "下周" : "";
  return rel ? `${rel}（${w}）` : w;
}
function weekOptions(o, pick) {
  return [["last", shiftWeek(o.this_week, -1)], ["this", o.this_week], ["next", o.next_week]]
    .map(([k, w]) => `<option value="${w}" ${k === pick ? "selected" : ""}>${weekName(w)}</option>`).join("");
}
function shiftWeek(w, n) {
  const [y, k] = w.split("-W").map(Number);
  const jan4 = new Date(Date.UTC(y, 0, 4));
  const monday = new Date(jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * 864e5 + (k - 1 + n) * 7 * 864e5);
  const t = new Date(monday.getTime() + 3 * 864e5); // Thursday decides the ISO year
  const y2 = t.getUTCFullYear();
  const j4 = new Date(Date.UTC(y2, 0, 4));
  const wk = 1 + Math.round((monday - (j4.getTime() - ((j4.getUTCDay() + 6) % 7) * 864e5)) / (7 * 864e5));
  return `${y2}-W${String(wk).padStart(2, "0")}`;
}

// ------------------------------------------------------------ dropdowns
// Every <select> on the page is replaced by a styled dropdown; the hidden native select keeps the value and fires change.
const CHEVRON = '<svg viewBox="0 0 24 24" class="dd-chev"><path d="M6 9l6 6 6-6"/></svg>';
const CHECK = '<svg viewBox="0 0 24 24" class="dd-check"><path d="M5 12l5 5 9-10"/></svg>';
function closeDropdowns(except) { document.querySelectorAll(".dd.open").forEach((d) => { if (d !== except) d.classList.remove("open"); }); }
function enhanceSelects(root = document) {
  root.querySelectorAll("select:not([data-dd])").forEach((sel) => {
    sel.dataset.dd = "1";
    const wrap = document.createElement("div");
    wrap.className = "dd";
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dd-btn";
    const menu = document.createElement("div");
    menu.className = "dd-menu";
    menu.setAttribute("role", "listbox");
    wrap.append(btn, menu);
    const paint = () => { const o = sel.options[sel.selectedIndex]; btn.innerHTML = `<span>${esc(o ? o.text : "")}</span>${CHEVRON}`; };
    paint();
    sel.addEventListener("change", paint);
    btn.onclick = (e) => {
      e.stopPropagation();
      closeDropdowns(wrap);
      if (!wrap.classList.toggle("open")) return;
      menu.innerHTML = [...sel.options].map((o, i) => `<div class="dd-opt ${i === sel.selectedIndex ? "on" : ""}" data-i="${i}" role="option">${esc(o.text)}${CHECK}</div>`).join("");
      const on = menu.querySelector(".on");
      if (on) on.scrollIntoView({ block: "nearest" });
    };
    menu.onclick = (e) => {
      e.stopPropagation();
      const it = e.target.closest(".dd-opt");
      if (!it) return;
      sel.selectedIndex = Number(it.dataset.i);
      wrap.classList.remove("open");
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    };
  });
}
document.addEventListener("click", () => closeDropdowns());
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDropdowns(); $("#drawer").classList.add("hidden"); } });
new MutationObserver(() => enhanceSelects()).observe(document.documentElement, { childList: true, subtree: true });

// ------------------------------------------------------------------ tabs
const VIEWS = { today, markets, review, data, accounts, competitors, rules };
const PAGES = {
  today: ["Today", "今天", "这是看板，只看不点。看到要处理的，直接在 Claude Code 里说一句。"],
  markets: ["Markets", "市场", "6 个市场各有自己的主推方向和渠道。先选市场，再看它的渠道和帖子；主推方向会写进 AI 的写稿包。"],
  review: ["Posts", "帖子", "左边先选市场、再选渠道，右边按日期看帖子：正文、图和视频、AI 审稿意见。要改、要批，在 Claude Code 里说。"],
  data: ["Analytics", "数据", "按渠道、市场、平台、内容类型看表现。拿不到的数据记为未知，不当 0。"],
  accounts: ["Channels", "渠道账号", "所有市场 × 平台的账号在这里管理：谁来发、每周几条、几点发。"],
  competitors: ["Benchmarks", "竞对与对标", "对标样本库。生成帖子位时按同平台、同市场、同类型自动挑选。"],
  rules: ["Memory", "写法规则", "复盘 AI 提出的规则，你采纳后才进入写稿记忆 LESSONS.md。"],
};
async function show(tab) {
  state.tab = tab; save();
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const [eyebrow, t, desc] = PAGES[tab];
  $("#page-head").innerHTML = `<div class="eyebrow">${eyebrow}</div><h1>${t}</h1><p>${desc}</p>`;
  $("#view").innerHTML = '<div class="hint">加载中…</div>';
  try { await VIEWS[tab]($("#view")); } catch (e) { $("#view").innerHTML = `<div class="empty-state">出错了：${esc(e.message)}</div>`; }
}
function goReview(filters) { state.filters = Object.assign({ status: "review,rejected" }, filters); show("review"); }

// ------------------------------------------------------------------ today
async function today(el) {
  const o = await api("overview");
  const c = o.counts;
  navCounts(o);
  const tile = (n, l, cls, go) => `<div class="tile ${cls || ""}" data-go='${JSON.stringify(go || null)}'><div class="n">${n}</div><div class="l">${l}</div></div>`;
  const row = (p, timeOnly) => `<tr class="click" data-id="${esc(p.id)}"><td class="nowrap">${esc(timeOnly ? p.slot_at.slice(11, 16) : when(p.slot_at))}</td><td>${chanChip(p.account_id)}</td><td>${title(p)}</td>${p.status ? `<td>${statusBadge(p.status)}</td>` : ""}</tr>`;
  el.innerHTML = `
    <div class="tiles">
      ${tile(c.review + c.rejected, "待审 / 打回", c.review ? "warn" : "", { tab: "review", status: "review,rejected" })}
      ${tile(o.overdue.length, "已批准但过档未发", o.overdue.length ? "bad" : "", { tab: "review", status: "approved", past: true })}
      ${tile(c.approved, "已批准待发", "", { tab: "review", status: "approved" })}
      ${tile(o.metrics_due, "该采数据还没采", o.metrics_due ? "warn" : "", { tab: "data" })}
      ${tile(o.rules_proposed, "待确认的规则提案", o.rules_proposed ? "warn" : "", { tab: "rules" })}
      ${tile(c.todo, "待写帖子位", "", { tab: "review", status: "todo" })}
    </div>
    <h2>在 Claude Code 里这样说</h2>
    <div class="panel say">
      ${say("看一下今天要处理什么", "Claude 读这页的数据，按市场列给你")}
      ${say("把日文市场待审的帖子给我过一遍", "逐条列出正文和图，你说哪条行、哪条改")}
      ${say("英文 X 明天 21:30 那条开头太弱，改得更直接", "改完生成新版本，你再看")}
      ${say("这三条可以发", "Claude 带着你的原话批准，记进操作记录")}
      ${say("复盘上周，给我下周配额建议", "生成复盘和配额，你点头后生效")}
      ${say("下周的稿写一下", "生成帖子位 → 按市场批量写 → 独立 AI 审稿")}
    </div>
    ${o.pending_quotas.length ? `<h2>待你确认的配额 <span class="count">${o.pending_quotas.length}</span></h2>
      <div class="hint">复盘按上周各内容类型的表现给出的建议，你确认后才生效。</div>
      ${o.pending_quotas.map((q) => `<div class="card" style="margin-top:10px"><div class="card-head"><b>${esc(weekName(q.week))}</b><span class="meta">依据 ${esc(weekName(q.based_on || ""))} 的数据</span></div>
        <div class="table-wrap"><table><tr><th>渠道</th><th>每种内容排几条</th><th class="num">合计</th></tr>
        ${Object.entries(q.accounts || {}).map(([a, t]) => `<tr><td class="nowrap">${chanChip(a)}</td><td>${Object.entries(t).map(([k, n]) => `<span class="qchip"><span>${esc(k)}</span><b>${n}</b></span>`).join("")}</td><td class="num">${Object.values(t).reduce((x, y) => x + y, 0)}</td></tr>`).join("")}
        </table></div><div class="hint" style="margin-top:10px">要确认或改数字，在 Claude Code 里说「下周配额确认」或「英文 X 比价改成 6 条」。</div></div>`).join("")}` : ""}
    ${o.slots_without_benchmark ? `<div class="hint" style="margin-top:8px">有 ${o.slots_without_benchmark} 个待写帖子位没配到对标（这个平台和市场还没有样本），AI 只能按打法卡写。</div>` : ""}
    ${o.samples_missing_type || o.samples_missing_adapt ? `<h2>对标库待补</h2><div class="hint">${o.samples_missing_type} 条对标样本没标内容类型，${o.samples_missing_adapt} 条没写「怎么替换成我们的」。按 prompts/竞对刷新.md 让 AI 补齐后，对标会按内容类型精准匹配。</div>` : ""}
    <h2>库存：已批准的稿还能撑几天</h2>
    <div class="hint">少于 3 天黄色，少于 1 天红色。点一行进入这个渠道的审核。</div>
    <div class="table-wrap"><table><tr><th>渠道</th><th>账号</th><th class="num">每周</th><th class="num">可发</th><th class="num">待审</th><th class="num">待写</th><th class="num">够几天</th></tr>
    ${o.inventory.map((r) => `<tr class="click" data-acc="${esc(r.account)}"><td>${chanChip(r.account)}</td><td class="hint">${esc((ACC[r.account] || {}).handle || "")}</td><td class="num">${r.per_week}</td><td class="num">${r.approved_ready}</td><td class="num">${r.in_review}</td><td class="num">${r.todo}</td><td class="num level-${r.level}">${r.days_covered}</td></tr>`).join("") || '<tr><td colspan="7" class="hint">没有设置每周条数的运营中渠道</td></tr>'}
    </table></div>
    <h2>今天排期 <span class="count">${o.today.length}</span></h2>
    ${o.today.length ? `<div class="table-wrap"><table><tr><th>时间</th><th>渠道</th><th>内容</th><th>状态</th></tr>${o.today.map((p) => row(p, true)).join("")}</table></div>` : '<div class="empty-state">今天没有排期</div>'}
    ${o.overdue.length ? `<h2>过档未发 <span class="count">${o.overdue.length}</span></h2><div class="hint">已批准但过了发布时间 6 小时以上。点开可以改时间或取消。</div><div class="table-wrap"><table><tr><th>原定时间</th><th>渠道</th><th>内容</th></tr>${o.overdue.slice(0, 15).map((p) => row(p)).join("")}</table></div>${o.overdue.length > 15 ? `<div class="hint" style="margin-top:6px">还有 ${o.overdue.length - 15} 条：<a href="#" id="all-overdue">到审核页看全部</a></div>` : ""}` : ""}`;
  el.querySelectorAll(".tile").forEach((t) => t.onclick = () => { const g = JSON.parse(t.dataset.go); if (!g) return; if (g.tab === "review") goReview({ status: g.status, past: g.past }); else show(g.tab); });
  el.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => openPost(tr.dataset.id));
  el.querySelectorAll("tr[data-acc]").forEach((tr) => tr.onclick = () => goReview({ account: tr.dataset.acc, market: (ACC[tr.dataset.acc] || {}).country }));
  const all = $("#all-overdue"); if (all) all.onclick = (e) => { e.preventDefault(); goReview({ status: "approved", past: true }); };
}

// ------------------------------------------------------------------ markets
async function markets(el) {
  const rows = await api("markets");
  el.innerHTML = `<div class="markets">${rows.map((m) => `
    <section class="market m-${esc(m.code.toLowerCase())}">
      <header>
        <div class="market-name"><i></i><h3>${esc(m.name)}</h3></div>
        <div class="market-stats"><span><b>${m.channels.filter((c) => c.status === "active").length}</b> 个渠道在运营</span><span><b>${m.per_week}</b> 条/周</span><span class="${m.review ? "hot" : ""}"><b>${m.review}</b> 条待审</span></div>
      </header>
      <dl class="market-focus">
        <dt>主推</dt><dd>${esc(m.focus || "（还没写）")}</dd>
        <dt>对谁说</dt><dd>${esc(m.audience || "—")}</dd>
        <dt>引导去</dt><dd>${esc(m.cta || "—")}</dd>
        ${m.notes ? `<dt>备注</dt><dd class="hint">${esc(m.notes)}</dd>` : ""}
      </dl>
      <div class="market-chans">
        ${m.channels.map((c) => `<button class="mchan ${c.status !== "active" ? "off" : ""}" data-acc="${esc(c.id)}">
          <span class="mchan-name">${platform(c.platform)}</span>
          <span class="mchan-sub">${esc(c.handle || c.name)}${c.status !== "active" ? ` · ${esc(META.account_statuses[c.status])}` : c.per_week ? ` · ${c.per_week} 条/周` : ""}</span>
          ${c.review ? `<em>${c.review}</em>` : ""}
        </button>`).join("") || '<span class="hint">这个市场还没有渠道，到「账号」页新增。</span>'}
      </div>
      <footer><button class="primary" data-mk="${esc(m.code)}">看这个市场的帖子${m.review ? `（${m.review} 条待审）` : ""}</button><span class="hint">改主推方向：跟 Claude 说「${esc(MARKET[m.code])}市场主推改成…」</span></footer>
    </section>`).join("")}</div>`;
  el.querySelectorAll("[data-mk]").forEach((b) => b.onclick = () => goReview({ market: b.dataset.mk }));
  el.querySelectorAll(".mchan").forEach((b) => b.onclick = () => goReview({ market: (ACC[b.dataset.acc] || {}).country, account: b.dataset.acc }));
}

// ------------------------------------------------------------------ review
const STATUS_TABS = [
  ["review,rejected", "待审"],
  ["approved", "已批准待发"],
  ["published,measured", "已发布"],
  ["todo", "待写"],
  ["", "全部"],
];
async function review(el) {
  const f = state.filters;
  if (f.status === undefined) f.status = "review,rejected";
  const isPublished = f.status === "published,measured";
  const q = { status: f.status || "" };
  if (f.market) q.country = f.market;
  if (f.week) q.week = f.week;
  else if (!f.past && !isPublished) q.since = new Date().toISOString().slice(0, 10);
  if (!q.status) delete q.status;
  const posts = await api("posts?" + new URLSearchParams(q).toString());
  if (isPublished || f.past) posts.reverse();
  const counts = {};
  posts.forEach((p) => (counts[p.account_id] = (counts[p.account_id] || 0) + 1));
  const shownAll = f.account ? posts.filter((p) => p.account_id === f.account) : posts;
  const shown = shownAll.slice(0, 120);

  // channel rail grouped by market
  const groups = {};
  META.accounts.filter((a) => a.status === "active" || counts[a.id]).forEach((a) => (groups[a.country] = groups[a.country] || []).push(a));
  const mcount = (m) => (groups[m] || []).reduce((n, a) => n + (counts[a.id] || 0), 0);
  const marketsShown = f.market ? [f.market] : MARKET_ORDER.filter((m) => groups[m]);
  const rail = `<div class="rail-markets"><button class="mk ${!f.market ? "on" : ""}" data-mk="">全部市场</button>${MARKET_ORDER.map((m) => `<button class="mk m-${m.toLowerCase()} ${f.market === m ? "on" : ""}" data-mk="${m}"><i></i>${esc(MARKET[m])}</button>`).join("")}</div>` +
    `<button class="ch ${!f.account ? "on" : ""}" data-ch=""><span>${f.market ? `${esc(MARKET[f.market])}全部渠道` : "全部渠道"}</span><em>${posts.length || ""}</em></button>` +
    marketsShown.filter((m) => groups[m]).map((m) => `<div class="rail-group">${esc(META.countries[m] || m)}<span>${mcount(m) || ""}</span></div>` +
      groups[m].map((a) => `<button class="ch ${f.account === a.id ? "on" : ""} ${counts[a.id] ? "" : "zero"}" data-ch="${esc(a.id)}"><span>${platform(a.platform)}</span><em>${counts[a.id] || ""}</em></button>`).join("")).join("");

  // posts grouped by day
  let lastDay = null;
  const list = shown.map((p) => {
    const d = dayKey(p.slot_at);
    const head = d !== lastDay ? `<div class="day"><b>${esc(dayLabel(p.slot_at))}</b><span>${shownAll.filter((x) => dayKey(x.slot_at) === d).length} 条</span></div>` : "";
    lastDay = d;
    return head + card(p, true);
  }).join("");

  el.innerHTML = `
    <div class="review">
      <aside class="rail">${rail}</aside>
      <section class="review-main">
        ${f.status === "review,rejected" ? `<div class="howto"><span><b>1</b>左边选市场和渠道</span><span><b>2</b>看正文、图、AI 审稿意见</span><span><b>3</b>在 Claude Code 里说：「日文 X 11月16日 07:30 这条可以发」或「这条开头改短」</span></div>` : ""}
        <div class="filters">
          <div class="seg">${STATUS_TABS.map(([v, l]) => `<button data-st="${v}" class="${(f.status || "") === v ? "on" : ""}">${l}</button>`).join("")}</div>
          <label>周 <select id="f-week">${options(Object.fromEntries(META.weeks.map((w) => [w, weekName(w)])), f.week, "今天以后")}</select></label>
          ${!f.week && !isPublished ? `<label><input type="checkbox" id="past" ${f.past ? "checked" : ""}> 含过去的</label>` : ""}
          <span class="spacer"></span>
        </div>
        <div class="list-head">${f.account ? chanChip(f.account) : f.market ? esc(META.countries[f.market]) : "全部市场"} <span class="hint">共 ${shownAll.length} 条${shownAll.length > shown.length ? `，先显示前 ${shown.length} 条` : ""}</span></div>
        <div id="list">${list || `<div class="empty-state">这里没有帖子。${f.status === "review,rejected" ? "待审的稿都处理完了。" : "换个状态或渠道看看。"}</div>`}</div>
      </section>
    </div>`;
  el.querySelectorAll("[data-ch]").forEach((b) => b.onclick = () => { state.filters.account = b.dataset.ch || undefined; save(); review(el); });
  el.querySelectorAll(".rail [data-mk]").forEach((b) => b.onclick = () => { state.filters.market = b.dataset.mk || undefined; state.filters.account = undefined; save(); review(el); });
  el.querySelectorAll("[data-st]").forEach((b) => b.onclick = () => { state.filters.status = b.dataset.st; state.filters.past = undefined; save(); review(el); });
  $("#f-week").onchange = (e) => { state.filters.week = e.target.value || undefined; save(); review(el); };
  const past = $("#past"); if (past) past.onchange = (e) => { state.filters.past = e.target.checked || undefined; save(); review(el); };
  shown.forEach((p) => bindCard($("#list").querySelector(`[data-card="${CSS.escape(p.id)}"]`), p));
}

function say(text, what) {
  return `<div class="say-row"><span class="say-q">「${esc(text)}」</span><span class="hint">${esc(what)}</span></div>`;
}

function navCounts(o) {
  const set = (id, n, hot) => { const e = $(id); if (e) { e.textContent = n ? String(n) : ""; e.classList.toggle("hot", !!hot); } };
  set("#nav-review", o.counts.review + o.counts.rejected, true);
  set("#nav-today", o.overdue.length, o.overdue.length > 0);
  set("#nav-rules", o.rules_proposed, true);
}

function card(p, selectable) {
  HASHES[p.id] = p.content_hash;
  const blocks = p.flags.filter((x) => x.level === "block");
  const canApprove = ["review", "rejected"].includes(p.status) && !blocks.length;
  const needsPrice = p.flags.some((x) => x.level === "price");
  const done = ["published", "measured", "cancelled"].includes(p.status);
  const ai = p.ai_review ? `<span class="badge ${p.ai_review === "pass" ? "ok" : "bad"}">AI 审${p.ai_review === "pass" ? "通过" : "不通过"}</span>` : (p.status === "review" ? '<span class="badge">AI 未审</span>' : "");
  const fb = p.last_feedback && ["review", "rejected", "todo"].includes(p.status) ? feedbackText(p.last_feedback) : "";
  const stale = p.status === "approved" && !p.approval_valid ? '<span class="badge bad">批准后内容变过</span>' : "";
  const media = (p.media || []).map((m) => m.file
    ? `<div class="media-slot">${m.kind === "video" ? `<video src="${mediaUrl(m.file)}" controls preload="metadata"></video>` : `<a href="${mediaUrl(m.file)}" target="_blank"><img loading="lazy" src="${mediaUrl(m.file)}"></a>`}<div class="cap">${m.kind === "video" ? "视频" : "图片"} ${m.slot}${m.spec ? " · " + esc(m.spec.slice(0, 50)) : ""}</div></div>`
    : `<div class="media-slot empty"><div class="ph">还没做：${esc(m.spec || "未写要求")}</div><div class="cap">${m.kind === "video" ? "视频" : "图片"} ${m.slot}</div></div>`).join("") +
    "";
  const b = p.benchmark;
  const met = Object.values(p.metrics || {});
  return `<div class="card ${blocks.length ? "blocked" : ""}" data-card="${esc(p.id)}">
    <div class="card-head">
      ${chanChip(p.account_id)}
      <span class="card-title">${title(p)}</span>
      <span class="spacer"></span>
      ${ai} ${stale} ${statusBadge(p.status, p.status_label)}
    </div>
    <div class="card-meta">${esc(when(p.slot_at))}${p.type ? ` · ${esc(p.type)}` : ""} · ${esc(p.handle || p.account_name || "")} · 由 ${esc(p.bot || "未定")} 发布</div>
    ${fb ? `<div class="flag warn" style="margin:8px 0 0">${fb}</div>` : ""}
    ${p.flags.length ? `<div class="flags">${p.flags.map((x) => `<div class="flag ${x.level}">${esc(x.msg)}</div>`).join("")}</div>` : ""}
    <div class="cols">
      <div class="body">
        <div class="field-label">正文</div><div class="text">${esc(p.text) || '<span class="hint">（还没写）</span>'}</div>
        ${p.reply_thread ? `<div class="field-label">首评 / 回复</div><div class="text">${esc(p.reply_thread)}</div>` : ""}
        ${p.zh_ref ? `<div class="field-label">中文参考（不发布）</div><div class="text zh">${esc(p.zh_ref)}</div>` : ""}
        ${p.writer_note ? `<div class="field-label">写稿备注</div><div class="hint">${esc(p.writer_note)}</div>` : ""}
      </div>
      <div class="side">
        <div class="field-label">图片 / 视频</div>
        <div class="media-grid">${media || '<span class="hint">纯文字</span>'}</div>
        ${b ? `<div class="field-label">参考的竞对</div><div class="bench"><a href="${safeUrl(b.url)}" target="_blank" rel="noopener">${esc(b.competitor_name || b.author || "原帖")}</a>${b.impressions != null ? ` · 曝光 ${fmt(b.impressions)}` : ""}${b.structure ? `<br>${esc(b.structure)}` : ""}${b.how_to_adapt ? `<br><span class="hint">怎么改成我们的：${esc(b.how_to_adapt)}</span>` : ""}</div>` : ""}
        ${p.published_url ? `<div class="field-label">已发布</div><a href="${safeUrl(p.published_url)}" target="_blank" rel="noopener">打开帖子</a> <span class="hint">${esc(when(p.published_at))}</span>` : ""}
        ${met.length ? `<div class="field-label">数据</div><div class="metrics">${met.map((m) => `<div><b>${m.period === "d7" ? "7 天" : m.period === "h24" ? "24 小时" : esc(m.period)}</b>　曝光 ${fmt(m.impressions)} · 赞 ${fmt(m.likes)} · 回复 ${fmt(m.replies)} · 转发 ${fmt(m.reposts)}</div>`).join("")}</div>` : ""}
      </div>
    </div>
    <div class="actions">
      <button class="act-history ghost-btn">看修改记录</button>
      <span class="spacer"></span>
      <span class="pid" title="给 AI 或 bot 指认这条时用">编号 ${esc(p.id)} · 第 ${p.current_v} 版</span>
    </div>
    <div class="events hidden"></div>
  </div>`;
}

function bindCard(el, p) {
  if (!el) return;
  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = fn; };
  on(".act-history", async () => {
    const box = el.querySelector(".events");
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    const full = await api("post?id=" + encodeURIComponent(p.id));
    const NAMES = { create: "建帖子位", draft: "交稿", edit: "改稿", approve: "批准", approval_void: "批准失效", reject: "打回", note: "备注", claim: "bot 领取", receipt: "已发布", metric: "记录数据", ai_review: "AI 审稿", media: "设置素材", reschedule: "改时间", cancel: "取消", import: "迁移", off_plan: "计划外发布" };
    box.innerHTML = full.events.map((e) => `<div>${esc((e.at || "").slice(5, 16))} · ${esc(e.actor)} · ${esc(NAMES[e.action] || e.action)} <span class="hint">${esc((e.detail || "").slice(0, 200))}</span></div>`).join("") +
      `<div>版本：${full.versions.map((v) => `第 ${v.v} 版（${esc(v.author || "")} ${esc((v.created_at || "").slice(5, 16))}）`).join("，")}</div>`;
    box.classList.remove("hidden");
  });
}

function feedbackText(e) {
  let d = e.detail || "";
  try { const j = JSON.parse(d); if (j && typeof j === "object") d = [j.result === "fail" ? "不通过" : j.result === "pass" ? "通过" : "", ...(j.must_fix || []), j.notes || ""].filter(Boolean).join("；"); } catch (x) {}
  return `${e.action === "reject" ? "打回意见" : "AI 审稿意见"}（${esc(e.actor)} ${esc((e.at || "").slice(5, 16))}）：${esc(d.slice(0, 400))}`;
}

async function openPost(id) {
  const p = await api("post?id=" + encodeURIComponent(id));
  $("#drawer-content").innerHTML = card(p, false);
  $("#drawer").classList.remove("hidden");
  bindCard($("#drawer-content .card"), p);
}

// ------------------------------------------------------------------ data
async function data(el) {
  const dims = { account: "渠道", country: "市场", platform: "平台", type: "内容类型", week: "周", bot: "发布 bot" };
  el.innerHTML = `
    <div class="filters">
      <label>按 <select id="by">${options(dims, state.statsBy)}</select> 统计</label>
      <label>周 <select id="sw">${options(Object.fromEntries(META.weeks.map((w) => [w, weekName(w)])), state.statsWeek, "全部")}</select></label>
      <label>口径 <select id="sp">${options({ h24: "只看 24 小时", d7: "只看 7 天" }, state.statsPeriod, "7 天优先")}</select></label>
      <span class="hint">拿不到的记为未知，不当 0。</span>
    </div>
    <div id="stats"></div>
    <h2>该采还没采的数据</h2><div id="due"></div>`;
  $("#by").onchange = (e) => { state.statsBy = e.target.value; save(); data(el); };
  $("#sw").onchange = (e) => { state.statsWeek = e.target.value; data(el); };
  $("#sp").onchange = (e) => { state.statsPeriod = e.target.value; data(el); };
  const rows = await api(`stats?by=${state.statsBy}${state.statsWeek ? "&week=" + state.statsWeek : ""}${state.statsPeriod ? "&period=" + state.statsPeriod : ""}`);
  const max = Math.max(1, ...rows.map((r) => r.median_impressions || 0));
  const label = (k) => state.statsBy === "account" ? chanChip(k) : state.statsBy === "country" ? country(k) : state.statsBy === "platform" ? platform(k) : state.statsBy === "week" ? esc(weekName(k)) : esc(k);
  $("#stats").innerHTML = `<div class="table-wrap"><table><tr><th>${dims[state.statsBy]}</th><th class="num">计划</th><th class="num">已发</th><th>中位曝光</th><th class="num">总曝光</th><th class="num">最高</th><th class="num">中位赞</th><th class="num">互动率%</th><th class="num">缺数据</th><th class="num">待审</th><th class="num">待发</th></tr>
    ${rows.map((r) => `<tr><td class="nowrap">${label(r.key)}</td><td class="num">${r.planned}</td><td class="num">${r.published}</td>
      <td><div class="row">${r.median_impressions != null ? `<div class="bar" style="width:${Math.max(3, Math.round((r.median_impressions / max) * 120))}px"></div>` : ""}<span class="mono-num">${fmt(r.median_impressions)}</span></div></td>
      <td class="num">${fmt(r.total_impressions)}</td><td class="num">${fmt(r.max_impressions)}</td><td class="num">${fmt(r.median_likes)}</td><td class="num">${fmt(r.engagement_rate)}</td><td class="num">${r.missing_metrics || ""}</td><td class="num">${r.in_review || ""}</td><td class="num">${r.approved_waiting || ""}</td></tr>`).join("")}
  </table></div>`;
  const due = await api("metrics_due");
  $("#due").innerHTML = due.length ? `<div class="hint">共 ${due.length} 条，由各渠道 bot 采集写入。点一行看帖子。</div><div class="table-wrap" style="margin-top:8px"><table><tr><th>渠道</th><th>要采的数据</th><th class="num">发布后</th><th>谁来采</th><th>帖子</th></tr>${due.slice(0, 100).map((d) => `<tr class="click" data-id="${esc(d.id)}"><td>${chanChip(d.account)}</td><td>${d.period === "d7" ? "7 天数据" : "24 小时数据"}</td><td class="num">${Math.round(d.hours_since / 24)} 天</td><td>${esc(d.bot || "")}</td><td>${d.url ? `<a href="${safeUrl(d.url)}" target="_blank" rel="noopener">打开</a>` : ""}</td></tr>`).join("")}</table></div>` : '<div class="empty-state">没有待采的数据</div>';
  el.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = (e) => { if (e.target.tagName !== "A") openPost(tr.dataset.id); });
}

// ------------------------------------------------------------------ accounts
async function accounts(el) {
  const rows = await api("accounts");
  const groups = {};
  rows.forEach((a) => (groups[a.country] = groups[a.country] || []).push(a));
  el.innerHTML = `<div class="row"><h2 style="margin:0">全部渠道 <span class="count">${rows.length}</span></h2><span class="spacer"></span><span class="hint">新增或修改渠道：跟 Claude 说「加一个韩文 Threads 账号 @xxx，每周 7 条，Yuki 发」</span></div>
    <div class="hint" style="margin:6px 0 4px">按市场分组。每周条数和发布时间决定「生成帖子位」，发布 bot 决定谁来发。点一行看这个渠道的帖子。</div>
    ${MARKET_ORDER.filter((c) => groups[c]).map((c) => `<h2>${country(c)} <span class="count">${groups[c].length}</span></h2><div class="table-wrap"><table><tr><th>渠道</th><th>账号</th><th>谁来发</th><th class="num">每周</th><th>发布时间（北京）</th><th>状态</th></tr>
      ${groups[c].map((a) => `<tr class="click" data-acc="${esc(a.id)}"><td class="nowrap">${chanChip(a.id)}</td><td>${esc(a.handle || "")}</td><td>${esc(a.bot || "")}</td><td class="num">${a.per_week || 0}</td><td class="hint">${esc((a.slot_times || []).join("  "))}</td><td><span class="badge ${a.status === "active" ? "ok" : a.status === "paused" ? "bad" : "warn"}">${esc(META.account_statuses[a.status])}</span></td></tr>`).join("")}
    </table></div>`).join("")}`;
  el.querySelectorAll("tr[data-acc]").forEach((tr) => tr.onclick = () => goReview({ account: tr.dataset.acc, market: (ACC[tr.dataset.acc] || {}).country }));
}

// ------------------------------------------------------------------ competitors
async function competitors(el) {
  const f = state.cf || {};
  const comps = await api("competitors");
  el.innerHTML = `
    <div class="filters">
      <label>平台 <select data-c="platform">${options(META.platforms, f.platform, "全部")}</select></label>
      <label>市场 <select data-c="country">${options(META.countries, f.country, "全部")}</select></label>
      <label>竞对 <select data-c="competitor">${options(Object.fromEntries(comps.map((c) => [c.id, `${c.name}（${c.samples}）`])), f.competitor, "全部")}</select></label>
      <span class="hint">新样本由 bot 每周补充；生成帖子位时自动挑对标。</span>
    </div><div id="samples"></div>`;
  el.querySelectorAll("[data-c]").forEach((s) => s.onchange = () => { state.cf = Object.assign({}, f, { [s.dataset.c]: s.value || undefined }); competitors(el); });
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
  const rows = await api("samples?" + qs);
  $("#samples").innerHTML = `<div class="hint" style="margin-bottom:8px">共 ${rows.length} 条，按曝光从高到低</div><div class="table-wrap"><table><tr><th>竞对</th><th>平台 · 市场</th><th>开头 / 结构</th><th class="num">曝光</th><th class="num">赞</th><th class="num">被参考</th><th>怎么改成我们的</th></tr>
    ${rows.map((s) => `<tr><td><a href="${safeUrl(s.url)}" target="_blank" rel="noopener">${esc(s.competitor_name || s.author || "原帖")}</a><div class="hint">${esc((s.posted_at || "").slice(0, 10))}</div></td><td class="nowrap">${platform(s.platform)} · ${esc(MARKET[s.country] || s.country || "")}</td><td>${esc((s.hook || "").slice(0, 90))}<div class="hint">${esc(s.structure || "")}</div></td><td class="num">${fmt(s.impressions)}</td><td class="num">${fmt(s.likes)}</td><td class="num">${s.used || ""}</td><td class="hint">${esc((s.how_to_adapt || "").slice(0, 140))}</td></tr>`).join("")}
  </table></div>`;
}

// ------------------------------------------------------------------ rules
async function rules(el) {
  const rows = await api("rules");
  const item = (r) => `<tr><td><span class="badge ${r.status === "active" ? "ok" : r.status === "proposed" ? "warn" : ""}">${esc(META.rule_statuses[r.status])}</span></td><td class="nowrap">${scopeLabel(r.scope)}</td><td>${esc(r.text)}${r.evidence ? `<div class="hint">依据：${esc(r.evidence)}</div>` : ""}</td><td class="hint nowrap">${esc(r.proposed_by || "")} ${esc((r.proposed_at || "").slice(5, 10))}</td>
    <td class="hint nowrap">R${r.id}</td></tr>`;
  el.innerHTML = `
    <div class="hint">复盘 AI 提出规则 → 你在 Claude Code 里说「R3 采纳」或「R5 不要」→ 自动写进 memory/LESSONS.md，下次写稿时 AI 会读到。</div>
    <div class="table-wrap" style="margin-top:12px"><table><tr><th>状态</th><th>适用于</th><th>规则</th><th>提出</th><th></th></tr>${rows.map(item).join("") || '<tr><td colspan="5" class="hint">还没有规则</td></tr>'}</table></div>
`;
}

// ------------------------------------------------------------------ boot
async function refreshMeta() {
  META = await api("meta");
  ACC = Object.fromEntries(META.accounts.map((a) => [a.id, a]));
}
(async function boot() {
  load();
  await refreshMeta();
  $("#who").textContent = `${META.user} · ${META.data_dir.split("/").pop()}`;
  $("#who").title = META.data_dir;
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => (b.dataset.tab === "review" ? goReview({}) : show(b.dataset.tab))));
  $("#drawer-close").onclick = () => $("#drawer").classList.add("hidden");
  $("#theme").onclick = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("beatsocial-theme", root.dataset.theme); } catch (e) {}
  };
  api("overview").then(navCounts).catch(() => {});
  $("#drawer").onclick = (e) => { if (e.target.id === "drawer") $("#drawer").classList.add("hidden"); };
  show(VIEWS[state.tab] ? state.tab : "today");
})();
