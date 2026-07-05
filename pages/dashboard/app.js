/* ═══════════════════════════════════════════════
   Overstats Monitor — Dashboard App
   Bridge API + SSE + SVG 折线图 + 可折叠指令列表 + 节点点击筛选
   全部中文化
   ═══════════════════════════════════════════════ */

// ★ 如果 window.AstrBotPluginPage 不存在（本地测试环境），创建 mock bridge
if (!window.AstrBotPluginPage) {
  console.warn("[Monitor] AstrBotPluginPage not found, creating mock bridge");
  window.AstrBotPluginPage = {
    _mock: true,
    async ready() {
      console.log("[MockBridge] ready");
      return { pluginName: "overstats_full", pageTitle: "Overstats 监控 (本地测试)" };
    },
    async apiGet(endpoint, params) {
      let url = "/api/overstats_full/" + endpoint;
      if (params && Object.keys(params).length) {
        const qs = Object.entries(params)
          .filter(([, v]) => v !== "" && v != null)
          .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v))
          .join("&");
        if (qs) url += "?" + qs;
      }
      console.log("[MockBridge] GET", url);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    },
    async subscribeSSE(endpoint, handlers) {
      const url = "/api/overstats_full/" + endpoint;
      console.log("[MockBridge] SSE", url);
      try {
        const es = new EventSource(url);
        es.onopen = () => handlers.onOpen?.();
        es.onmessage = (e) => {
          try {
            const parsed = JSON.parse(e.data);
            if (!parsed._heartbeat) handlers.onMessage?.({ parsed, raw: e.data, event: e });
          } catch (_) { }
        };
        es.addEventListener("init", (e) => {
          try {
            const data = JSON.parse(e.data);
            if (Array.isArray(data)) data.forEach(item => handlers.onMessage?.({ parsed: item, raw: e.data, event: e }));
          } catch (_) { }
        });
        es.onerror = (e) => handlers.onError?.(e);
        return "mock-sse";
      } catch (e) {
        console.error("[MockBridge] SSE error:", e);
        return null;
      }
    },
    onContext(cb) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", (e) => cb({ isDark: e.matches }));
    },
    getContext() {
      return { isDark: window.matchMedia("(prefers-color-scheme: dark)").matches };
    },
  };
}

const bridge = window.AstrBotPluginPage;

// ── State ──
let overviewData = null;
let commandsData = [];
let trendData = [];
let hourlyData = [];
let backendPerf = [];
let upstreamData = [];
let selectedCategory = "";
let selectedTimeRange = "week";
let selectedErrorLevel = "";
let selectedTrendDate = new Date().toISOString().slice(0, 10); // 默认最新24小时
let cmdListExpanded = false;
let sseSubId = null;

// 是区吗调用日志状态
let shiquData = { records: [], summary: {}, total: 0, offset: 0, limit: 30 };
let shiquSelectedIdx = -1; // 当前选中行（侧边栏显示详情）

// ── Category list ──
const CATEGORIES = ["基础绑定", "数据查询", "总结", "图表排行", "游戏资讯", "AI开庭", "管理部署"];

// ── Time range ─ days mapping ──
const TRANGE_DAYS = { "24h": 1, "3d": 3, "week": 7, "month": 30, "all": 90 };

// ══════════ Init ══════════

// ══════════ Init ══════════

async function init() {
  // 显示初始化状态
  function status(msg, color) {
    const el = document.getElementById("init-status");
    if (el) { el.textContent = msg; el.style.color = color; }
  }
  status("初始化中...", "var(--c-warning)");

  // ★ 事件绑定必须在任何异步操作之前，确保无论 bridge 是否可用，UI 交互都能响应
  function safeBind(id, event, fn) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, fn);
  }
  safeBind("btn-expand-cmds", "click", toggleCmdList);
  safeBind("btn-refresh", "click", refreshAll);
  safeBind("btn-clear-errors", "click", clearErrors);
  safeBind("btn-clear-stats", "click", clearAllStats);
  safeBind("btn-shiqu-search", "click", shiquSearch);
  safeBind("btn-shiqu-reset", "click", shiquReset);
  safeBind("shiqu-side-close", "click", closeShiquDetail);
  safeBind("cmd-search", "input", onSearchInput);
  bindNavTabs();
  bindTimeTabs();
  bindErrorTabs();
  buildCategoryTabs();
  status("事件已绑定 ✓", "var(--c-success)");

  try {
    await bridge.ready();
    status("连接到 Bridge ✓", "var(--c-success)");
    document.title = "Overstats 监控面板";
    bridge.onContext(handleContextChange);
    await refreshAll();
    connectSSE();
    setInterval(refreshAll, 30000);
    status("", ""); // 隐藏状态
  } catch (e) {
    console.error("[Monitor] Init failed:", e.message || e);
    status("Bridge 连接失败，请检查后端", "var(--c-error)");
  }
}

function bindTimeTabs() {
  document.querySelectorAll("#time-tabs .timetab").forEach(btn => {
    btn.addEventListener("click", () => setTimeRange(btn.dataset.range));
  });
}

function bindErrorTabs() {
  document.querySelectorAll("#err-level-tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => setErrorLevel(btn.dataset.level));
  });
}

function handleContextChange() {
  const ctx = bridge.getContext?.();
  if (ctx) document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
}

// ══════════ Time Range ══════════

