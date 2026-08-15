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
let customStart = null;      // 自定义开始 (ISO, 无时区后缀)
let customEnd = null;        // 自定义结束
let customDays = 7;          // 自定义区间对应天数（供趋势图）
let selectedErrorLevel = "";
let selectedTrendDate = new Date().toISOString().slice(0, 10); // 默认最新24小时
let cmdListExpanded = false;
let sseSubId = null;

// 是区吗调用日志状态
let shiquData = { records: [], summary: {}, total: 0, offset: 0, limit: 30 };
let shiquSelectedIdx = -1; // 当前选中行（侧边栏显示详情）
let shiquCurrentMode = "";  // "analysis" | "prompt"
let shiquDetailReqToken = 0; // 丢弃过期异步详情响应，避免竞态覆盖
const shiquRecordById = new Map(); // id(Number) -> record

// 开庭调用日志状态
let courtData = { records: [], summary: {}, total: 0, offset: 0, limit: 30 };
let courtSelectedIdx = -1; // 当前选中行（侧边栏显示详情）
let courtCurrentMode = "";  // "verdict" | "prompt"
let courtDetailReqToken = 0; // 丢弃过期异步详情响应，避免竞态覆盖
const courtRecordById = new Map(); // id(Number) -> record
let courtCopyCache = "";

// ── Category list ──
const CATEGORIES = ["基础绑定", "数据查询", "总结", "图表排行", "游戏资讯", "AI开庭", "管理部署"];

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
  safeBind("btn-apply-range", "click", applyCustomRange);
  safeBind("btn-clear-errors", "click", clearErrors);
  safeBind("btn-clear-stats", "click", clearAllStats);
  safeBind("btn-shiqu-search", "click", shiquSearch);
  safeBind("btn-shiqu-reset", "click", shiquReset);
  safeBind("shiqu-side-close", "click", closeShiquDetail);
  safeBind("shiqu-side-copy", "click", copyShiquPrompt);
  safeBind("btn-court-search", "click", courtSearch);
  safeBind("btn-court-reset", "click", courtReset);
  safeBind("court-side-close", "click", closeCourtDetail);
  safeBind("court-side-copy", "click", copyCourtPrompt);
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

// ══════════ Time Range（运行总览 + 指令分析 共用）══════════

// ISO 字符串（UTC，无时区后缀）——与后端 datetime.utcnow().isoformat() 存储格式一致，
// 保证字符串比较正确（避免 Z 后缀导致排序错位）。
function _fmt2(n) { return String(n).padStart(2, "0"); }
function isoUTC(d) {
  return d.getUTCFullYear() + "-" + _fmt2(d.getUTCMonth() + 1) + "-" + _fmt2(d.getUTCDate()) +
    "T" + _fmt2(d.getUTCHours()) + ":" + _fmt2(d.getUTCMinutes()) + ":" + _fmt2(d.getUTCSeconds()) +
    "." + String(d.getUTCMilliseconds()).padStart(3, "0") + "000";
}
function startOfWeekUTC(now) {
  const day = now.getUTCDay();               // 0=Sun..6=Sat
  const diff = day === 0 ? 6 : day - 1;      // 距本周一的天数
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - diff, 0, 0, 0, 0));
}
function startOfMonthUTC(now) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1, 0, 0, 0, 0));
}

// 根据当前选择的时间范围返回 {start, end, days}
// start/end 为 ISO 字符串（无时区后缀），null 表示不限（全部）。
function getRangeParams() {
  const now = new Date();
  let start = null, end = null, days = 7;
  if (selectedTimeRange === "24h") {
    start = isoUTC(new Date(now.getTime() - 86400000)); end = isoUTC(now); days = 1;
  } else if (selectedTimeRange === "3d") {
    start = isoUTC(new Date(now.getTime() - 3 * 86400000)); end = isoUTC(now); days = 3;
  } else if (selectedTimeRange === "week") {
    start = isoUTC(startOfWeekUTC(now)); end = isoUTC(now); days = 7;
  } else if (selectedTimeRange === "month") {
    start = isoUTC(startOfMonthUTC(now)); end = isoUTC(now); days = 30;
  } else if (selectedTimeRange === "custom") {
    start = customStart; end = customEnd; days = customDays;
  } else { // all
    start = null; end = null; days = 90;
  }
  return { start, end, days };
}

function setTimeRange(range) {
  console.log("[Monitor] setTimeRange:", range);
  selectedTimeRange = range;
  document.querySelectorAll("#time-tabs .timetab").forEach(t =>
    t.classList.toggle("active", t.dataset.range === range)
  );
  applyTimeRange();
}

function applyCustomRange() {
  const s = document.getElementById("range-start")?.value;
  const e = document.getElementById("range-end")?.value;
  if (!s || !e) { alert("请选择开始和结束日期"); return; }
  if (new Date(s) > new Date(e)) { alert("开始日期不能晚于结束日期"); return; }
  customStart = s + "T00:00:00";
  customEnd = e + "T23:59:59.999999";
  customDays = Math.max(1, Math.min(90, Math.round((new Date(e) - new Date(s)) / 86400000) + 1));
  selectedTimeRange = "custom";
  document.querySelectorAll("#time-tabs .timetab").forEach(t => t.classList.remove("active"));
  applyTimeRange();
}

