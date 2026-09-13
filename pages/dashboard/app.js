/* 折淘客返利助手 · 插件页面逻辑（通过 AstrBotPluginPage bridge 调用插件后端 API） */
const bridge = window.AstrBotPluginPage;
const $ = (s) => document.querySelector(s);

let page = 1;
const pageSize = 20;

function toast(m) {
  const t = $("#toast");
  t.textContent = m;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}
function money(v) { return "¥" + Number(v || 0).toFixed(2); }

function statusTag(r) {
  const map = { "1": ["paid", "已付款"], "8": ["ok", "已完成"], "9": ["refund", "已退款/风控"] };
  const [cls, txt] = map[r.status] || ["paid", r.status_text || r.status || "—"];
  return `<span class="tag ${cls}">${txt}</span>`;
}
function pfTag(p) { return `<span class="pf ${p}">${p}</span>`; }

function renderTheme(ctx) {
  document.documentElement.dataset.theme = ctx?.isDark ? "dark" : "light";
}

async function loadStats() {
  try {
    const s = await bridge.apiGet("stats");
    $("#statCards").innerHTML = `
      <div class="card blue"><div class="k">订单总数</div><div class="v">${s.cnt}</div></div>
      <div class="card orange"><div class="k">佣金合计(预估)</div><div class="v">${money(s.profit_total)}</div></div>
      <div class="card green"><div class="k">已结算佣金</div><div class="v">${money(s.profit_settled)}</div></div>
      <div class="card orange"><div class="k">今日订单</div><div class="v">${s.cnt_today}<small> 笔 / ${money(s.profit_today)}</small></div></div>
      <div class="card red"><div class="k">退款/风控订单</div><div class="v">${s.cnt_refund}</div></div>`;
    $("#pfRows").innerHTML = (s.by_platform || []).map((r) =>
      `<tr><td>${pfTag(r.platform)}</td><td>${r.cnt}</td><td class="money">${money(r.profit)}</td></tr>`).join("")
      || '<tr><td colspan="3" class="empty">暂无数据，等待订单同步…</td></tr>';
    const sel = $("#fPlatform");
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部平台</option>'
      + (s.by_platform || []).map((r) => `<option>${r.platform}</option>`).join("");
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    $("#syncTime").textContent = "数据更新于 " + new Date().toLocaleTimeString();
  } catch (e) { toast("加载统计失败: " + e.message); }
}

function orderParams() {
  const p = {};
  if ($("#fPlatform").value) p.platform = $("#fPlatform").value;
  if ($("#fStatus").value) p.status = $("#fStatus").value;
  if ($("#fStart").value) p.start = $("#fStart").value + " 00:00:00";
  if ($("#fEnd").value) p.end = $("#fEnd").value + " 23:59:59";
  if ($("#fQ").value) p.q = $("#fQ").value;
  return p;
}

async function loadOrders(p) {
  page = Math.max(1, p || 1);
  try {
    const d = await bridge.apiGet("orders", { ...orderParams(), page, page_size: pageSize });
    const rows = d.rows || [];
    $("#orderRows").innerHTML = rows.map((r) => `
      <tr><td>${pfTag(r.platform)}</td><td>${r.orderid}</td><td title="${r.title}">${r.title || "—"}</td>
      <td>${money(r.pay_price)}</td><td class="money">${money(r.profit)}</td>
      <td class="money" style="color:var(--green)">${money(r.rebate)}</td>
      <td>${statusTag(r)}</td>
      <td>${r.settled ? `<span class="tag ok">已结算 ${money(r.settle_profit)}</span>` : '<span class="muted">未结算</span>'}</td>
      <td class="muted">${r.pay_time || "—"}</td><td class="muted">${r.customer_id || "—"}</td></tr>`).join("")
      || '<tr><td colspan="10" class="empty">没有符合条件的订单</td></tr>';
    const totalPage = Math.max(1, Math.ceil(d.total / pageSize));
    $("#pageInfo").textContent = `共 ${d.total} 条 / 第 ${page} 页（返利比例 ${Math.round(d.rebate_rate * 100)}%）`;
    $("#prevBtn").disabled = page <= 1;
    $("#nextBtn").disabled = page >= totalPage;
  } catch (e) { toast("加载订单失败: " + e.message); }
}

async function loadBindings() {
  try {
    const d = await bridge.apiGet("bindings");
    $("#rateTxt1").textContent = Math.round(d.rebate_rate * 100) + "%";
    const rows = d.rows || [];
    $("#bindRows").innerHTML = rows.map((r) => `
      <tr><td><b>${r.bind_id}</b></td><td class="muted">${r.created_at || "—"}</td>
      <td>${r.cnt}</td><td class="money">${money(r.total)}</td>
      <td>${money(r.settled)}</td><td class="money" style="color:var(--green)">${money(r.rebate)}</td></tr>`).join("")
      || '<tr><td colspan="6" class="empty">暂无绑定用户</td></tr>';
    $("#bindEmpty").style.display = rows.length ? "none" : "block";
  } catch (e) { toast("加载绑定失败: " + e.message); }
}

