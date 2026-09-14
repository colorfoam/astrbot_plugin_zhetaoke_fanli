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

/* 时间筛选格式校验：4位年 / 6位年月 / 8位年月日 / 带分隔符日期，月份日期必须真实存在 */
function dateValid(s) {
  const t = String(s || "").trim();
  if (!t) return true;
  if (/[^\d年月日\-/. ]/.test(t)) return false;
  const norm = t.replace(/[年月./]/g, "-").replace(/日/g, "").replace(/-+$/, "").trim();
  let parts = norm.split("-").filter(Boolean);
  if (parts.length !== 1) return parts.length >= 1;
  const p = parts[0];
  if (!/^\d+$/.test(p)) return false;
  if (p.length === 4) parts = [p];
  else if (p.length === 6) parts = [p.slice(0, 4), p.slice(4)];
  else if (p.length === 8) parts = [p.slice(0, 4), p.slice(4, 6), p.slice(6)];
  else return false;
  const n = parts.map(Number);
  if (parts.some((x) => !/^\d+$/.test(x))) return false;
  if (n[0] < 2015 || n[0] > 2100) return false;
  if (parts.length >= 2 && !(n[1] >= 1 && n[1] <= 12)) return false;
  if (parts.length >= 3) {
    const last = new Date(n[0], n[1], 0).getDate();
    if (!(n[2] >= 1 && n[2] <= last)) return false;
  }
  return true;
}

function markDate(el, ok) {
  el.style.borderColor = ok ? "" : "#d93026";
  el.title = ok ? "" : "格式：2026-09-14 / 20260914 / 202609(整月) / 2026(全年)，月份日期须真实存在";
}

function orderParams() {
  const p = {};
  if ($("#fPlatform").value) p.platform = $("#fPlatform").value;
  if ($("#fStatus").value) p.status = $("#fStatus").value;
  const sOk = dateValid($("#fStart").value), eOk = dateValid($("#fEnd").value);
  markDate($("#fStart"), sOk);
  markDate($("#fEnd"), eOk);
  if (!sOk || !eOk) { toast("日期格式不对：支持 2026-09-14、20260914、202609(整月)、2026(全年)"); return null; }
  if ($("#fStart").value) p.start = $("#fStart").value.trim();
  if ($("#fEnd").value) p.end = $("#fEnd").value.trim();
  if ($("#fQ").value) p.q = $("#fQ").value;
  return p;
}