// 时间范围变更后：重新拉取 总览 + 指令统计 + 趋势 并渲染
async function applyTimeRange() {
  const st = document.getElementById("init-status");
  if (st) st.textContent = "加载时间范围数据...";
  await Promise.all([fetchOverview(), fetchCommands(), fetchTrend()]);
  renderOverview();
  renderCommands();
  renderTrend();
  updateRangeSummary();
  if (st) st.textContent = "";
}

function updateRangeSummary() {
  const el = document.getElementById("range-summary");
  if (!el) return;
  const { start, end } = getRangeParams();
  let text;
  if (selectedTimeRange === "week") text = "本周";
  else if (selectedTimeRange === "24h") text = "近 24 小时";
  else if (selectedTimeRange === "3d") text = "近 3 天";
  else if (selectedTimeRange === "month") text = "本月";
  else if (selectedTimeRange === "all") text = "全部时间";
  else text = `${customStart?.slice(0, 10)} ~ ${customEnd?.slice(0, 10)}`;
  if (start && end) text += `（${start.slice(0, 10)} ~ ${end.slice(0, 10)}）`;
  el.textContent = text;
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
    fetchCourt(),
  ]);
  renderAll();
}

async function fetchOverview() {
  try {
    const { start, end } = getRangeParams();
    const params = {};
    if (start) params.start = start;
    if (end) params.end = end;
    overviewData = await bridge.apiGet("monitor/overview", params);
  }
  catch (e) { console.error("overview:", e.message); }
}

async function fetchCommands() {
  try {
    const params = {};
    if (selectedCategory) params.category = selectedCategory;
    const q = document.getElementById("cmd-search")?.value?.trim();
    if (q) params.search = q;
    const { start, end } = getRangeParams();
    if (start) params.start = start;
    if (end) params.end = end;
    console.log("[Monitor] fetchCommands:", params);
    commandsData = await bridge.apiGet("monitor/commands", params);
    console.log("[Monitor] commandsData received:", commandsData?.length, "items, first:", commandsData?.[0]?.cmd_name);
  } catch (e) { console.error("commands FAILED:", e.message || e); }
}