async function doSync() {
  const b = $("#syncBtn");
  b.disabled = true; b.textContent = "同步中…";
  try {
    const d = await bridge.apiGet("sync");
    if (d.ok) {
      const t = Object.entries(d.result || {}).map(([k, v]) => `${k}+${v}`).join("，") || "无新增";
      toast("同步完成：" + t);
      loadStats();
      if (document.querySelector("#p2.active")) loadOrders(page);
    } else toast(d.error || "同步失败");
  } catch (e) { toast("同步请求失败: " + e.message); }
  b.disabled = false; b.textContent = "⟳ 立即同步订单";
}

async function convert() {
  const params = { platform: $("#cPlatform").value };
  if ($("#cBind").value) params.bind_id = $("#cBind").value;
  $("#cResult").textContent = "请求中…";
  try {
    const d = await bridge.apiGet("convert", params);
    $("#cResult").textContent = JSON.stringify(d, null, 2);
  } catch (e) { $("#cResult").textContent = "失败: " + e.message; }
}

async function convert2() {
  const content = $("#cContent").value.trim();
  if (!content) { toast("请输入商品链接或口令"); return; }
  const params = { platform: $("#cPlatform2").value, content };
  if ($("#cBind2").value) params.bind_id = $("#cBind2").value;
  $("#cResult2").textContent = "请求中…";
  try {
    const d = await bridge.apiGet("convert", params);
    $("#cResult2").textContent = JSON.stringify(d, null, 2);
  } catch (e) { $("#cResult2").textContent = "失败: " + e.message; }
}

/* ---------- 定时推送 ---------- */
const KIND_NAME = { meituan: "美团红包", eleme: "闪购红包", orders: "订单日报" };
let pushTasks = {};
let platformMap = {};  // 平台实例ID → 显示名+机器人账号，用于任务列表展示
let editingPushId = null;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadPlatforms() {
  const sel = $("#pushPlatform");
  platformMap = {};
  try {
    const d = await bridge.apiGet("push/platforms");
    sel.innerHTML = (d.platforms || []).map((p) => {
      const name = p.name || p.display_name || p.id;  // 面板里设置的实例名称
      const st = p.status && p.status !== "running" ? ` · ${p.status}` : "";
      const pro = p.proactive === false ? " · 不支持主动发" : "";
      const bot = p.bot_id ? ` · 机器人:${p.bot_name}(${p.bot_id})` : "";
      platformMap[p.id] = {
        label: p.bot_id ? `${name} · ${p.bot_name}(${p.bot_id})` : name,
      };
      return `<option value="${esc(p.id)}">${esc(name)}（${esc(p.type)}${bot}${st}${pro}）</option>`;
    }).join("") || '<option value="">未检测到已启用的平台</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">平台列表加载失败</option>';
    toast("加载平台失败: " + e.message);
  }
  loadTargets();
  if (Object.keys(pushTasks).length) renderPushRows();
}