async function loadOrders(p) {
  page = Math.max(1, p || 1);
  const params = orderParams();
  if (!params) return;   // 日期校验未过
  try {
    const d = await bridge.apiGet("orders", { ...params, page, page_size: pageSize });
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

/* 可靠复制：iframe 里 navigator.clipboard 常被禁，用 execCommand 兜底 */
function copyText(text, btn) {
  const done = () => {
    if (!btn) { toast("已复制"); return; }
    btn.textContent = "✓ 已复制";
    setTimeout(() => { btn.textContent = "复制"; }, 1500);
  };
  const legacy = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;top:-999px;left:-999px;opacity:0";
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try {
      if (document.execCommand("copy")) { done(); }
      else { toast("复制失败，请长按/选中手动复制"); }
    } catch (e) { toast("复制失败，请长按/选中手动复制"); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(legacy);
  } else legacy();
}

const ROW_BTN = 'class="cbtn" data-copy="';
function row(label, value) {
  return `<div class="crow"><span class="clabel">${label}</span>`
    + `<span class="cval"><a href="${esc(value)}" target="_blank">${esc(value)}</a></span>`
    + `<button ${ROW_BTN}${esc(value)}">复制</button></div>`;
}

async function convert() {
  const params = { platform: $("#cPlatform").value };
  if ($("#cBind").value) params.bind_id = $("#cBind").value;
  $("#cResult").innerHTML = '<span style="opacity:.6">请求中…</span>';
  try {
    const d = await bridge.apiGet("convert", params);
    if (d.error) { $("#cResult").innerHTML = `<div class="cerr">❌ ${esc(d.error)}</div>`; return; }
    const link = d.link || (d.item && (d.item.short_link || d.item.click_url)) || "";
    let html = "";
    if (link) html += `<div class="cok">✅ 生成成功</div>` + row("推广链接", link);
    if (d.password) html += row("淘口令", d.password);
    if (d.pic) {
      if ($("#cShortQr").checked) {
        // 二维码图片长链接 → 短链（选项勾选时）
        try {
          const s = await bridge.apiGet("shorten", { url: d.pic, engine: "sina" });
          if (s.ok) html += row("二维码短链", s.short);
          else html += `<div class="cwarn">⚠ 二维码短链失败: ${esc(s.error || "")}</div>` + row("二维码图片", d.pic);
        } catch (e) {
          html += `<div class="cwarn">⚠ 二维码短链请求失败</div>` + row("二维码图片", d.pic);
        }
      } else {
        html += row("二维码图片", d.pic);
      }
      html += `<div class="cqr"><img src="${esc(d.pic)}" alt="二维码" referrerpolicy="no-referrer"></div>`;
    }
    if (!html) html = `<pre>${esc(JSON.stringify(d, null, 2))}</pre>`;
    $("#cResult").innerHTML = html;
  } catch (e) { $("#cResult").innerHTML = `<div class="cerr">请求失败: ${esc(e.message)}</div>`; }
}

async function convert2() {
  const content = $("#cContent").value.trim();
  if (!content) { toast("请输入商品链接或口令"); return; }
  const params = { platform: $("#cPlatform2").value, content };
  if ($("#cBind2").value) params.bind_id = $("#cBind2").value;
  $("#cResult2").innerHTML = '<span style="opacity:.6">请求中…</span>';
  try {
    const d = await bridge.apiGet("convert", params);
    if (d.error) { $("#cResult2").innerHTML = `<div class="cerr">❌ ${esc(d.error)}</div>`; return; }
    const link = d.link || (d.item && (d.item.short_link || d.item.click_url)) || "";
    let html = "";
    if (link) html += `<div class="cok">✅ 转链成功</div>` + row("推广链接", link);
    if (d.password) html += row("淘口令", d.password);
    if (!html) html = `<pre>${esc(JSON.stringify(d, null, 2))}</pre>`;
    $("#cResult2").innerHTML = html;
  } catch (e) { $("#cResult2").innerHTML = `<div class="cerr">请求失败: ${esc(e.message)}</div>`; }
}

/* ---------- 配置 ---------- */
let cfgListVals = {};        // list 型字段当前值（标签编辑器数据源）
let cfgQuickOpts = {};       // key → [{v,label}] 简单快捷选项（如 Markdown 渠道）
let cfgPlatformCache = null; // 平台列表缓存（会话选择器用）
const PICKER_KEYS = ["admin_ids", "chat_blacklist", "chat_whitelist"];

async function loadConfig() {
  const el = $("#cfgBody");
  el.textContent = "加载中…";
  try {
    const d = await bridge.apiGet("config");
    if (d.error) { el.textContent = "❌ " + d.error; return; }
    renderConfig(d.schema || {}, d.values || {});
    await loadPlatformCache();
    renderConfig(d.schema || {}, d.values || {});
    initSessionPickers();
  } catch (e) { el.textContent = "加载失败: " + e.message; }
}

/* 读取平台实例列表（会话选择器数据源），失败则只剩手填 */
async function loadPlatformCache() {
  try {
    const d = await bridge.apiGet("push/platforms");
    cfgPlatformCache = (d.platforms || []).map((p) => ({
      id: p.id,
      name: p.name || p.display_name || p.id,
      type: p.type || "",
    }));
  } catch (e) { cfgPlatformCache = []; }
  // Markdown 渠道列表：简单快捷选项 = 平台实例名 + 适配器类型
  cfgQuickOpts.rp_md_platforms = (cfgPlatformCache || []).flatMap((p) =>
    [p.id, p.type].filter(Boolean)).filter((v, i, a) => a.indexOf(v) === i)
    .map((v) => ({ v, label: v }));
}

/* 会话选择器：平台 → 群聊/私聊 → 目标下拉（也可在旁边输入框手填），同定时推送页 */
function sessionPickerHTML(k) {
  const pls = cfgPlatformCache || [];
  if (!pls.length) return "";   // 平台读不到 → 只保留手填输入框
  return `<div class="session-picker" data-k="${esc(k)}">
    <select class="sp-platform" title="平台（AstrBot 实例）">
      ${pls.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}（${esc(p.type)}）</option>`).join("")}
    </select>
    <select class="sp-type" title="会话类型">
      <option value="group">群聊</option>
      <option value="private">私聊</option>
    </select>
    <select class="sp-select" title="从机器人读取的列表">
      <option value="">加载中…</option>
    </select>
    <input class="sp-target" placeholder="或手动输入群号/QQ号">
    <button type="button" class="tagbtn sp-add" data-tk="${esc(k)}">添加</button>
  </div>`;
}

/* 按当前平台/类型拉取群列表或好友列表填充下拉（读不到则提示手填） */
async function loadPickerTargets(picker) {
  if (!picker) return;
  const pid = picker.querySelector(".sp-platform").value;
  const tt = picker.querySelector(".sp-type").value;
  const sel = picker.querySelector(".sp-select");
  const input = picker.querySelector(".sp-target");
  input.placeholder = tt === "group" ? "或手动输入群号" : "或手动输入QQ号";
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const d = await bridge.apiGet("push/targets",
      { platform_id: pid, target_type: tt });
    const ts = d.targets || [];
    sel.innerHTML = ts.length
      ? `<option value="">选择${tt === "group" ? "群" : "好友"}（${ts.length}）</option>`
        + ts.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}（${esc(t.id)}）</option>`).join("")
      : '<option value="">该平台读不到列表，请手填</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">该平台读不到列表，请手填</option>';
  }
}