async function fetchTrend() {
  try {
    const { days } = getRangeParams();
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
    grid.innerHTML = `<div class="feature-card metric-card" style="grid-column:1/-1"><span class="metric-label">日志不可用</span><span class="metric-value" style="font-size:16px">shiqu_llm.sqlite3 未找到（后端未启用数据库写入？）</span></div>`;
  } else {
    const total = s.total ?? 0;
    const success = s.success ?? 0;
    const rate = total ? ((success / total) * 100).toFixed(1) : "0.0";
    const avgMs = s.avg_duration_ms ? (s.avg_duration_ms / 1000).toFixed(1) + "s" : "--";
    grid.innerHTML = `
      <div class="feature-card metric-card">
        <span class="metric-label">总调用次数</span>
        <span class="metric-value">${total.toLocaleString()}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">成功次数</span>
        <span class="metric-value" style="color:${rate > 90 ? 'var(--c-success)' : 'var(--c-warning)'}">${success.toLocaleString()}（${rate}%）</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">平均耗时</span>
        <span class="metric-value">${avgMs}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">失败次数</span>
        <span class="metric-value">${(s.failed ?? 0).toLocaleString()}</span>
      </div>`;
  }

  // 表格
  const tbody = document.getElementById("shiqu-tbody");
  const records = shiquData.records || [];
  shiquRecordById.clear();
  if (!records.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="shiqu-loading">暂无调用记录</td></tr>`;
    closeShiquDetail();
  } else {
    tbody.innerHTML = records.map((r) => {
      shiquRecordById.set(r.id, r);
      const ts = r.created_at ? new Date(r.created_at * 1000).toLocaleString() : "--";
      const dur = r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : "--";
      const durCls = r.duration_ms > 120000 ? "shiqu-slow" : "";
      const okBadge = r.ok
        ? `<span class="badge badge-success">成功</span>`
        : `<span class="badge badge-error">失败</span>`;
      const callBadge = (r.call_count > 1) ? `<span style="font-size:10px;color:var(--c-warning);margin-left:4px">×${r.call_count}</span>` : "";

      return `<tr class="shiqu-row" data-id="${r.id}">
        <td class="shiqu-ts">${ts}</td>
        <td class="shiqu-target" title="${esc(r.target_id||'')}">${esc((r.target_id||'--').length > 16 ? r.target_id.slice(0,16)+'…' : (r.target_id||'--'))}</td>
        <td class="shiqu-dur ${durCls}">${dur}</td>
        <td class="shiqu-call">
          <button class="shiqu-call-count" data-id="${r.id}" title="点击查看本次提示词">${r.call_count ?? 1}</button>${callBadge}
        </td>
        <td style="white-space:nowrap">${okBadge}</td>
      </tr>`;
    }).join("");

    // 绑定行点击 → LLM 分析详情（按需拉取单条大字段）
    tbody.querySelectorAll(".shiqu-row").forEach(row => {
      row.addEventListener("click", () => openShiquDetail(Number(row.dataset.id), "analysis"));
    });
    // 绑定调用次数按钮点击 → 提示词（阻止冒泡，避免同时触发行点击）
    tbody.querySelectorAll(".shiqu-call-count").forEach(btn => {
      if (btn.classList.contains("disabled")) return;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        openShiquDetail(Number(btn.dataset.id), "prompt");
      });
    });

    // 重绘后恢复已选中行高亮（30s 轮询会重绘表格，避免高亮丢失）
    if (shiquSelectedIdx > 0) {
      const sel = tbody.querySelector(`.shiqu-row[data-id="${shiquSelectedIdx}"]`);
      if (sel) sel.classList.add("selected");
    }
  }

  // 分页
  renderShiquPagination();
}

function closeShiquDetail() {
  shiquSelectedIdx = -1;
  shiquCurrentMode = "";
  shiquDetailReqToken++; // 取消可能在途的详情请求，避免关闭后内容被回填
  document.querySelectorAll(".shiqu-row").forEach(r => r.classList.remove("selected"));
  const body = document.getElementById("shiqu-side-body");
  if (body) body.innerHTML = `<p class="shiqu-side-placeholder">← 点击左侧表格行查看 <b>LLM 分析详情</b><br>点击「状态」列下的数字查看 <b>本次提示词</b></p>`;
  const titleEl = document.getElementById("shiqu-side-title");
  if (titleEl) titleEl.textContent = "LLM 分析详情";
  const modeEl = document.getElementById("shiqu-side-mode");
  if (modeEl) modeEl.textContent = "";
  const copyBtn = document.getElementById("shiqu-side-copy");
  if (copyBtn) copyBtn.style.display = "none";
}

// ── 打开详情面板（按需拉取单条大字段，mode: "analysis" 显示 LLM 分析；"prompt" 显示提示词）──
async function openShiquDetail(id, mode) {
  shiquSelectedIdx = id;
  shiquCurrentMode = mode;
  const body = document.getElementById("shiqu-side-body");
  const titleEl = document.getElementById("shiqu-side-title");
  const modeEl = document.getElementById("shiqu-side-mode");
  const copyBtn = document.getElementById("shiqu-side-copy");

  // 高亮选中行
  document.querySelectorAll(".shiqu-row").forEach(r =>
    r.classList.toggle("selected", Number(r.dataset.id) === id)
  );

  // 轻量记录即可提供标题
  const lean = shiquRecordById.get(id);
  if (titleEl) titleEl.textContent = "是区吗 · " + esc((lean && lean.target_id) || "未知玩家");
  if (modeEl) modeEl.textContent = mode === "prompt" ? "提示词" : "LLM 分析详情";
  if (copyBtn) copyBtn.style.display = "none";
  if (body) body.innerHTML = `<p class="shiqu-side-placeholder">加载中…</p>`;

  const myToken = ++shiquDetailReqToken; // 标记本次请求，丢弃后到的过期响应
  try {
    const resp = await bridge.apiGet("monitor/shiqu/detail", { id });
    if (myToken !== shiquDetailReqToken) return; // 已有更新的点击，丢弃本次结果
    const rec = resp && resp.record;
    if (!rec) {
      if (body) body.innerHTML = `<p class="sr-empty">未找到该条记录（数据库可能已清理）。</p>`;
      return;
    }
    if (titleEl) titleEl.textContent = "是区吗 · " + esc(rec.target_id || "未知玩家");

    if (mode === "prompt") {
      shiquCopyCache = rec.prompt || "";
      if (copyBtn) copyBtn.style.display = "";
      if (body) body.innerHTML = renderShiquPromptHTML(rec.prompt);
    } else {
      let parsed = null;
      if (rec.raw_response) {
        try { parsed = typeof rec.raw_response === "string" ? JSON.parse(rec.raw_response) : rec.raw_response; }
        catch (e) { parsed = null; }
      }
      if (parsed) {
        if (body) body.innerHTML = renderShiquAnalysisHTML(parsed);
      } else {
        const diag = (!rec.ok)
          ? "本次调用未成功（LLM 未返回有效结果）。"
          : "数据库中未保存有效的 LLM 分析内容。";
        if (body) body.innerHTML = `<p class="sr-empty">${esc(diag)}</p>` +
          (rec.prompt ? `<div class="sr-section">本次提示词（供参考）</div><div class="sr-prompt-wrap"><pre class="sr-prompt">${esc(rec.prompt)}</pre></div>` : "");
      }
    }
  } catch (e) {
    if (body) body.innerHTML = `<p class="sr-error">加载失败：${esc(e.message || e)}</p>`;
  }
  const panel = document.getElementById("shiqu-side-panel");
  if (panel) panel.scrollTop = 0;
}

let shiquCopyCache = "";

// ── 渲染：本次提示词 ──
function renderShiquPromptHTML(prompt) {
  if (!prompt) return `<p class="sr-empty">本次调用没有记录到提示词。</p>`;
  return `<div class="sr-prompt-wrap"><pre class="sr-prompt">${esc(prompt)}</pre></div>`;
}

// ── 渲染：LLM 分析详情（对应 shiqu.py 模板的 .sr-* 结构）──
function _srScoreClass(score) {
  score = Number(score) || 0;
  if (score >= 83) return "god";
  if (score >= 75) return "boom";
  if (score >= 68) return "butterfly";
  if (score >= 60) return "ok";
  if (score >= 52) return "mid";
  if (score >= 43) return "bad";
  return "terrible";
}
const _SR_VERDICT_CLASS = {
  "你是职业吗？": "god", "来了，暴力炸！": "boom", "化蛹成蝶（？）": "butterfly",
  "恭喜，你不是区！": "ok", "不幸，你可能是区？": "mid", "哦灭跌多，你就是区！": "bad",
  "你个大区！！！": "terrible",
};
const _SR_VERDICT_EMOJI = {
  "你是职业吗？": "😱", "来了，暴力炸！": "🤤", "化蛹成蝶（？）": "🦋",
  "恭喜，你不是区！": "😂", "不幸，你可能是区？": "🤔", "哦灭跌多，你就是区！": "🎉", "你个大区！！！": "😡",
};
function _srVerdictClass(label) { return _SR_VERDICT_CLASS[(label || "").trim()] || "terrible"; }
function _srVerdictEmoji(label) { return _SR_VERDICT_EMOJI[(label || "").trim()] || ""; }
function _srResultClass(result) {
  return { "胜": "win", "负": "loss", "平": "draw" }[result] || "unknown";
}

// verdict 由 score 推导（与后端 _score_rule 一致）；raw_response 通常不含该字段，故按阈值还原。
const _SR_SCORE_VERDICT = [
  { min: 83, label: "你是职业吗？", emoji: "😱", cls: "god" },
  { min: 75, label: "来了，暴力炸！", emoji: "🤤", cls: "boom" },
  { min: 68, label: "化蛹成蝶（？）", emoji: "🦋", cls: "butterfly" },
  { min: 60, label: "恭喜，你不是区！", emoji: "😂", cls: "ok" },
  { min: 52, label: "不幸，你可能是区？", emoji: "🤔", cls: "mid" },
  { min: 43, label: "哦灭跌多，你就是区！", emoji: "🎉", cls: "bad" },
  { min: 0,  label: "你个大区！！！", emoji: "😡", cls: "terrible" },
];
function _srVerdictFromScore(score) {
  for (const r of _SR_SCORE_VERDICT) if (score >= r.min) return r;
  return _SR_SCORE_VERDICT[_SR_SCORE_VERDICT.length - 1];
}

function renderShiquAnalysisHTML(d) {
  if (!d || typeof d !== "object") return `<p class="sr-empty">无 LLM 分析结果。</p>`;
  const score = Number(d.score) || 0;
  const scoreCls = _srScoreClass(score);
  let verdict = d.verdict;
  let verdictCls, verdictEmoji;
  if (verdict) {
    verdictCls = _srVerdictClass(verdict);
    verdictEmoji = _srVerdictEmoji(verdict);
  } else {
    const vr = _srVerdictFromScore(score);
    verdict = vr.label; verdictCls = vr.cls; verdictEmoji = vr.emoji;
  }

  let html = `<div class="sr-score ${scoreCls}">${score}</div>`;
  html += `<div class="sr-verdict ${verdictCls}">${verdictEmoji} ${esc(verdict)}</div>`;

  html += `<div class="sr-section">数据概况</div>`;
  html += `<div class="sr-summary">${esc(d.summary || "暂无数据概况。")}</div>`;

  const matches = Array.isArray(d.match_comments) ? d.match_comments : [];
  if (matches.length) {
    html += `<div class="sr-section">对局点评</div>`;
    for (const m of matches) {
      const res = m.result || "未知";
      html += `<div class="sr-game">`;
      html += `<b>第 ${m.index != null ? m.index : "?"} 局</b> · ${esc(m.mode || "")} · `;
      html += `<span class="sr-result-${_srResultClass(res)}">${esc(res)}</span> · 英雄：${esc(m.heroes || "")}<br>`;
      html += `${esc(m.comment || "")}</div>`;
    }
  }

  html += `<div class="sr-section">综合评价</div>`;
  html += `<div class="sr-overall">${esc(d.overall_comment || "暂无综合评价。")}</div>`;

  const mates = Array.isArray(d.teammate_comments) ? d.teammate_comments : [];
  if (mates.length) {
    html += `<div class="sr-section">队友点评</div>`;
    for (const t of mates) {
      const tcls = _srScoreClass(t.score);
      html += `<div class="sr-mate-entry">`;
      html += `<div class="sr-mate-head"><span class="sr-mate-name">${esc(t.name || "?")}</span>`;
      html += ` <span class="sr-mate-games">共 ${t.games != null ? t.games : "?"} 局</span>`;
      html += ` <span class="sr-iv ${tcls} sr-mate-score">${t.score != null ? t.score : "?"}</span></div>`;
      html += `<p class="sr-mate-comment">${esc(t.comment || "暂无点评。")}</p></div>`;
    }
  }
  return html;
}

// ── 复制当前提示词 ──
function copyShiquPrompt() {
  if (!shiquCopyCache) return;
  const text = shiquCopyCache;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      const btn = document.getElementById("shiqu-side-copy");
      if (btn) { const old = btn.textContent; btn.textContent = "已复制"; setTimeout(() => btn.textContent = old, 1200); }
    }).catch(() => {});
  }
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

// ══════════ 开庭调用日志（复刻是区吗调用日志）══════════

let courtCurrentPage = 0;        // 当前页码 (0-based)
const COURT_PAGE_SIZE = 20;

async function fetchCourt(page) {
  if (page === undefined) page = courtCurrentPage;
  try {
    const params = { limit: COURT_PAGE_SIZE, offset: page * COURT_PAGE_SIZE };
    const search = document.getElementById("court-search")?.value?.trim();
    const success = document.getElementById("court-success-filter")?.value;
    if (search) params.search = search;
    if (success !== "") params.success = success;
    console.log("[Monitor] fetchCourt:", params);
    courtData = await bridge.apiGet("monitor/court/calls", params);
    courtCurrentPage = page;
    console.log("[Monitor] courtData:", courtData?.summary, "records:", courtData?.records?.length);
  } catch (e) { console.error("court/calls:", e.message); }
}

function courtSearch() {
  fetchCourt(0).then(renderCourt);
}

function courtReset() {
  const el1 = document.getElementById("court-search");
  const el2 = document.getElementById("court-success-filter");
  if (el1) el1.value = "";
  if (el2) el2.value = "";
  fetchCourt(0).then(renderCourt);
}

function renderCourt() {
  // 汇总卡片
  const grid = document.getElementById("court-summary-grid");
  const s = courtData.summary || {};
  const avail = courtData.available !== false;
  if (!avail) {
    grid.innerHTML = `<div class="feature-card metric-card" style="grid-column:1/-1"><span class="metric-label">日志不可用</span><span class="metric-value" style="font-size:16px">shiqu_llm.sqlite3 未找到（后端未启用数据库写入？）</span></div>`;
  } else {
    const total = s.total ?? 0;
    const success = s.success ?? 0;
    const rate = total ? ((success / total) * 100).toFixed(1) : "0.0";
    const avgMs = s.avg_duration_ms ? (s.avg_duration_ms / 1000).toFixed(1) + "s" : "--";
    grid.innerHTML = `
      <div class="feature-card metric-card">
        <span class="metric-label">总调用次数</span>
        <span class="metric-value">${total.toLocaleString()}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">成功次数</span>
        <span class="metric-value" style="color:${rate > 90 ? 'var(--c-success)' : 'var(--c-warning)'}">${success.toLocaleString()}（${rate}%）</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">平均耗时</span>
        <span class="metric-value">${avgMs}</span>
      </div>
      <div class="feature-card metric-card">
        <span class="metric-label">失败次数</span>
        <span class="metric-value">${(s.failed ?? 0).toLocaleString()}</span>
      </div>`;
  }

  // 表格
  const tbody = document.getElementById("court-tbody");
  const records = courtData.records || [];
  courtRecordById.clear();
  if (!records.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="shiqu-loading">暂无调用记录</td></tr>`;
    closeCourtDetail();
  } else {
    tbody.innerHTML = records.map((r) => {
      courtRecordById.set(r.id, r);
      const ts = r.created_at ? new Date(r.created_at * 1000).toLocaleString() : "--";
      const dur = r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : "--";
      const durCls = r.duration_ms > 120000 ? "shiqu-slow" : "";
      const okBadge = r.ok
        ? `<span class="badge badge-success">成功</span>`
        : `<span class="badge badge-error">失败</span>`;
      const callBadge = (r.call_count > 1) ? `<span style="font-size:10px;color:var(--c-warning);margin-left:4px">×${r.call_count}</span>` : "";
      const idx = (r.match_index != null ? r.match_index : 0) + 1;
      const mapName = esc(r.map_name || "--");
      const gameMode = esc(r.game_mode || "--");

      return `<tr class="shiqu-row" data-id="${r.id}">
        <td class="shiqu-ts">${ts}</td>
        <td class="shiqu-target" title="${esc(r.target_id||'')}">${esc((r.target_id||'--').length > 16 ? r.target_id.slice(0,16)+'…' : (r.target_id||'--'))}</td>
        <td class="shiqu-target" title="${mapName}">${mapName}</td>
        <td class="shiqu-target" title="${gameMode}">${gameMode}</td>
        <td style="text-align:center;font-family:var(--ff-code);color:var(--c-body)">${idx}</td>
        <td class="shiqu-dur ${durCls}">${dur}</td>
        <td class="shiqu-call">
          <button class="shiqu-call-count" data-id="${r.id}" title="点击查看本次提示词">${r.call_count ?? 1}</button>${callBadge}
        </td>
        <td style="white-space:nowrap">${okBadge}</td>
      </tr>`;
    }).join("");

    // 绑定行点击 → 判决书详情（按需拉取单条大字段）
    tbody.querySelectorAll(".shiqu-row").forEach(row => {
      row.addEventListener("click", () => openCourtDetail(Number(row.dataset.id), "verdict"));
    });
    // 绑定调用次数按钮点击 → 提示词（阻止冒泡，避免同时触发行点击）
    tbody.querySelectorAll(".shiqu-call-count").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        openCourtDetail(Number(btn.dataset.id), "prompt");
      });
    });

    // 重绘后恢复已选中行高亮（30s 轮询会重绘表格，避免高亮丢失）
    if (courtSelectedIdx > 0) {
      const sel = tbody.querySelector(`.shiqu-row[data-id="${courtSelectedIdx}"]`);
      if (sel) sel.classList.add("selected");
    }
  }

  // 分页
  renderCourtPagination();
}