function setTimeRange(range) {
  console.log("[Monitor] setTimeRange:", range);
  selectedTimeRange = range;
  document.querySelectorAll("#time-tabs .timetab").forEach(t =>
    t.classList.toggle("active", t.dataset.range === range)
  );
  // 临时状态
  const st = document.getElementById("init-status");
  if (st) st.textContent = `加载趋势: ${range}...`;
  fetchTrend().then(() => { renderTrend(); if (st) st.textContent = ""; });
}

// ══════════ Trend Node Click → 筛选时段分布 ══════════

function selectTrendDate(date) {
  console.log("[Monitor] selectTrendDate:", date);
  selectedTrendDate = date;
  const st = document.getElementById("init-status");
  if (st) st.textContent = `加载时段: ${date}...`;
  fetchHourly(date).then(() => { renderHourly(); if (st) st.textContent = ""; });
}

function resetTrendDate() {
  selectedTrendDate = "";
  document.getElementById("hourly-title").innerHTML = '时段分布';
  fetchHourly().then(renderHourly);
}

// ══════════ Command List Collapse ══════════

function toggleCmdList() {
  cmdListExpanded = !cmdListExpanded;
  const list = document.getElementById("cmd-list");
  const btn = document.getElementById("btn-expand-cmds");
  list.classList.toggle("expanded", cmdListExpanded);
  btn.textContent = cmdListExpanded ? "收起" : "展开全部";
  console.log("[Monitor] cmdListExpanded:", cmdListExpanded);
}

// ══════════ Data Fetching ══════════

async function refreshAll() {
  await Promise.all([
    fetchOverview(),
    fetchCommands(),
    fetchTrend(),
    fetchHourly(selectedTrendDate),
    fetchBackendPerf(),
    fetchUpstream(),
    fetchErrors(),
    fetchShiqu(),
  ]);
  renderAll();
}

async function fetchOverview() {
  try { overviewData = await bridge.apiGet("monitor/overview"); }
  catch (e) { console.error("overview:", e.message); }
}

async function fetchCommands() {
  try {
    const params = {};
    if (selectedCategory) params.category = selectedCategory;
    const q = document.getElementById("cmd-search")?.value?.trim();
    if (q) params.search = q;
    console.log("[Monitor] fetchCommands:", params);
    commandsData = await bridge.apiGet("monitor/commands", params);
    console.log("[Monitor] commandsData received:", commandsData?.length, "items, first:", commandsData?.[0]?.cmd_name);
  } catch (e) { console.error("commands FAILED:", e.message || e); }
}

async function fetchTrend() {
  try {
    const days = TRANGE_DAYS[selectedTimeRange] || 7;
    console.log("[Monitor] fetchTrend days:", days);
    trendData = await bridge.apiGet("monitor/trend", { days });
    console.log("[Monitor] trendData:", trendData?.length, "items");
  } catch (e) { console.error("trend:", e.message); }
}

async function fetchHourly(date) {
  try {
    const params = {};
    if (date) params.date = date;
    console.log("[Monitor] fetchHourly:", params);
    hourlyData = await bridge.apiGet("monitor/hourly", params);
    console.log("[Monitor] hourlyData:", hourlyData?.length, "items");
  } catch (e) { console.error("hourly:", e.message); }
}

async function fetchBackendPerf() {
  try {
    const resp = await bridge.apiGet("monitor/backend/perf", { hours: 72 });
    backendPerf = resp.perf || [];
    const info = resp.db_info;
    const slow = resp.slow || [];
    const el = document.getElementById("perf-db-info");
    if (info && el) {
      el.textContent = ` (${info.total_rows?.toLocaleString() || 0} 条, ${info.time_range_start?.slice(0,10) || "?"} ~ ${info.time_range_end?.slice(0,10) || "?"})`;
    } else if (resp.expected_path && el) {
      el.textContent = ` (预期路径: ${resp.expected_path})`;
    }
    renderSlowEndpoints(slow);
    renderBackendPerfList();
  } catch (e) { console.error("backend/perf:", e.message); }
}

async function fetchUpstream() {
  try {
    const resp = await bridge.apiGet("monitor/backend/upstream", { limit: 15 });
    console.log("[Monitor] upstream resp:", JSON.stringify(resp).slice(0, 300));
    upstreamData = resp.items || [];
    upstreamMeta = resp;
    renderUpstream();
  } catch (e) { console.error("upstream:", e.message); }
}

let upstreamMeta = {};

async function fetchErrors() {
  try {
    const resp = await bridge.apiGet("monitor/errors", { limit: 50, level: selectedErrorLevel });
    renderErrors(resp.rows || []);
  } catch (e) { console.error("errors:", e.message); }
}

// ══════════ 是区吗调用日志 ══════════

let shiquCurrentPage = 0;        // 当前页码 (0-based)
const SHIQU_PAGE_SIZE = 20;

async function fetchShiqu(page) {
  if (page === undefined) page = shiquCurrentPage;
  try {
    const params = { limit: SHIQU_PAGE_SIZE, offset: page * SHIQU_PAGE_SIZE };
    const search = document.getElementById("shiqu-search")?.value?.trim();
    const success = document.getElementById("shiqu-success-filter")?.value;
    if (search) params.search = search;
    if (success !== "") params.success = success;
    console.log("[Monitor] fetchShiqu:", params);
    shiquData = await bridge.apiGet("monitor/shiqu/calls", params);
    shiquCurrentPage = page;
    console.log("[Monitor] shiquData:", shiquData?.summary, "records:", shiquData?.records?.length);
  } catch (e) { console.error("shiqu/calls:", e.message); }
}