function initSessionPickers() {
  document.querySelectorAll("#cfgBody .session-picker")
    .forEach((p) => loadPickerTargets(p));
}

function quickSelectHTML(k, opts) {
  if (!opts.length) return "";
  return `<select class="tagquick" data-tk="${esc(k)}">
       <option value="">＋ 从机器人读取…</option>
       ${opts.map((o) => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join("")}
     </select>`;
}

function tagEditorHTML(k) {
  const tags = cfgListVals[k] || [];
  const chips = tags.map((t, i) =>
    `<span class="tagchip">${esc(t)}<a data-tk="${esc(k)}" data-ti="${i}" title="移除">×</a></span>`).join("");
  const picker = PICKER_KEYS.includes(k) ? sessionPickerHTML(k) : "";
  const quick = picker ? "" : quickSelectHTML(k, cfgQuickOpts[k] || []);
  return `<div class="tagbox">${chips}
    <input class="tagin" data-tk="${esc(k)}" placeholder="手动输入，回车或点添加">
    <button type="button" class="tagbtn" data-tk="${esc(k)}">添加</button>${quick}</div>${picker}`;
}

/* 板块图标（分组名 → emoji，未匹配则用通用图标） */
const GROUP_ICONS = { "折淘客凭据": "🔑", "返利设置": "💰", "订单同步": "🔄",
  "会话与推送": "💬", "WebUI仪表盘": "🖥️", "京东": "🛍️", "定时推送": "⏰" };