function closeCourtDetail() {
  courtSelectedIdx = -1;
  courtCurrentMode = "";
  courtDetailReqToken++; // 取消可能在途的详情请求，避免关闭后内容被回填
  document.querySelectorAll("#court-tbody .shiqu-row").forEach(r => r.classList.remove("selected"));
  const body = document.getElementById("court-side-body");
  if (body) body.innerHTML = `<p class="shiqu-side-placeholder">← 点击左侧表格行查看 <b>开庭判决书</b><br>点击「状态」列下的数字查看 <b>本次提示词</b></p>`;
  const titleEl = document.getElementById("court-side-title");
  if (titleEl) titleEl.textContent = "开庭判决书";
  const modeEl = document.getElementById("court-side-mode");
  if (modeEl) modeEl.textContent = "";
  const copyBtn = document.getElementById("court-side-copy");
  if (copyBtn) copyBtn.style.display = "none";
}

// ── 打开详情面板（按需拉取单条大字段，mode: "verdict" 显示判决书；"prompt" 显示提示词）──
async function openCourtDetail(id, mode) {
  courtSelectedIdx = id;
  courtCurrentMode = mode;
  const body = document.getElementById("court-side-body");
  const titleEl = document.getElementById("court-side-title");
  const modeEl = document.getElementById("court-side-mode");
  const copyBtn = document.getElementById("court-side-copy");

  // 高亮选中行
  document.querySelectorAll("#court-tbody .shiqu-row").forEach(r =>
    r.classList.toggle("selected", Number(r.dataset.id) === id)
  );

  // 轻量记录即可提供标题
  const lean = courtRecordById.get(id);
  if (titleEl) titleEl.textContent = "开庭 · " + esc((lean && lean.target_id) || "未知玩家");
  if (modeEl) modeEl.textContent = mode === "prompt" ? "提示词" : "判决书";
  if (copyBtn) copyBtn.style.display = "none";
  if (body) body.innerHTML = `<p class="shiqu-side-placeholder">加载中…</p>`;

  const myToken = ++courtDetailReqToken; // 标记本次请求，丢弃后到的过期响应
  try {
    const resp = await bridge.apiGet("monitor/court/detail", { id });
    if (myToken !== courtDetailReqToken) return; // 已有更新的点击，丢弃本次结果
    const rec = resp && resp.record;
    if (!rec) {
      if (body) body.innerHTML = `<p class="sr-empty">未找到该条记录（数据库可能已清理）。</p>`;
      return;
    }
    if (titleEl) titleEl.textContent = "开庭 · " + esc(rec.target_id || "未知玩家");

    if (mode === "prompt") {
      courtCopyCache = rec.prompt || "";
      if (copyBtn) copyBtn.style.display = "";
      if (body) body.innerHTML = renderCourtPromptHTML(rec.prompt);
    } else {
      if (body) body.innerHTML = renderCourtVerdictHTML(rec);
    }
  } catch (e) {
    if (body) body.innerHTML = `<p class="sr-error">加载失败：${esc(e.message || e)}</p>`;
  }
  const panel = document.getElementById("court-side-panel");
  if (panel) panel.scrollTop = 0;
}