function shiquSearch() {
  fetchShiqu(0).then(renderShiqu);
}

function shiquReset() {
  const el1 = document.getElementById("shiqu-search");
  const el2 = document.getElementById("shiqu-success-filter");
  if (el1) el1.value = "";
  if (el2) el2.value = "";
  fetchShiqu(0).then(renderShiqu);
}

function renderShiqu() {
  // 汇总卡片
  const grid = document.getElementById("shiqu-summary-grid");
  const s = shiquData.summary || {};
  const avail = shiquData.available !== false;
  if (!avail) {
    grid.innerHTML = `<div class="feature-card metric-card" style="grid-column:1/-1"><span class="metric-label">日志不可用</span><span class="metric-value" style="font-size:16px">shiqu_calls.log 未找到</span></div>`;
  } else {
    const total = s.total ?? 0;
    const rate = ((s.success_rate || 0) * 100).toFixed(1);
    const avgMs = s.avg_duration_ms ? (s.avg_duration_ms / 1000).toFixed(1) + "s" : "--";
    const avgScore = s.avg_score ? s.avg_score.toFixed(0) : "--";
    grid.innerHTML = `
      <div class="feature-card metric-card">
        <span class="metric-label">总调用次数</span>
        <span class="metric-value">${total.toLocaleString()}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">成功率</span>
        <span class="metric-value" style="color:${rate > 90 ? 'var(--c-success)' : 'var(--c-warning)'}">${rate}%</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">平均耗时</span>
        <span class="metric-value">${avgMs}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">平均评分</span>
        <span class="metric-value">${avgScore}</span>
      </div>`;
  }

  // 表格
  const tbody = document.getElementById("shiqu-tbody");
  const records = shiquData.records || [];
  if (!records.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="shiqu-loading">暂无调用记录</td></tr>`;
    closeShiquDetail();
  } else {
    tbody.innerHTML = records.map((r, i) => {
      const ts = r.ts ? new Date(r.ts).toLocaleString() : "--";
      const dur = r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : "--";
      const durCls = r.duration_ms > 120000 ? "shiqu-slow" : "";
      const okBadge = r.success
        ? `<span class="badge badge-success">成功</span>`
        : `<span class="badge badge-error">失败</span>`;
      const attemptBadge = r.attempt > 1 ? `<span style="font-size:10px;color:var(--c-warning);margin-left:4px">×${r.attempt}</span>` : "";
      const globalIdx = shiquCurrentPage * SHIQU_PAGE_SIZE + i;
      const selected = shiquSelectedIdx === globalIdx ? " selected" : "";

      return `<tr class="shiqu-row${selected}" data-index="${globalIdx}">
        <td class="shiqu-ts">${ts}</td>
        <td class="shiqu-target" title="${esc(r.target_id||'')}">${esc((r.target_id||'--').length > 16 ? r.target_id.slice(0,16)+'…' : (r.target_id||'--'))}</td>
        <td class="shiqu-dur ${durCls}">${dur}${attemptBadge}</td>
        <td style="white-space:nowrap">${okBadge}</td>
        <td><button class="btn btn-compact btn-shiqu-detail" data-idx="${globalIdx}">详情</button></td>
      </tr>`;
    }).join("");
  }

  // 绑定行点击 + 详情按钮
  document.querySelectorAll(".shiqu-row").forEach(row => {
    row.addEventListener("click", function (e) {
      if (e.target.tagName === "BUTTON") return; // 按钮有自己的事件
      const idx = parseInt(this.dataset.index);
      openShiquDetail(idx);
    });
  });
  document.querySelectorAll(".btn-shiqu-detail").forEach(btn => {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      openShiquDetail(parseInt(this.dataset.idx));
    });
  });

  // 分页
  renderShiquPagination();

  // 分页后重新恢复选中行的高亮
  if (shiquSelectedIdx >= 0) {
    const row = document.querySelector(`.shiqu-row[data-index="${shiquSelectedIdx}"]`);
    if (row) row.classList.add("selected");
  }
}

function openShiquDetail(idx) {
  shiquSelectedIdx = idx;
  // 高亮行
  document.querySelectorAll(".shiqu-row").forEach(r => r.classList.remove("selected"));
  const row = document.querySelector(`.shiqu-row[data-index="${idx}"]`);
  if (row) row.classList.add("selected");

  // 查找记录
  const localIdx = idx - shiquCurrentPage * SHIQU_PAGE_SIZE;
  const record = (shiquData.records || [])[localIdx];
  if (!record) { closeShiquDetail(); return; }

  const body = document.getElementById("shiqu-side-body");
  if (!body) return;

  const dur = record.duration_ms ? (record.duration_ms / 1000).toFixed(1) + "s" : "--";
  body.innerHTML = `
    <div class="sr-gentime">${record.ts ? new Date(record.ts).toLocaleString() : "--"} · ${esc(record.target_id||"--")} · 耗时 ${dur} · 字符 ${(record.llm_chars||0).toLocaleString()}</div>
    ${renderShiquHTML(record)}
  `;
}

function closeShiquDetail() {
  shiquSelectedIdx = -1;
  document.querySelectorAll(".shiqu-row").forEach(r => r.classList.remove("selected"));
  const body = document.getElementById("shiqu-side-body");
  if (body) body.innerHTML = `<p class="shiqu-side-placeholder">← 点击左侧表格中的「详情」查看 LLM 完整分析</p>`;
}

function renderShiquPagination() {
  const el = document.getElementById("shiqu-pagination");
  const total = shiquData.total || 0;
  const totalPages = Math.ceil(total / SHIQU_PAGE_SIZE);
  if (totalPages <= 1) { el.innerHTML = ""; return; }

  let html = `<span class="shiqu-page-info">共 ${total} 条，第 ${shiquCurrentPage + 1}/${totalPages} 页</span>`;
  html += `<button class="btn btn-compact" id="btn-shiqu-prev" ${shiquCurrentPage <= 0 ? 'disabled' : ''}>上一页</button>`;
  html += `<button class="btn btn-compact" id="btn-shiqu-next" ${shiquCurrentPage >= totalPages - 1 ? 'disabled' : ''}>下一页</button>`;
  el.innerHTML = html;

  const prevBtn = document.getElementById("btn-shiqu-prev");
  const nextBtn = document.getElementById("btn-shiqu-next");
  if (prevBtn) prevBtn.addEventListener("click", () => { if (shiquCurrentPage > 0) fetchShiqu(shiquCurrentPage - 1).then(renderShiqu); });
  if (nextBtn) nextBtn.addEventListener("click", () => { if (shiquCurrentPage < totalPages - 1) fetchShiqu(shiquCurrentPage + 1).then(renderShiqu); });
}

// ══════════ 页面切换 ══════════

function bindNavTabs() {
  document.querySelectorAll(".nav-tab").forEach(tab => {
    tab.addEventListener("click", () => switchPage(tab.dataset.page));
  });
}

function switchPage(page) {
  document.querySelectorAll(".nav-tab").forEach(t => t.classList.toggle("active", t.dataset.page === page));
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + page));
}

// ══════════ 是区吗 LLM 响应 → HTML 渲染 ══════════

const _SHIQU_SCORE_RULES = [
  { min: 83, cls: "god",     label: "你是职业吗？",       emoji: "😱" },
  { min: 75, cls: "boom",    label: "来了，暴力炸！",     emoji: "🤤" },
  { min: 68, cls: "butterfly", label: "化蛹成蝶（？）",   emoji: "🦋" },
  { min: 60, cls: "ok",      label: "恭喜，你不是区！",   emoji: "😂" },
  { min: 52, cls: "mid",     label: "不幸，你可能是区？", emoji: "🤔" },
  { min: 43, cls: "bad",     label: "哦灭跌多，你就是区！", emoji: "🎉" },
  { min: 0,  cls: "terrible", label: "你个大区！！！",     emoji: "😡" },
];

function _shiquScoreRule(score) {
  for (const r of _SHIQU_SCORE_RULES) { if (score >= r.min) return r; }
  return _SHIQU_SCORE_RULES[_SHIQU_SCORE_RULES.length - 1];
}

function _sqEsc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const _SHIQU_RESULT_CLASSES = { "胜": "sr-result-win", "负": "sr-result-loss", "平": "sr-result-draw" };

function renderShiquHTML(record) {
  const resp = record.llm_response;
  if (!resp) return `<div class="sr-empty">(无 LLM 响应内容)</div>`;
  if (!record.success) {
    return `<div class="sr-disclaimer">调用失败</div><pre class="sr-overall" style="white-space:pre-wrap">${_sqEsc(resp.slice(0, 2000))}</pre>`;
  }

  let data;
  try { data = JSON.parse(resp); } catch (e) { return `<pre class="sr-overall" style="white-space:pre-wrap">${_sqEsc(resp)}</pre>`; }
  if (!data || typeof data !== "object") return `<pre class="sr-overall" style="white-space:pre-wrap">${_sqEsc(resp)}</pre>`;

  const score = (typeof data.score === "number") ? Math.round(data.score) : (record.score || 0);
  const rule = _shiquScoreRule(score);
  const scoreColor = { god: "#e67e22", boom: "#ff6b6b", butterfly: "#a78bfa", ok: "#4ecdc4", mid: "#f9ca24", bad: "#e17055", terrible: "#d63031" }[rule.cls] || "#ffd700";

  const parts = [];

  // 评分
  parts.push(`<div class="sr-score" style="color:${scoreColor}">${score}/100</div>`);
  parts.push(`<div class="sr-verdict ${rule.cls}">${rule.emoji} ${rule.label}</div>`);

  // 数据概况
  if (data.summary) {
    parts.push(`<div class="sr-subsection">数据概况</div>`);
    parts.push(`<p class="sr-summary">${_sqEsc(String(data.summary))}</p>`);
  }

  // 逐局点评
  if (Array.isArray(data.match_comments) && data.match_comments.length) {
    parts.push(`<div class="sr-section">逐局点评</div>`);
    for (const m of data.match_comments) {
      const idx = m.index || "?";
      const result = String(m.result || "未知");
      const hero = String(m.hero || "未知英雄");
      const comment = String(m.comment || "");
      const rCls = _SHIQU_RESULT_CLASSES[result] || "sr-result-unknown";
      parts.push(
        `<p class="sr-game"><b>第${idx}局: </b>` +
        `<span class="${rCls}">${_sqEsc(result)}</span>` +
        `<span class="sr-sep"> </span>` +
        `<span class="sr-hero">${_sqEsc(hero)}</span>` +
        `<span class="sr-sep">: </span>` +
        `<span>${_sqEsc(comment)}</span></p>`
      );
    }
  }

  // 综合评价
  if (data.overall_comment) {
    parts.push(`<div class="sr-section">综合评价</div>`);
    parts.push(`<p class="sr-overall">${_sqEsc(String(data.overall_comment))}</p>`);
  }

  // 队友点评
  if (Array.isArray(data.teammate_comments) && data.teammate_comments.length) {
    parts.push(`<div class="sr-section">队友点评</div>`);
    for (const t of data.teammate_comments) {
      const name = String(t.name || "未知队友");
      const tScore = (typeof t.score === "number") ? Math.round(t.score) : 0;
      const tRule = _shiquScoreRule(tScore);
      const tMateColor = { god: "#f6a863", boom: "#f58989", butterfly: "#bfacfa", ok: "#7eddd6", mid: "#f5d35a", bad: "#f09c88", terrible: "#ee6566" }[tRule.cls] || "#b0a080";
      const games = t.games ? `（共同${t.games}局）` : "";
      const comment = String(t.comment || "");
      parts.push(
        `<div class="sr-mate-entry">` +
        `<p class="sr-mate-head">` +
        `<span class="sr-mate-name">${_sqEsc(name)}</span>` +
        `<span class="sr-mate-games">${games}</span>` +
        `<span class="sr-sep">: </span>评分 ` +
        `<span class="sr-mate-score" style="color:${tMateColor}">${tScore}/100</span>` +
        `</p>` +
        `<p class="sr-mate-comment"><span class="sr-iv ${tRule.cls}">${tRule.emoji} ${tRule.label}</span> ${_sqEsc(comment)}</p>` +
        `</div>`
      );
    }
  }

  return parts.join("\n");
}

// ══════════ SSE ══════════

async function connectSSE() {
  if (sseSubId) return;
  try {
    sseSubId = await bridge.subscribeSSE("monitor/errors/stream", {
      onOpen() { showLiveBadge(true); },
      onMessage(event) {
        if (event.parsed && !event.parsed._heartbeat) {
          prependError(event.parsed);
          showLiveBadge(true);
        }
      },
      onError() { showLiveBadge(false); },
    });
  } catch (e) { console.error("SSE:", e.message); }
}

function showLiveBadge(show) {
  const el = document.getElementById("badge-live");
  if (el) el.style.display = show ? "inline-flex" : "none";
}

// ══════════ Render All ══════════

function renderAll() {
  renderOverview();
  renderCommands();
  renderTrend();
  renderHourly();
  renderBackendPerfList();
  renderUpstream();
  renderShiqu();
  renderDeployAndRL();
}

// ── 运行总览 ──

function renderOverview() {
  if (!overviewData) return;
  // 防御：API 返回错误信息
  if (overviewData.error) {
    setEl("m-uptime", "--");
    setEl("m-total-cmds", "--");
    setEl("m-success-rate", "--");
    setEl("m-api-total", "--");
    setEl("nav-uptime", "X");
    document.getElementById("status-dot").className = "status-dot warn";
    const st = document.getElementById("init-status");
    if (st) { st.textContent = `监控未就绪: ${overviewData.error}`; st.style.color = "var(--c-warning)"; }
    return;
  }
  const fmt = (v) => typeof v === "number" ? v.toLocaleString() : v;
  const uptimeH = ((overviewData.uptime_seconds || 0) / 3600).toFixed(1);
  setEl("m-uptime", `${uptimeH}h`);
  setEl("m-total-cmds", fmt(overviewData.cmd_total || 0));
  setEl("m-success-rate", `${((overviewData.cmd_success_rate || 0) * 100).toFixed(1)}%`);
  setEl("m-api-total", fmt(overviewData.api_total || 0));
  setEl("nav-uptime", `运行: ${uptimeH}h`);

  const dot = document.getElementById("status-dot");
  dot.className = "status-dot";
  // 绿色：插件运行正常（manual 模式不追踪进程）；红色：后端报错；黄色：未知
  if (overviewData.deploy?.last_error) dot.classList.add("err");
  else if (overviewData.deploy?.mode === "manual" || overviewData.deploy?.process_alive) dot.classList.add("ok");
  else dot.classList.add("warn");
}

// ── 指令列表（compact，可折叠）──

function renderCommands() {
  console.log("[Monitor] renderCommands called, data length:", commandsData?.length);
  const list = document.getElementById("cmd-list");
  if (!commandsData || !commandsData.length) {
    list.innerHTML = `<p class="muted">暂无指令使用数据</p>`;
    return;
  }
  const sorted = [...commandsData].sort((a, b) => b.total - a.total);
  const maxTotal = sorted[0]?.total || 1;
  list.innerHTML = sorted.map(c => {
    const rate = c.total > 0 ? c.success / c.total : 0;
    const rateCls = rate >= 0.95 ? "good" : rate >= 0.80 ? "warn" : "bad";
    const rateText = (rate * 100).toFixed(1) + "%";
    return `<div class="cmd-row-slim">
      <span class="cmd-name" title="${esc(c.cmd_name)}">${esc(c.cmd_name)}</span>
      <div class="cmd-bar-mini"><div class="cmd-bar-mini-fill" style="width:${(c.total / maxTotal * 100).toFixed(0)}%"></div></div>
      <span class="cmd-count">${c.total.toLocaleString()}</span>
      <span class="cmd-rate ${rateCls}">${rateText}</span>
    </div>`;
  }).join("");

  // 保持展开/折叠状态
  if (cmdListExpanded) list.classList.add("expanded");
  else list.classList.remove("expanded");
  document.getElementById("btn-expand-cmds").textContent = cmdListExpanded ? "收起" : "展开全部";

  // 更新分类指示器
  const st = document.getElementById("init-status");
  if (st) st.textContent = selectedCategory ? `已筛选: ${selectedCategory} (${sorted.length}条)` : `全部指令 (${sorted.length}条)`;
}

function onSearchInput() {
  // 搜索时自动展开
  if (!cmdListExpanded) toggleCmdList();
  fetchCommands().then(renderCommands);
}

// ── SVG 折线图 (调用趋势) ──

function renderTrend() {
  console.log("[Monitor] renderTrend called, data length:", trendData?.length);
  const wrap = document.getElementById("trend-chart");

  // 空数据
  if (!trendData || !trendData.length) {
    wrap.innerHTML = `<p class="muted" style="padding:24px">暂无趋势数据</p>`;
    return;
  }

  // 确保 svg 元素存在（之前空数据时可能被替换）
  let svgEl = document.getElementById("trend-svg");
  if (!svgEl || svgEl.tagName !== "svg") {
    wrap.innerHTML = `<svg class="line-chart-svg" viewBox="0 0 600 200" preserveAspectRatio="xMidYMid meet" id="trend-svg"></svg>`;
    svgEl = document.getElementById("trend-svg");
  }

  // 按日期聚合
  const byDate = {};
  for (const r of trendData) {
    if (!byDate[r.date]) byDate[r.date] = 0;
    byDate[r.date] += r.count;
  }
  const dates = Object.keys(byDate).sort();
  if (dates.length === 0) {
    svgEl.innerHTML = `<text x="300" y="100" text-anchor="middle" fill="var(--c-muted)" font-family="var(--ff-body)" font-size="13">暂无趋势数据</text>`;
    return;
  }

  const values = dates.map(d => byDate[d]);
  const maxVal = Math.max(1, ...values);

  // SVG 布局
  const W = 600, H = 200, padL = 44, padR = 16, padT = 14, padB = 30;
  const pw = W - padL - padR, ph = H - padT - padB;

  const xFor = (i) => padL + (dates.length === 1 ? pw / 2 : (i / (dates.length - 1)) * pw);
  const yFor = (v) => padT + ph - (v / maxVal) * ph;

  // Grid lines
  const gridCount = 4;
  let gridLines = "";
  for (let i = 0; i <= gridCount; i++) {
    const y = padT + (i / gridCount) * ph;
    const val = Math.round(maxVal * (1 - i / gridCount));
    gridLines += `<line class="grid-line" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>`;
    gridLines += `<text class="grid-label" x="${padL - 6}" y="${y + 3}" text-anchor="end">${val}</text>`;
  }

  // Polyline
  const pointsArr = dates.map((d, i) => `${xFor(i)},${yFor(byDate[d])}`);
  const polyPoints = pointsArr.join(" ");
  const fillPoints = `${xFor(0)},${padT + ph} ${polyPoints} ${xFor(dates.length - 1)},${padT + ph}`;

  // 数据点 + 日期标签
  let dots = "";
  let dateLabels = "";
  const labelInterval = dates.length > 10 ? Math.ceil(dates.length / 8) : 1;
  for (let i = 0; i < dates.length; i++) {
    const cx = xFor(i), cy = yFor(byDate[dates[i]]);
    const isSelected = dates[i] === selectedTrendDate;
    const dotClass = isSelected ? "data-dot selected" : "data-dot";
    dots += `<circle class="${dotClass}" cx="${cx}" cy="${cy}" r="${isSelected ? 5 : 3.5}"
              data-date="${dates[i]}" data-count="${byDate[dates[i]]}" style="cursor:pointer">
              <title>${dates[i]}: ${byDate[dates[i]]} 次 — 点击筛选时段分布</title></circle>`;
    if (i % labelInterval === 0 || i === dates.length - 1) {
      dateLabels += `<text class="date-label" x="${cx}" y="${H - 6}">${dates[i].slice(5)}</text>`;
    }
  }

  const gradientDef = `<defs>
    <linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--c-primary)" stop-opacity="0.4"/>
      <stop offset="100%" stop-color="var(--c-primary)" stop-opacity="0.02"/>
    </linearGradient>
  </defs>`;

  const axisLine = `<line class="axis-line" x1="${padL}" y1="${padT + ph}" x2="${W - padR}" y2="${padT + ph}"/>`;

  svgEl.innerHTML = gradientDef + gridLines + axisLine +
    `<polygon class="data-fill" points="${fillPoints}"/>` +
    `<polyline class="data-line" points="${polyPoints}"/>` +
    dots + dateLabels;

  // 事件委托：点击数据点 → 筛选时段分布
  svgEl.onclick = function(e) {
    const circle = e.target.closest("circle[data-date]");
    if (circle) {
      selectTrendDate(circle.dataset.date);
    }
  };
}

// ── 时段分布 ──

function renderHourly() {
  const el = document.getElementById("hourly-chart");
  const titleEl = document.getElementById("hourly-title");

  // 标题：显示筛选日期
  if (titleEl) {
    if (selectedTrendDate) {
      titleEl.innerHTML = `时段分布 <span class="badge badge-coral" style="font-size:10px;margin-left:6px">${selectedTrendDate}</span>
        <button class="btn btn-compact" id="btn-reset-hourly" style="margin-left:8px">全部时间</button>`;
      // 绑定事件
      setTimeout(() => {
        const rb = document.getElementById("btn-reset-hourly");
        if (rb) rb.addEventListener("click", resetTrendDate);
      }, 0);
    } else {
      titleEl.innerHTML = '时段分布';
    }
  }

  if (!hourlyData || !hourlyData.length) {
    el.innerHTML = `<p class="muted">暂无时段数据</p>`;
    return;
  }
  const maxVal = Math.max(1, ...hourlyData.map(r => r.count));
  el.innerHTML = hourlyData.map(r => {
    const h = Math.max(4, (r.count / maxVal * 100).toFixed(0));
    return `<div class="bar-col">
      <span class="bar-value">${r.count}</span>
      <div class="bar" style="height:${h}px"></div>
      <span class="bar-label">${r.hour}时</span>
    </div>`;
  }).join("");
}

// ── 后端 API 性能 ──

function renderBackendPerfList() {
  const el = document.getElementById("backend-perf-list");
  if (!backendPerf.length) {
    el.innerHTML = `<p class="on-dark-soft">request_metrics.sqlite3 未找到 — 请确保 Overstats 后端已启动并处理过请求</p><span id="perf-db-info" class="caption"></span>`;
    return;
  }
  const maxAvg = Math.max(1, ...backendPerf.map(p => p.avg_ms || 0));
  el.innerHTML = backendPerf.map(p => {
    const slow = (p.avg_ms || 0) > 10000 ? "perf-slow" : "";
    const w = ((p.avg_ms || 0) / maxAvg * 100).toFixed(0);
    const color = (p.avg_ms || 0) > 10000 ? "var(--c-error)" : "var(--c-accent-teal)";
    return `<div class="perf-row ${slow}">
      <span class="perf-endpoint">${esc(p.url.replace(/^\/api\/v2\//, "/"))}</span>
      <div class="perf-bar-wrap"><div class="perf-bar-fill" style="width:${w}%;background:${color}"></div></div>
      <span class="perf-meta">
        <span style="color:var(--c-on-dark)">${(p.avg_ms||0).toLocaleString()}ms</span>
        <span class="badge badge-pill" style="font-size:11px">${((p.success_rate||0)*100).toFixed(1)}%</span>
        <span style="color:var(--c-on-dark-soft)">队列:${(p.avg_queue_ms||0).toLocaleString()}ms</span>
        <span style="color:var(--c-on-dark-soft)">${p.count?.toLocaleString()||0}次</span>
      </span>
    </div>`;
  }).join("");
}

function renderSlowEndpoints(slow) {
  const el = document.getElementById("slow-endpoints");
  if (!slow.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<h4 style="color:var(--c-warning);margin-bottom:6px;font-size:13px">慢端点 (平均 &gt; 10s)</h4>` +
    slow.map(s => `<div class="perf-row perf-slow">
      <span class="perf-endpoint">${esc(s.url.replace(/^\/api\/v2\//, "/"))}</span>
      <span class="perf-meta">
        <span style="color:var(--c-error)">均 ${(s.avg_ms||0).toLocaleString()}ms</span>
        <span style="color:var(--c-on-dark-soft)">最 ${(s.max_ms||0).toLocaleString()}ms</span>
        <span class="badge badge-warning" style="font-size:10px">${((s.success_rate||0)*100).toFixed(1)}%</span>
      </span>
    </div>`).join("");
}

// ── 上游 API 统计 ──

function renderUpstream() {
  const el = document.getElementById("upstream-list");
  console.log("[Monitor] renderUpstream: dataLen=" + upstreamData.length + ", hasTable=" + upstreamMeta.table_exists + ", available=" + upstreamMeta.available);
  if (!upstreamData.length) {
    const msg = upstreamMeta.table_exists === false
      ? "request_url_stats 表不存在 — 请升级 Overstats 后端到最新版本"
      : "request_url_stats 表为空 — 后端处理请求后自动填充";
    el.innerHTML = `<p class="muted">${msg} (dataLen=${upstreamData.length}, table=${upstreamMeta.table_exists})</p>`;
    return;
  }
  const maxTotal = Math.max(1, ...upstreamData.map(u => u.total_requests || 0));
  el.innerHTML = upstreamData.map(u => {
    const w = ((u.total_requests || 0) / maxTotal * 100).toFixed(0);
    const rate = u.success_rate != null ? (u.success_rate * 100).toFixed(1) : "?";
    const badgeCls = u.success_rate >= 0.99 ? "badge-success" : "badge-warning";
    const parts = (u.url || "").replace(/^https?:\/\/[^/]+/, "").split("/").filter(Boolean);
    const urlShort = parts.length > 2 ? ".../" + parts.slice(-2).join("/") : parts.join("/");
    return `<div class="cmd-row">
      <div class="cmd-row-left"><div class="cmd-name" style="font-size:12px">${esc(urlShort)}</div></div>
      <div class="cmd-row-mid"><div class="cmd-bar-wrap"><div class="cmd-bar-fill" style="width:${w}%"></div></div></div>
      <div class="cmd-row-right">
        <span class="cmd-count" style="font-size:13px">${(u.total_requests||0).toLocaleString()}</span>
        <span class="badge ${badgeCls}">${rate}%</span>
        <span style="color:var(--c-success);font-size:11px">\u2713${(u.successful_requests||0).toLocaleString()}</span>
        <span style="color:var(--c-error);font-size:11px">\u2717${(u.failed_requests||0).toLocaleString()}</span>
      </div>
    </div>`;
  }).join("");
}

// ── 部署与限流 ──

function renderDeployAndRL() {
  if (!overviewData) return;
  const dep = overviewData.deploy || {};
  const rlConf = overviewData.rate_limit_config || {};
  const rlStats = overviewData.rate_limit || {};
  const cmdRL = rlStats["cmd"] || { total: 0, today: 0 };
  const llmRL = rlStats["llm"] || { total: 0, today: 0 };

  const stateMap = { "running": "运行中", "stopped": "已停止", "idle": "空闲", "deploying": "部署中", "failed": "失败" };
  const modeMap = { "auto": "自动", "manual": "手动" };

  document.getElementById("deploy-details").innerHTML = [
    ["部署模式", modeMap[dep.mode] || dep.mode || "?"],
    ["运行状态", stateMap[dep.state] || dep.state || "?"],
    ["后端进程", dep.process_alive ? "存活" : "已终止"],
    ["进程 PID", dep.pid || "--"],
    ["监听端口", dep.backend_port || "--"],
    ["Git 提交", (dep.git_commit || "").slice(0, 8) || "--"],
    ["最近部署", dep.last_deploy_time ? new Date(dep.last_deploy_time * 1000).toLocaleString() : "--"],
    ["最近错误", dep.last_error || "无"],
  ].map(([k, v]) => `<div class="detail-row"><span class="key">${k}</span><span class="val">${esc(String(v))}</span></div>`).join("");

  document.getElementById("rate-limit-details").innerHTML = [
    ["指令限流", rlConf.cmd_enabled ? "开启" : "关闭"],
    ["最大并发", rlConf.cmd_max || 3],
    ["指令拒绝 (累计)", cmdRL.total || 0],
    ["指令拒绝 (今日)", cmdRL.today || 0],
    ["LLM 限流", rlConf.llm_enabled ? "开启" : "关闭"],
    ["每分钟上限", rlConf.llm_per_minute || 10],
    ["LLM 拒绝 (累计)", llmRL.total || 0],
    ["LLM 拒绝 (今日)", llmRL.today || 0],
  ].map(([k, v]) => `<div class="detail-row"><span class="key">${k}</span><span class="val">${esc(String(v))}</span></div>`).join("");
}

// ── 错误日志 ──

function renderErrors(rows) {
  const el = document.getElementById("error-log");
  if (!rows || !rows.length) { el.innerHTML = `<p class="code-line on-dark-soft">暂无错误记录。</p>`; return; }
  el.innerHTML = rows.map(r => {
    const ts = r.recorded_at ? new Date(r.recorded_at + "Z").toLocaleString() : "--";
    const lvl = (r.level || "").toUpperCase();
    const lvlCls = lvl === "ERROR" ? "lvl-error" : "lvl-warning";
    const cmd = r.command ? `<span class="cmd-tag">[${esc(r.command)}]</span>` : "";
    const msg = esc((r.message || "").slice(0, 300));
    return `<div class="code-line"><span class="ts">${ts}</span><span class="lvl ${lvlCls}">${lvl}</span>${cmd}<span class="msg">${msg}</span></div>`;
  }).join("");
}

function prependError(event) {
  const el = document.getElementById("error-log");
  const ts = event.recorded_at ? new Date(event.recorded_at + "Z").toLocaleString() : "--";
  const lvl = (event.level || "").toUpperCase();
  const lvlCls = lvl === "ERROR" ? "lvl-error" : "lvl-warning";
  const cmd = event.command ? `<span class="cmd-tag">[${esc(event.command)}]</span>` : "";
  const msg = esc((event.message || "").slice(0, 300));
  const line = `<div class="code-line" style="animation:fadeIn 300ms ease"><span class="ts">${ts}</span><span class="lvl ${lvlCls}">${lvl}</span>${cmd}<span class="msg">${msg}</span></div>`;
  el.insertAdjacentHTML("afterbegin", line);
  while (el.children.length > 200) el.lastChild.remove();
}

// ══════════ Category Tabs ══════════

function buildCategoryTabs() {
  const el = document.getElementById("category-tabs");
  const allCats = [["", "全部"], ...CATEGORIES.map(c => [c, c])];
  el.innerHTML = allCats.map(([val, label], i) =>
    `<button class="tab${i === 0 ? ' active' : ''}" data-cat="${val}">${esc(label)}</button>`
  ).join("");
  el.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => selectCategory(btn.dataset.cat));
  });
}