function cfgField(k, item, val) {
  const desc = item.description || k;
  const hint = item.hint ? `<div class="cfg-hint">${esc(item.hint)}</div>` : "";
  let input = "";
  const t = item.type || "string";
  const v = val == null ? "" : String(val);
  if (t === "bool") {
    input = `<label class="cfg-switch">
      <input type="checkbox" class="cfg-input" data-key="${esc(k)}" data-type="bool" ${val ? "checked" : ""}><span>启用</span></label>`;
  } else if (t === "list") {
    cfgListVals[k] = Array.isArray(val) ? val.slice()
      : String(val || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    input = tagEditorHTML(k);
  } else if (t === "text") {
    input = `<textarea class="cfg-input" data-key="${esc(k)}" data-type="text" rows="3">${esc(v)}</textarea>`;
  } else if (Array.isArray(item.options)) {
    const labels = { both: "both · 文字+二维码图片", link: "link · 仅文字链接",
      image: "image · 仅二维码图片", tb: "tb · 淘宝独立二维码（推荐）",
      wx: "wx · 微信小程序码", off: "off · 关闭", "1": "1 · 京小街",
      "2": "2 · 京东购物（短链 u.jd.com，默认）", "3": "3 · 长链+短链",
      plain: "plain · 纯文本", markdown: "markdown · 始终 Markdown",
      auto: "auto · 按渠道自动", blacklist: "blacklist · 黑名单", whitelist: "whitelist · 白名单" };
    input = `<select class="cfg-input" data-key="${esc(k)}" data-type="${esc(t)}">`
      + item.options.map((o) => `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(labels[o] || o)}</option>`).join("")
      + `</select>`;
  } else if (t === "int" || t === "float") {
    const step = t === "int" ? "1" : "0.1";
    const min = item.min != null ? ` min="${item.min}"` : "";
    const max = item.max != null ? ` max="${item.max}"` : "";
    input = `<input type="number" step="${step}"${min}${max} class="cfg-input" data-key="${esc(k)}" data-type="${esc(t)}" value="${esc(v)}">`;
  } else {
    input = item.secret
      ? `<div class="cfg-secret"><input type="password" class="cfg-input" data-key="${esc(k)}" data-type="string" value="${esc(v)}"><button type="button" class="cfg-eye">👁 显示</button></div>`
      : `<input type="text" class="cfg-input" data-key="${esc(k)}" data-type="string" value="${esc(v)}">`;
  }
  const cls = (t === "list" || t === "text") ? "cfg-field cfg-wide" : "cfg-field";
  return `<div class="${cls}"><div class="cfg-label">${esc(desc)}</div>${hint}${input}</div>`;
}

function renderConfig(schema, values) {
  let html = "";
  for (const [gname, g] of Object.entries(schema)) {
    const items = (g && g.items) || {};
    const keys = Object.keys(items);
    if (!keys.length) continue;
    html += `<div class="cfg-card" data-group="${esc(gname)}">
      <div class="cfg-card-head"><span class="cfg-badge">${esc(gname)}</span>
        ${g.description ? `<span class="cfg-card-desc">${esc(g.description)}</span>` : ""}</div>
      <div class="cfg-card-body">`;
    for (const k of keys) html += cfgField(k, items[k] || {}, values[k]);
    html += `</div></div>`;
  }
  $("#cfgBody").innerHTML = html || "配置 schema 为空";
}

/* 标签编辑器交互（事件委托） */
function tagAdd(k, raw) {
  const t = String(raw || "").trim();
  if (!t) return;
  const arr = cfgListVals[k] || (cfgListVals[k] = []);
  if (!arr.includes(t)) arr.push(t);
  refreshTagbox(k);
}
function refreshTagbox(k) {
  const field = document.querySelector(`.tagbox .tagin[data-tk="${CSS.escape(k)}"]`);
  if (!field) return;
  const box = field.closest(".tagbox");
  box.outerHTML = tagEditorHTML(k);
  // 重新渲染后恢复会话选择器的群/好友列表
  const picker = document.querySelector(`.session-picker[data-k="${CSS.escape(k)}"]`);
  if (picker) loadPickerTargets(picker);
}

function bindCfgEvents() {
  $("#cfgBody").addEventListener("click", (e) => {
    // 密码字段 显示/隐藏
    const eye = e.target.closest(".cfg-eye");
    if (eye) {
      const inp = eye.parentElement.querySelector("input");
      const show = inp.type === "password";
      inp.type = show ? "text" : "password";
      eye.textContent = show ? "🙈 隐藏" : "👁 显示";
      return;
    }
    const rm = e.target.closest(".tagchip a");
    if (rm) {
      const arr = cfgListVals[rm.dataset.tk] || [];
      arr.splice(parseInt(rm.dataset.ti, 10), 1);
      refreshTagbox(rm.dataset.tk);
      return;
    }
    // 会话选择器「添加」：优先手填输入框，否则取下拉选中的群号/QQ号
    const spAdd = e.target.closest(".sp-add");
    if (spAdd) {
      const picker = spAdd.closest(".session-picker");
      const input = picker.querySelector(".sp-target");
      const sel = picker.querySelector(".sp-select");
      const val = (input && input.value.trim()) || (sel && sel.value) || "";
      tagAdd(spAdd.dataset.tk, val);
      if (input) input.value = "";
      if (sel) sel.value = "";
      return;
    }
    const add = e.target.closest(".tagbtn:not(.sp-add)");
    if (add) {
      const input = document.querySelector(`.tagin[data-tk="${CSS.escape(add.dataset.tk)}"]`);
      tagAdd(add.dataset.tk, input && input.value);
    }
  });
  $("#cfgBody").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.classList.contains("tagin")) return;
    e.preventDefault();
    tagAdd(e.target.dataset.tk, e.target.value);
  });
  $("#cfgBody").addEventListener("change", (e) => {
    // 会话选择器：切平台/切类型 → 重拉群/好友列表
    if (e.target.classList.contains("sp-platform")
        || e.target.classList.contains("sp-type")) {
      loadPickerTargets(e.target.closest(".session-picker"));
      return;
    }
    if (!e.target.classList.contains("tagquick")) return;
    const sel = e.target;
    if (sel.value) {
      tagAdd(sel.dataset.tk, sel.value);
      sel.value = "";
    }
  });
}