// ── 渲染：本次提示词 ──
function renderCourtPromptHTML(prompt) {
  if (!prompt) return `<p class="sr-empty">本次调用没有记录到提示词。</p>`;
  return `<div class="sr-prompt-wrap"><pre class="sr-prompt">${esc(prompt)}</pre></div>`;
}

// ── 渲染：开庭判决书（纯文本，仿 court.py / render.py 排版）──
function _courtInline(text) {
  // **粗体** → 金色；其余转义后原样输出（esc 不影响 *，故 ** 得以保留）
  return esc(text).replace(/\*\*(.+?)\*\*/g, '<b class="cv-b">$1</b>');
}

function renderCourtVerdictHTML(rec) {
  if (!rec || typeof rec !== "object") return `<p class="sr-empty">无法显示判决书。</p>`;
  const raw = rec.raw_response || "";
  if (!raw.trim()) {
    const diag = (!rec.ok)
      ? "本次调用未成功（LLM 未返回有效判决书）。"
      : "数据库中未保存有效的判决书内容。";
    let html = `<p class="sr-empty">${esc(diag)}</p>`;
    if (rec.prompt) html += `<div class="sr-section">本次提示词（供参考）</div><div class="sr-prompt-wrap"><pre class="sr-prompt">${esc(rec.prompt)}</pre></div>`;
    return html;
  }

  // ── 解析 raw_response ──
  let obj = null;
  let isMultiField = false;
  const s = raw.trim();
  if (s.startsWith("{")) {
    try { obj = JSON.parse(s); } catch (_) { obj = null; }
    if (obj && typeof obj === "object" && obj.case_no && obj.location &&
        obj.mvp && obj.defendant && obj.focus_verdict &&
        Array.isArray(obj.team_verdicts) && obj.lane_analysis) {
      isMultiField = true;
    }
  }

  // 新版结构化 JSON
  if (isMultiField) return _renderCourtStructuredHTML(obj);

  // 旧版单字段 {"verdict":"..."} 或纯文本
  const text = (obj && typeof obj.verdict === "string" && obj.verdict.trim()) ? obj.verdict : raw;
  const idx = (rec.match_index ?? 0) + 1;
  const subParts = [rec.target_id || "未知玩家"].concat(
    rec.map_name ? [rec.map_name] : [],
    rec.game_mode ? [`（${rec.game_mode}）`] : []
  );
  let html = `<div class="cv-head"><div class="cv-title">电竞法庭 · 第 ${idx} 局判决</div><div class="cv-sub">${esc(subParts.join('  ·  '))}</div></div>`;
  for (const ln of text.split("\n")) {
    const s = ln.trim();
    if (!s) continue;
    const lm = s.match(/^([-*+]\s+|\d+[.)]\s+)(.*)$/);
    html += lm
      ? `<div class="cv-list">${_courtInline(lm[2])}</div>`
      : `<div class="cv-para">${_courtInline(s)}</div>`;
  }
  html += `<div class="cv-footer">* 功能仅限娱乐, 切勿因为ai瞎编影响心情</div>`;
  return html;
}