function selectCategory(cat) {
  console.log("[Monitor] selectCategory:", cat);
  selectedCategory = cat;
  document.querySelectorAll("#category-tabs .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.cat === cat)
  );
  // 不再重置折叠状态，保持用户的展开/折叠偏好
  fetchCommands().then(renderCommands);
}

// ══════════ Error Level ══════════

function setErrorLevel(level) {
  selectedErrorLevel = level;
  document.querySelectorAll("#err-level-tabs .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.level === level)
  );
  fetchErrors();
}

function clearErrors() {
  document.getElementById("error-log").innerHTML = `<p class="code-line on-dark-soft">视图已清空。（数据仍保留在数据库）</p>`;
}

async function clearAllStats() {
  if (!confirm("确定清空全部统计数据？包括指令调用记录、API 请求、错误日志、限流记录。此操作不可撤销。")) return;
  try {
    const res = await bridge.apiPost("monitor/clear");
    console.log("[Monitor] clearAllStats:", res);
    const st = document.getElementById("init-status");
    if (st) st.textContent = `已清空 ${res.deleted} 条记录`;
    setTimeout(() => refreshAll(), 500);
  } catch (e) {
    console.error("clearAllStats failed:", e.message);
    alert("清空失败: " + (e.message || "未知错误"));
  }
}

// ══════════ Utils ══════════

function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function setEl(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }

// ══════════ Start ══════════
console.log("[Monitor] Starting...");
init();