/* 保存全部板块：收集所有卡片的字段，经右下角浮动保存条提交 */
async function saveConfigAll() {
  const btn = $("#cfgSaveAll"), msg = $("#cfgMsg");
  const payload = {};
  document.querySelectorAll("#cfgBody .cfg-input[data-key]").forEach((el) => {
    payload[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.value;
  });
  // list 型字段的值来自标签编辑器/会话选择器（按 data-tk 收集）
  document.querySelectorAll("#cfgBody [data-tk]").forEach((el) => {
    const k = el.dataset.tk;
    if (cfgListVals[k]) payload[k] = cfgListVals[k];
  });
  btn.disabled = true; msg.textContent = "保存中…";
  try {
    const d = await bridge.apiGet("config/save", { data: JSON.stringify(payload) });
    if (d.error) msg.textContent = "❌ " + d.error;
    else { msg.textContent = "✅ 已保存并生效"; toast("配置已保存"); }
  } catch (e) { msg.textContent = "保存失败: " + e.message; }
  btn.disabled = false;
}

/* ---------- 定时推送 ---------- */
const KIND_NAME = { meituan: "美团红包", eleme: "闪购红包", jd: "京东红包", orders: "订单日报" };
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

/* ---------- 使用说明：聊天指令从后端动态读取（与实际注册指令同源，不会写死过期） ---------- */
async function loadHelpCommands() {
  const el = $("#helpCmds");
  if (!el) return;
  try {
    const d = await bridge.apiGet("commands");
    el.innerHTML = (d.commands || []).map((c) => {
      const names = [c.cmd, ...(c.alias || [])].map((n) => `<code>${esc(n)}</code>`).join(" / ");
      const tag = c.admin ? ' <span class="tag refund">管理员</span>' : "";
      return `${names}${tag} <span class="muted">— ${esc(c.desc)}</span>`;
    }).join("<br>");
  } catch (e) {
    el.textContent = "聊天指令：绑定 / 我的返利 / 查订单 / 美团红包 / 闪购红包 / 京东红包 / 淘宝转链 内容 / 抖音转链 内容 / 搜抖音 关键词 / 同步订单(管理员)";
  }
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
    if (t.dataset.p === "p7") loadConfig();
    if (t.dataset.p === "p5") loadHelpCommands();
  }));
  $("#syncBtn").addEventListener("click", doSync);
  $("#searchBtn").addEventListener("click", () => loadOrders(1));
  $("#fQ").addEventListener("keydown", (e) => { if (e.key === "Enter") loadOrders(1); });
  // 时间输入：输入时清除红框提示
  ["fStart", "fEnd"].forEach((id) => {
    $("#" + id).addEventListener("input", (e) => markDate(e.target, true));
  });
  $("#fQuick").addEventListener("change", () => {
    const v = $("#fQuick").value;
    const now = new Date();
    const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    if (!v) { $("#fStart").value = ""; $("#fEnd").value = ""; }
    else if (v === "today") { $("#fStart").value = fmt(now); $("#fEnd").value = fmt(now); }
    else if (v === "yesterday") { const y = new Date(now); y.setDate(y.getDate() - 1); $("#fStart").value = fmt(y); $("#fEnd").value = fmt(y); }
    else if (v === "7d") { const s = new Date(now); s.setDate(s.getDate() - 6); $("#fStart").value = fmt(s); $("#fEnd").value = fmt(now); }
    else if (v === "month") { $("#fStart").value = fmt(new Date(now.getFullYear(), now.getMonth(), 1)); $("#fEnd").value = fmt(now); }
    else if (v === "lastmonth") {
      $("#fStart").value = fmt(new Date(now.getFullYear(), now.getMonth() - 1, 1));
      $("#fEnd").value = fmt(new Date(now.getFullYear(), now.getMonth(), 0));
    }
    loadOrders(1);
  });
  $("#prevBtn").addEventListener("click", () => loadOrders(page - 1));
  $("#nextBtn").addEventListener("click", () => loadOrders(page + 1));
  $("#convertBtn").addEventListener("click", convert);
  $("#convert2Btn").addEventListener("click", convert2);
  // 配置页：每张板块卡片自己的「保存本板块」按钮
  // 配置页：右下角浮动「保存设置」一键保存全部板块
  $("#cfgSaveAll").addEventListener("click", saveConfigAll);
  bindCfgEvents();
  // 转链结果「复制」按钮（事件委托，覆盖 cResult 动态生成的按钮）
  $("#cResult").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-copy]");
    if (!b) return;
    copyText(b.dataset.copy, b);
  });
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