function _renderCourtStructuredHTML(d) {
  const mvp = d.mvp || {};
  const defendant = d.defendant || {};
  const focus = d.focus_verdict || {};
  const lanes = d.lane_analysis || {};
  const tvs = Array.isArray(d.team_verdicts) ? d.team_verdicts : [];
  const scoreClsMap = { S:"sky", A:"boom", B:"ok", C:"mid", D:"bad" };
  const scoreCls = scoreClsMap[focus.score] || "ok";

  function _$e(s) { return esc(String(s ?? "")); }

  let h = '<div class="cv-struct">';
  h += '<div class="cvs-title">⚖️ 电竞法庭判决书</div>';
  h += '<div class="cvs-meta">';
  h += '<span class="cvs-case">📋 案件编号：第 ' + _$e(d.case_no) + ' 局</span>';
  h += '<span class="cvs-location">🗺️ 案发地点：' + _$e(d.location) + '</span>';
  h += '</div>';

  // MVP
  h += '<div class="cvs-section"><div class="cvs-mvp"><span class="cvs-section-icon">🏆</span><span class="cvs-section-label">MVP（最佳表现者）</span></div>';
  h += '<div class="cvs-player">' + _$e(mvp.player) + '</div>';
  h += '<div class="cvs-reason">' + _$e(mvp.reason) + '</div></div>';

  // Defendant
  h += '<div class="cvs-section"><div class="cvs-defendant"><span class="cvs-section-icon">👎</span><span class="cvs-section-label">最差表现者（被告）</span></div>';
  h += '<div class="cvs-player">' + _$e(defendant.player) + '</div>';
  h += '<div class="cvs-charges"><span class="cvs-charges-label">原罪清单</span>：' + _$e(defendant.charges) + '</div></div>';

  // Focus verdict
  h += '<div class="cvs-section"><div class="cvs-focus"><span class="cvs-section-icon">⚡</span><span class="cvs-section-label">焦点玩家判决</span></div>';
  h += '<div class="cvs-player">' + _$e(focus.player) + ' <span class="cvs-score ' + scoreCls + '">' + _$e(focus.score) + '</span></div>';
  h += '<div class="cvs-reason">' + _$e(focus.reason) + '</div></div>';

  // Team verdicts
  if (tvs.length) {
    h += '<div class="cvs-section"><div class="cvs-team"><span class="cvs-section-icon">📊</span><span class="cvs-section-label">全队审判</span></div>';
    h += '<table class="cvs-team-table">';
    for (const tv of tvs) {
      h += '<tr><td class="cvs-tp">' + _$e(tv.player) + '</td><td class="cvs-tv">' + _$e(tv.verdict) + '</td></tr>';
    }
    h += '</table></div>';
  }

  // Lane analysis
  h += '<div class="cvs-section"><div class="cvs-lanes"><span class="cvs-section-icon">⚔️</span><span class="cvs-section-label">三路对位分析</span></div>';
  h += '<div class="cvs-lane"><span class="cvs-lane-label">坦克位</span>' + _$e(lanes.tank) + '</div>';
  h += '<div class="cvs-lane"><span class="cvs-lane-label">输出位</span>' + _$e(lanes.dps) + '</div>';
  h += '<div class="cvs-lane"><span class="cvs-lane-label">辅助位</span>' + _$e(lanes.healer) + '</div></div>';

  h += '<div class="cv-footer">* 功能仅限娱乐, 切勿因为ai瞎编影响心情</div>';
  h += '</div>';
  return h;
}