async function loadTargets() {
  const pid = $("#pushPlatform").value;
  const tt = $("#pushTargetType").value;
  const dl = $("#pushTargetList");
  dl.innerHTML = "";
  if (!pid) return;
  try {
    const d = await bridge.apiGet("push/targets", { platform_id: pid, target_type: tt });
    if (d.error) { toast(d.error); return; }
    dl.innerHTML = (d.targets || []).map((t) =>
      `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  } catch (e) { toast("读取会话列表失败: " + e.message); }
}

async function loadPushTasks() {
  try {
    const d = await bridge.apiGet("push/tasks");
    pushTasks = {};
    (d.tasks || []).forEach((t) => { pushTasks[t.id] = t; });
    renderPushRows();
    const legacy = d.legacy_tasks || [];
    const el = $("#pushLegacy");
    if (legacy.length) {
      el.style.display = "block";
      el.innerHTML = "<b>插件配置中的旧版定时任务（只读，建议删除配置项后在上表重建）：</b><br>"
        + legacy.map((t) => `${esc(t.cron)} → ${t.target_type === "group" ? "群" : "私聊"}${esc(t.target_id)} ${esc(t.kind_name || t.kind)}`).join("<br>");
    } else el.style.display = "none";
  } catch (e) { toast("加载推送任务失败: " + e.message); }
}

function renderPushRows() {
  const rows = Object.values(pushTasks);
  $("#pushRows").innerHTML = rows.map((t) => `
    <tr>
      <td><code>${esc(t.cron)}</code></td>
      <td>${esc(t.desc || "—")}</td>
      <td>${esc((platformMap[t.platform] || {}).label || t.platform)}</td>
      <td>${t.target_type === "group" ? "群聊" : "私聊"} ${esc(t.target_id)}</td>
      <td>${esc(t.kind_name || KIND_NAME[t.kind] || t.kind)}</td>
      <td>${t.enabled === false ? '<span class="muted">已停用</span>' : '<span class="tag ok">启用中</span>'}</td>
      <td>
        <button class="ghost" data-act="once" data-id="${esc(t.id)}">推送</button>
        <button class="ghost" data-act="edit" data-id="${esc(t.id)}">编辑</button>
        <button class="ghost" data-act="toggle" data-id="${esc(t.id)}">${t.enabled === false ? "启用" : "停用"}</button>
        <button class="ghost" data-act="delete" data-id="${esc(t.id)}">删除</button>
      </td>
    </tr>`).join("")
    || '<tr><td colspan="7" class="empty">还没有定时推送任务，在上方创建一个吧</td></tr>';
}

function resetPushForm() {
  editingPushId = null;
  $("#pushCron").value = "";
  $("#pushTarget").value = "";
  $("#pushBind").value = "";
  $("#pushSaveBtn").textContent = "保存任务";
  $("#pushCancelBtn").style.display = "none";
}

async function savePush() {
  const params = {
    cron: $("#pushCron").value.trim(),
    platform: $("#pushPlatform").value,
    target_type: $("#pushTargetType").value,
    target_id: $("#pushTarget").value.trim(),
    kind: $("#pushKind").value,
    bind_id: $("#pushBind").value.trim(),
  };
  if (!params.platform) { toast("请先选择发送平台"); return; }
  if (!params.target_id) { toast("请选择或填写推送目标（群号/QQ号）"); return; }
  if (!params.cron) { toast("请填写 cron 表达式，或用「每天此时间」快速生成"); return; }
  if (editingPushId) params.id = editingPushId;
  try {
    const d = await bridge.apiGet("push/save", params);
    if (d.error) { toast(d.error); return; }
    toast(editingPushId ? "任务已更新" : "任务已创建");
    resetPushForm();
    loadPushTasks();
  } catch (e) { toast("保存失败: " + e.message); }
}

function editPush(id) {
  const t = pushTasks[id];
  if (!t) return;
  editingPushId = id;
  $("#pushKind").value = t.kind || "meituan";
  $("#pushPlatform").value = t.platform || "";
  $("#pushTargetType").value = t.target_type || "group";
  loadTargets();
  $("#pushTarget").value = t.target_id || "";
  $("#pushCron").value = t.cron || "";
  $("#pushBind").value = t.bind_id || "";
  $("#pushSaveBtn").textContent = "更新任务";
  $("#pushCancelBtn").style.display = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function pushAction(act, id) {
  try {
    const d = await bridge.apiGet("push/" + act, { id });
    if (d.error) { toast(d.error); return; }
    if (act === "once") { toast("已推送 ✅"); return; }
    if (act === "delete" && editingPushId === id) resetPushForm();
    loadPushTasks();
  } catch (e) { toast("操作失败: " + e.message); }
}

function cronQuick() {
  const t = $("#pushTime").value || new Date().toTimeString().slice(0, 5);
  const [h, m] = t.split(":");
  $("#pushCron").value = `${Number(m)} ${Number(h)} * * *`;
}

function bindPushEvents() {
  $("#pushPlatform").addEventListener("change", loadTargets);
  $("#pushTargetType").addEventListener("change", loadTargets);
  $("#pushSaveBtn").addEventListener("click", savePush);
  $("#pushCancelBtn").addEventListener("click", resetPushForm);
  $("#cronQuick").addEventListener("click", cronQuick);
  $("#pushRows").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const { act, id } = b.dataset;
    if (act === "edit") editPush(id);
    else pushAction(act, id);
  });
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("#" + t.dataset.p).classList.add("active");
    if (t.dataset.p === "p1") loadStats();
    if (t.dataset.p === "p2") loadOrders(1);
    if (t.dataset.p === "p3") loadBindings();
    if (t.dataset.p === "p6") { loadPlatforms(); loadPushTasks(); }
  }));
  $("#syncBtn").addEventListener("click", doSync);
  $("#searchBtn").addEventListener("click", () => loadOrders(1));
  $("#fQ").addEventListener("keydown", (e) => { if (e.key === "Enter") loadOrders(1); });
  $("#prevBtn").addEventListener("click", () => loadOrders(page - 1));
  $("#nextBtn").addEventListener("click", () => loadOrders(page + 1));
  $("#convertBtn").addEventListener("click", convert);
  $("#convert2Btn").addEventListener("click", convert2);
  bindPushEvents();
}

(async () => {
  const ctx = await bridge.ready();
  renderTheme(ctx);
  bridge.onContext(renderTheme);
  bindEvents();
  loadStats();
  loadOrders(1);
  loadBindings();
})();