// ── 复制当前提示词 ──
function copyCourtPrompt() {
  if (!courtCopyCache) return;
  const text = courtCopyCache;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      const btn = document.getElementById("court-side-copy");
      if (btn) { const old = btn.textContent; btn.textContent = "已复制"; setTimeout(() => btn.textContent = old, 1200); }
    }).catch(() => {});
  }
}

function renderCourtPagination() {
  const el = document.getElementById("court-pagination");
  const total = courtData.total || 0;
  const totalPages = Math.ceil(total / COURT_PAGE_SIZE);
  if (totalPages <= 1) { el.innerHTML = ""; return; }

  let html = `<span class="shiqu-page-info">共 ${total} 条，第 ${courtCurrentPage + 1}/${totalPages} 页</span>`;
  html += `<button class="btn btn-compact" id="btn-court-prev" ${courtCurrentPage <= 0 ? 'disabled' : ''}>上一页</button>`;
  html += `<button class="btn btn-compact" id="btn-court-next" ${courtCurrentPage >= totalPages - 1 ? 'disabled' : ''}>下一页</button>`;
  el.innerHTML = html;

  const prevBtn = document.getElementById("btn-court-prev");
  const nextBtn = document.getElementById("btn-court-next");
  if (prevBtn) prevBtn.addEventListener("click", () => { if (courtCurrentPage > 0) fetchCourt(courtCurrentPage - 1).then(renderCourt); });
  if (nextBtn) nextBtn.addEventListener("click", () => { if (courtCurrentPage < totalPages - 1) fetchCourt(courtCurrentPage + 1).then(renderCourt); });
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
  renderCourt();
  renderDeployAndRL();
  updateRangeSummary();
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
    closeCmdFailures();
    return;
  }
  const sorted = [...commandsData].sort((a, b) => b.total - a.total);
  const maxTotal = sorted[0]?.total || 1;
  list.innerHTML = sorted.map(c => {
    // 有效成功率（soft errors 不计入分母）；为 null 时（全是 soft）显示 —
    const rate = (c.success_rate != null) ? c.success_rate : null;
    const rateCls = rate == null ? "muted" : rate >= 0.95 ? "good" : rate >= 0.80 ? "warn" : "bad";
    const rateText = rate == null ? "—" : (rate * 100).toFixed(1) + "%";
    const softHint = (c.soft > 0) ? `（含 ${c.soft} 次不计入成功率）` : "";
    const sel = (c.cmd_name === selectedCmd) ? " selected" : "";
    return `<div class="cmd-row-slim${sel}" data-cmd="${esc(c.cmd_name)}" style="cursor:pointer" title="点击查看失败原因${softHint}">
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

  // 绑定点击 → 失败原因下钻
  list.querySelectorAll(".cmd-row-slim").forEach(row => {
    row.addEventListener("click", () => selectCommand(row.dataset.cmd));
  });

  // 更新分类指示器
  const st = document.getElementById("init-status");
  if (st) st.textContent = selectedCategory ? `已筛选: ${selectedCategory} (${sorted.length}条)` : `全部指令 (${sorted.length}条)`;
}

// ── 指令失败原因下钻 ──

let selectedCmd = "";

function selectCommand(cmd) {
  if (selectedCmd === cmd) { closeCmdFailures(); return; } // 再次点击收起
  selectedCmd = cmd;
  document.querySelectorAll(".cmd-row-slim").forEach(r =>
    r.classList.toggle("selected", r.dataset.cmd === cmd)
  );
  fetchCmdFailures(cmd);
}

async function fetchCmdFailures(cmd) {
  const panel = document.getElementById("cmd-failures-panel");
  panel.style.display = "block";
  panel.innerHTML = `<div class="cf-header">指令「${esc(cmd)}」失败原因</div><div class="cf-empty">加载中...</div>`;
  const { start, end } = getRangeParams();
  const params = { cmd };
  if (start) params.start = start;
  if (end) params.end = end;
  try {
    const resp = await bridge.apiGet("monitor/commands/failures", params);
    renderCmdFailures(cmd, resp.reasons || []);
  } catch (e) {
    console.error("cmd failures:", e.message);
    panel.innerHTML = `<div class="cf-header">指令「${esc(cmd)}」失败原因</div><div class="cf-empty">加载失败：${esc(e.message || e)}</div>`;
  }
}

function renderCmdFailures(cmd, reasons) {
  const panel = document.getElementById("cmd-failures-panel");
  if (!reasons.length) {
    panel.innerHTML = `<div class="cf-header">指令「${esc(cmd)}」失败原因</div><div class="cf-empty">该指令在当前时间范围内没有记录到失败原因</div>`;
    return;
  }
  const max = Math.max(1, ...reasons.map(r => r.count));
  const rows = reasons.map(r => {
    const pct = (r.count / max * 100).toFixed(0);
    const softBadge = r.is_soft
      ? `<span class="badge badge-soft" title="属于正常业务结果，不计入成功率">不计入成功率</span>`
      : "";
    return `<div class="cf-reason">
      <span class="cf-code">${esc(r.label)}</span>
      <div class="cf-bar-wrap"><div class="cf-bar-fill ${r.is_soft ? "soft" : ""}" style="width:${pct}%"></div></div>
      <span class="cf-count">${r.count} 次</span>
      ${softBadge}
    </div>`;
  }).join("");
  panel.innerHTML = `<div class="cf-header">
      指令「${esc(cmd)}」失败原因分布
      <button class="btn btn-compact" id="cf-close">关闭</button>
    </div>${rows}`;
  document.getElementById("cf-close").addEventListener("click", closeCmdFailures);
}

function closeCmdFailures() {
  selectedCmd = "";
  const panel = document.getElementById("cmd-failures-panel");
  if (panel) panel.style.display = "none";
  document.querySelectorAll(".cmd-row-slim").forEach(r => r.classList.remove("selected"));
}

function onSearchInput() {
  closeCmdFailures();
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
    ["单用户并发上限", rlConf.cmd_per_user_max ?? 3],
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
  closeCmdFailures();
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
