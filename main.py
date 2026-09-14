# -*- coding: utf-8 -*-
"""
astrbot_plugin_zhetaoke_fanli — 折淘客全平台返利助手

对接折淘客开放平台 API（https://www.zhetaoke.com/one/api3.aspx）：
  - 美团外卖/闪购：转链 + 订单/佣金查询（open_meituan_generateLink / open_meituan_orderList2）
  - 饿了么（淘宝闪购）：红包活动转链（open_eleme_generateLink）
  - 淘宝：高佣转链 + 订单查询（open_gaoyongzhuanlian_tkl / open_dingdanchaxun2）
  - 抖音：商品搜索/转链（open_douyin_product_search / open_douyin_zhuanlian）
  - 联盟全平台订单：open_lianmeng_orderList（美团/淘宝/饿了么/抖音/京东/拼多多...）
  - 内置 WebUI 仪表盘：订单浏览/搜索/统计/用户返利/转链工具
"""

import asyncio
import calendar
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode, quote

import aiohttp
from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# AstrBot 插件页面 Web API（v4.24+ 提供，旧版本优雅降级）
try:
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request
    _WEB_API_AVAILABLE = True
except ImportError:  # 旧版 AstrBot 无 astrbot.api.web，仅保留独立 WebUI
    _WEB_API_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_zhetaoke_fanli"
API_BASE = "https://api.zhetaoke.com:10001/api"
API_BASE_BACKUP = "http://api.zhetaoke.cn:10000/api"

# 订单状态映射（美团/联盟订单 + 淘宝 tk_status）
ORDER_STATUS = {"1": "已付款", "8": "已完成", "9": "已退款/风控",
                "3": "结算成功", "12": "已付款", "13": "已关闭", "14": "确认收货"}
# 美团订单类型
MT_ORDER_TYPE = {"0": "团购", "2": "酒店", "4": "外卖", "5": "话费/团好货", "6": "闪购", "8": "优选"}
# 联盟订单平台 id
LIANMENG_PLATFORM = {
    "1": "美团", "2": "考拉", "3": "苏宁", "4": "淘宝", "5": "京东",
    "6": "拼多多", "7": "唯品会", "8": "饿了么", "9": "抖音",
}
# 京东订单 validCode → 本插件统一状态键（1已付款/8已完成/9已退款·风控）
JD_VALID_STATUS = {"15": "1", "16": "1", "17": "8", "18": "8"}
# 京东订单 validCode → 展示文案
JD_STATUS_TEXT = {"15": "待付款", "16": "已付款", "17": "已完成", "18": "已结算"}
# 京东无效单状态码说明（其余码一律按无效处理）
JD_INVALID_CODE = {
    "2": "无效-拆单", "3": "无效-取消", "4": "无效-京东帮帮主订单",
    "5": "无效-账号异常", "6": "无效-赠品类目不返佣", "7": "无效-校园订单",
    "8": "无效-企业订单", "9": "无效-团购订单", "11": "无效-乡村推广员下单",
    "13": "无效-违规订单", "14": "无效-来源与备案网址不符",
}
# 京东红包默认活动：京东外卖 CPS 活动页（可直接在插件配置里换成你自己的活动链接）
JD_DEFAULT_ACT_URL = ("https://pro.m.jd.com/mall/active/"
                      "4CJH74pqm4snemxqc2TBUJpZe9JQ/index.html")

# 数据目录：优先用 AstrBot 官方数据目录（绝对路径，不依赖启动时的工作目录），
# 取不到时退回相对路径 data/<插件名>/
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    _DATA_ROOT = get_astrbot_data_path()
except Exception:
    _DATA_ROOT = "data"
DATA_DIR = os.path.join(_DATA_ROOT, "plugin_data", PLUGIN_NAME)
DB_PATH = os.path.join(DATA_DIR, "orders.db")
# 旧版相对路径的库文件，找到后自动迁移到新位置
_LEGACY_DB = os.path.join("data", PLUGIN_NAME, "orders.db")


def _migrate_legacy_db():
    try:
        if os.path.isfile(_LEGACY_DB) and not os.path.isfile(DB_PATH):
            shutil.copy2(_LEGACY_DB, DB_PATH)
            logger.info(f"[ztk] 已迁移历史订单库: {_LEGACY_DB} -> {DB_PATH}")
    except Exception as e:
        logger.warning(f"[ztk] 旧订单库迁移失败（不影响运行）: {e}")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def auto_bind_id(user_id: str) -> str:
    """从用户 id 派生一个稳定的 10 位数字返利 ID（≤11 位数字限制）。"""
    h = hashlib.md5(str(user_id).encode("utf-8")).hexdigest()
    return str(int(h[:12], 16) % 10_000_000_000).zfill(10)


class ZtkClient:
    """折淘客 API 客户端（aiohttp 异步，带主备域名切换与全局限速）"""

    def __init__(self, appkey: str, min_interval: float = 2.0):
        self.appkey = appkey
        self.min_interval = min_interval
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def terminate(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _get(self, path: str, params: dict) -> dict:
        if not self.appkey:
            raise RuntimeError("未配置折淘客 appkey，请在插件配置中填写")
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["appkey"] = self.appkey
        async with self._lock:
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()
        last_err = None
        for base in (API_BASE, API_BASE_BACKUP):
            url = f"{base}/{path}"
            try:
                async with self.session.get(url, params=params) as resp:
                    text = await resp.text()
                    data = json.loads(text)
                    if isinstance(data, str):
                        # 折淘客部分接口返回二次编码的 JSON 字符串
                        try:
                            data = json.loads(data)
                        except (ValueError, TypeError):
                            pass
                    if not isinstance(data, dict):
                        raise ValueError(f"响应非JSON对象: {str(data)[:200]}")
                    return data
            except Exception as e:
                last_err = e
                logger.warning(f"[ztk] 请求 {url} 失败: {e}，尝试备用域名")
        raise RuntimeError(f"折淘客接口请求失败: {last_err}")

    # ---------- 转链 ----------
    async def meituan_convert(self, sid, act_id, customer_id="", link_type="1"):
        return await self._get("open_meituan_generateLink.ashx", {
            "sid": sid, "actId": act_id, "linkType": link_type,
            "miniCode": "1", "customer_id": customer_id,
        })

    async def eleme_convert(self, sid, activity_id, customer_id=""):
        return await self._get("open_eleme_generateLink.ashx", {
            "sid": sid, "activity_id": activity_id, "customer_id": customer_id,
        })

    async def taobao_convert(self, sid, pid, tkl_content, external_id=""):
        return await self._get("open_gaoyongzhuanlian_tkl.ashx", {
            "sid": sid, "pid": pid, "tkl": tkl_content, "external_id": external_id,
            "signurl": "5",
        })

    async def douyin_convert(self, sid, product_url, external_info=""):
        return await self._get("open_douyin_zhuanlian.ashx", {
            "sid": sid, "product_url": quote(str(product_url), safe=""),
            "external_info": external_info, "use_coupon": "true",
        })

    async def douyin_search(self, sid, keyword, page=1, page_size=10):
        return await self._get("open_douyin_product_search.ashx", {
            "sid": sid, "title": quote(keyword, safe=""),
            "page": page, "page_size": page_size, "search_type": "3", "sort_type": "1",
        })

    async def jd_convert(self, material_id, union_id, position_id="",
                         chain_type="2", sub_union_id="", signurl="0",
                         wechat_type=""):
        """京东转链API-新：商品/活动链接、短链（3.cn）、口令、SKU ID 均可转链。
        positionId/subUnionId 用于返利归因（订单接口会透出）；
        wechat_type：1=京小街、2=京东购物，返回微信小程序短链（可选）"""
        params = {
            "materialId": quote(str(material_id), safe=""),
            "unionId": union_id,
            "positionId": position_id,
            "subUnionId": sub_union_id,
            "chainType": chain_type,
            "signurl": signurl,
        }
        if str(wechat_type or "").strip() in ("1", "2"):
            params["weChatType"] = str(wechat_type).strip()
        return await self._get("open_jing_union_open_promotion_byunionid_get.ashx",
                               params)

    async def shorturl(self, content: str, sid: str = "", engine: str = "sina") -> dict:
        """短链接生成：sina(t.cn)/baidu(dwz.cn)。content 为需缩短的原始 URL（需 urlencode）。
        接口要求 appkey + sid（淘客账号授权ID）+ content"""
        path = ("open_shorturl_sina_get.ashx" if engine == "sina"
                else "open_shorturl_baidu_get.ashx")
        return await self._get(path, {"content": content, "sid": sid})

    async def jd_orders(self, jd_app_key, jd_app_secret, page=1, page_size=100,
                        query_type="1", start_time="", end_time="", key="",
                        child_union_id=""):
        """京东订单查询（需京东开放平台应用 appkey/appsecret，时间跨度≤1小时）"""
        return await self._get("open_jing_union_openz_order_row_query.ashx", {
            "jd_app_key": jd_app_key, "jd_app_secret": jd_app_secret,
            "key": key, "childUnionId": child_union_id,
            "pageIndex": page, "pageSize": page_size, "type": query_type,
            "startTime": start_time, "endTime": end_time, "fields": "goodsInfo",
        })

    # ---------- 订单 ----------
    async def meituan_orders(self, query_type="1", page=1, page_size=50,
                             start_time="", end_time="", orderid="", sid=""):
        return await self._get("open_meituan_orderList2.ashx", {
            "type": query_type, "page": page, "page_size": page_size,
            "startTime": start_time, "endTime": end_time,
            "orderid": orderid, "sid": sid,
        })

    async def lianmeng_orders(self, query_type="2", page=1, page_size=50,
                              start_time="", end_time="", orderid="", sid="",
                              san_pingtai_id=""):
        return await self._get("open_lianmeng_orderList.ashx", {
            "type": query_type, "page": page, "page_size": page_size,
            "startTime": start_time, "endTime": end_time,
            "orderid": orderid, "sid": sid, "san_pingtai_id": san_pingtai_id,
        })

    async def taobao_orders(self, sid, start_time, end_time, query_type="4",
                            page_no=1, page_size=40, position_index=""):
        return await self._get("open_dingdanchaxun2.ashx", {
            "sid": sid, "start_time": start_time, "end_time": end_time,
            "query_type": query_type, "page_no": page_no, "page_size": page_size,
            "position_index": position_index,
        })


class OrderStore:
    """SQLite 订单/绑定存储"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _migrate_legacy_db()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            platform TEXT NOT NULL,
            orderid TEXT NOT NULL,
            title TEXT DEFAULT '',
            pay_price REAL DEFAULT 0,
            profit REAL DEFAULT 0,
            status TEXT DEFAULT '',
            status_text TEXT DEFAULT '',
            order_type TEXT DEFAULT '',
            pay_time TEXT DEFAULT '',
            update_time TEXT DEFAULT '',
            refund_price REAL DEFAULT 0,
            refund_time TEXT DEFAULT '',
            settled INTEGER DEFAULT 0,
            settle_profit REAL DEFAULT 0,
            customer_id TEXT DEFAULT '',
            raw TEXT DEFAULT '{}',
            PRIMARY KEY (platform, orderid)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
        CREATE INDEX IF NOT EXISTS idx_orders_paytime ON orders(pay_time);
        CREATE TABLE IF NOT EXISTS bindings (
            user_key TEXT PRIMARY KEY,
            bind_id TEXT NOT NULL,
            created_at TEXT
        );
        """)
        self.db.commit()

    # ---- 订单 ----
    @staticmethod
    def _normalize_rows(rows):
        """兼容折淘客各订单接口的返回差异：rows 本身或其中元素可能是
        JSON 字符串（如 content 为 "[{...},{...}]" 或元素为 "{\"orderid\":...}"）"""
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except (ValueError, TypeError):
                return []
        if isinstance(rows, dict):
            for v in ("content", "data", "list", "result"):
                if isinstance(rows.get(v), list):
                    rows = rows[v]
                    break
            else:
                rows = [rows]
        norm = []
        for r in rows:
            if isinstance(r, str):
                try:
                    r = json.loads(r)
                except (ValueError, TypeError):
                    continue
            if isinstance(r, dict):
                norm.append(r)
        return norm

    def upsert_orders(self, platform: str, rows: list):
        cur = self.db.cursor()
        new_cnt = 0
        for r in self._normalize_rows(rows):
            oid = str(r.get("orderid") or r.get("trade_id") or "")
            if not oid:
                continue
            status_key = str(r.get("status") or r.get("tk_status") or "")
            exists = cur.execute(
                "SELECT 1 FROM orders WHERE platform=? AND orderid=?",
                (platform, oid)).fetchone()
            cur.execute("""
                INSERT OR REPLACE INTO orders
                (platform, orderid, title, pay_price, profit, status, status_text,
                 order_type, pay_time, update_time, refund_price, refund_time,
                 settled, settle_profit, customer_id, raw)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (platform, oid,
                 str(r.get("smstitle") or r.get("item_title") or ""),
                 float(r.get("payprice") or r.get("pay_price") or 0),
                 float(r.get("profit") or r.get("pub_share_fee") or 0),
                 status_key,
                 ORDER_STATUS.get(status_key, status_key),
                 str(r.get("type") or ""),
                 _norm_time(r.get("paytime") or r.get("tk_paid_time") or ""),
                 _norm_time(r.get("update_time") or r.get("tk_earning_time") or ""),
                 float(r.get("refundprice") or 0),
                 _norm_time(r.get("refundtime") or ""),
                 1 if str(r.get("is_jiesuan")) == "1" else 0,
                 float(r.get("jiesuan_profit") or 0),
                 _norm_customer(r),
                 json.dumps(r, ensure_ascii=False, default=str)))
            if not exists:
                new_cnt += 1
        self.db.commit()
        return new_cnt

    def query_orders(self, platform="", status="", q="", start="", end="",
                     page=1, page_size=20):
        where, args = [], []
        if platform:
            where.append("platform=?")
            args.append(platform)
        if status:
            where.append("status=?")
            args.append(status)
        if q:
            where.append("(title LIKE ? OR orderid LIKE ? OR customer_id LIKE ?)")
            args.extend([f"%{q}%"] * 3)
        if start:
            where.append("pay_time >= ?")
            args.append(start)
        if end:
            where.append("pay_time <= ?")
            args.append(end)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(
            f"SELECT COUNT(*) FROM orders{cond}", args).fetchone()[0]
        rows = self.db.execute(
            f"SELECT * FROM orders{cond} ORDER BY pay_time DESC LIMIT ? OFFSET ?",
            args + [page_size, (page - 1) * page_size]).fetchall()
        return total, [dict(r) for r in rows]

    def stats(self):
        row = self.db.execute("""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(profit),0) AS profit_total,
                   COALESCE(SUM(CASE WHEN settled=1 THEN settle_profit ELSE 0 END),0) AS profit_settled,
                   COALESCE(SUM(CASE WHEN pay_time>=date('now','localtime') THEN profit ELSE 0 END),0) AS profit_today,
                   COALESCE(SUM(CASE WHEN pay_time>=date('now','localtime') THEN 1 ELSE 0 END),0) AS cnt_today,
                   COALESCE(SUM(refund_price>0),0) AS cnt_refund
            FROM orders""").fetchone()
        by_platform = [dict(r) for r in self.db.execute(
            "SELECT platform, COUNT(*) AS cnt, COALESCE(SUM(profit),0) AS profit "
            "FROM orders GROUP BY platform ORDER BY cnt DESC")]
        return {**dict(row), "by_platform": by_platform}

    # ---- 绑定 ----
    def get_bind(self, user_key: str) -> Optional[str]:
        r = self.db.execute("SELECT bind_id FROM bindings WHERE user_key=?",
                            (user_key,)).fetchone()
        return r["bind_id"] if r else None

    def set_bind(self, user_key: str, bind_id: str):
        self.db.execute(
            "INSERT OR REPLACE INTO bindings VALUES (?,?,?)",
            (user_key, bind_id, now_str()))
        self.db.commit()

    def all_bindings(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM bindings")]

    def user_rebate(self, bind_id: str):
        row = self.db.execute("""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(profit),0) AS total,
                   COALESCE(SUM(CASE WHEN settled=1 THEN settle_profit ELSE 0 END),0) AS settled
            FROM orders WHERE customer_id=?""", (str(bind_id),)).fetchone()
        return dict(row)

    def user_recent_orders(self, bind_id: str, limit=10):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders WHERE customer_id=? ORDER BY pay_time DESC LIMIT ?",
            (str(bind_id), limit))]

    def close(self):
        self.db.close()


def _norm_time(t) -> str:
    """统一时间格式为 YYYY-MM-DD HH:MM:SS"""
    s = str(t or "").strip()
    if not s:
        return ""
    s = s.replace("/", "-")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} 00:00:00" if m else s


def _norm_customer(r: dict) -> str:
    """提取返利归因 ID：美团/联盟用 customer_id_ztk，淘宝用 special_id/relation_id"""
    for k in ("customer_id_ztk", "special_id", "relation_id"):
        v = r.get(k)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def _jd_rows(data: dict) -> list:
    """解析京东订单查询返回：queryResult 为（可能二次编码的）JSON，
    orderRowResp 可能是单个对象或数组"""
    node = data.get("jd_union_open_order_row_query_response") or {}
    qr = node.get("queryResult")
    if isinstance(qr, str):
        try:
            qr = json.loads(qr)
        except (ValueError, TypeError):
            qr = {}
    rows = ((qr or {}).get("data") or {}).get("orderRowResp") or []
    if isinstance(rows, dict):
        rows = [rows]
    return OrderStore._normalize_rows(rows)


def _jd_norm_rows(rows) -> list:
    """把京东订单字段映射为本插件统一结构，复用订单入库逻辑。
    归因字段：positionId（自定义推广位）优先，其次 subUnionId"""
    out = []
    for r in OrderStore._normalize_rows(rows):
        code = str(r.get("validCode") or "").strip()
        status = JD_VALID_STATUS.get(code) or ("9" if code else "")
        pos = str(r.get("positionId") or "").strip()
        out.append({
            **r,
            "orderid": r.get("orderId") or r.get("id") or "",
            "smstitle": r.get("skuName") or "",
            "payprice": r.get("estimateCosPrice") or r.get("price") or 0,
            "profit": r.get("estimateFee") or 0,
            "status": status,
            "status_text": (JD_INVALID_CODE.get(code) or JD_STATUS_TEXT.get(code)
                            or ORDER_STATUS.get(status, "")),
            "type": "京东",
            "paytime": r.get("orderTime") or "",
            "update_time": r.get("modifyTime") or "",
            "is_jiesuan": "1" if code == "18" else "0",
            "jiesuan_profit": r.get("estimateFee") if code == "18" else 0,
            "customer_id_ztk": pos if pos not in ("", "0") else str(r.get("subUnionId") or ""),
        })
    return out


# ---------- cron（5 字段：分 时 日 月 周） ----------
def _parse_cron_field(field: str, lo: int, hi: int) -> Optional[set]:
    """解析单个 cron 字段，返回取值集合；None 表示 *（任意值）。
    支持 * , - / 语法，如 */5、1-5、1,15、8-18/2。"""
    field = str(field).strip().lower()
    if field in ("*", "?", ""):
        return None
    vals = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 字段含空段: '{field}'")
        step = 1
        rng = part
        if "/" in part:
            rng, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"cron 步长非法: '{part}'")
            if step <= 0:
                raise ValueError(f"cron 步长须为正整数: '{part}'")
        try:
            if rng in ("*", "?", ""):
                start, end = lo, hi
            elif "-" in rng:
                a, b = rng.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = int(rng)
                end = hi if "/" in part else start
        except ValueError:
            raise ValueError(f"cron 字段含非数字: '{part}'")
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron 字段超出范围 [{lo}-{hi}]: '{field}'")
        vals.update(range(start, end + 1, step))
    return vals


def _cron_match(expr: str, dt: Optional[datetime] = None) -> bool:
    """判断 datetime 是否命中 5 字段 cron 表达式（本地时间）。
    语法错误抛 ValueError。星期字段 0/7=周日，1-6=周一到周六。"""
    dt = dt or datetime.now()
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError("cron 需 5 个字段（分 时 日 月 周），如: 30 8 * * *")
    mins = _parse_cron_field(fields[0], 0, 59)
    hrs = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    mons = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 7)
    if dows is not None:
        dows = {0 if v == 7 else v for v in dows}
    if mins is not None and dt.minute not in mins:
        return False
    if hrs is not None and dt.hour not in hrs:
        return False
    if mons is not None and dt.month not in mons:
        return False
    cron_dow = (dt.weekday() + 1) % 7  # Python 周一=0 -> cron 周日=0
    dom_hit = doms is None or dt.day in doms
    dow_hit = dows is None or cron_dow in dows
    if doms is not None and dows is not None:
        return dom_hit or dow_hit  # 标准 cron：日与周都受限时取并集
    return dom_hit and dow_hit


_DOW_NAMES = ["日", "一", "二", "三", "四", "五", "六"]


def _describe_cron(expr: str) -> str:
    """常用 cron 表达式的简短中文描述，无法归纳时原样返回"""
    fields = str(expr).split()
    if len(fields) != 5:
        return str(expr)
    m, h, dom, mon, dow = fields
    try:
        t = f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return str(expr)
    if dom == "*" and mon == "*":
        if dow == "*":
            return f"每天 {t}"
        dows = _parse_cron_field(dow, 0, 7)
        if dows:
            days = sorted({0 if v == 7 else v for v in dows})
            if days == [1, 2, 3, 4, 5]:
                return f"工作日 {t}"
            return "每周" + "、".join(_DOW_NAMES[d] for d in days) + f" {t}"
    return str(expr)


def _kind_name(kind: str) -> str:
    return {"meituan": "美团红包", "eleme": "闪购红包", "orders": "订单日报"}.get(
        kind, kind)


# 聊天指令总表（与下方 @filter.command 注册保持同步，使用说明页动态读取展示）
CHAT_COMMANDS = [
    {"cmd": "ztk帮助", "alias": ["返利帮助"], "desc": "查看全部指令", "admin": False},
    {"cmd": "绑定", "alias": [], "desc": "获取返利ID，之后订单自动归因", "admin": False},
    {"cmd": "我的返利", "alias": [], "desc": "查询自己的绑定与返利汇总", "admin": False},
    {"cmd": "查订单", "alias": [], "desc": "查询自己的最近订单", "admin": False},
    {"cmd": "美团红包", "alias": [], "desc": "获取美团外卖红包", "admin": False},
    {"cmd": "闪购红包", "alias": ["饿了么红包"], "desc": "获取饿了么/淘宝闪购红包", "admin": False},
    {"cmd": "京东红包", "alias": ["京东外卖红包", "京东外卖"], "desc": "获取京东外卖红包", "admin": False},
    {"cmd": "淘宝转链", "alias": [], "desc": "淘宝转链 内容", "admin": False},
    {"cmd": "抖音转链", "alias": [], "desc": "抖音转链 内容", "admin": False},
    {"cmd": "京东转链", "alias": ["京东"], "desc": "京东转链 内容", "admin": False},
    {"cmd": "搜抖音", "alias": [], "desc": "搜抖音 关键词", "admin": False},
    {"cmd": "回溯订单", "alias": [], "desc": "手动回拉历史订单", "admin": True},
    {"cmd": "同步订单", "alias": [], "desc": "手动同步一次订单", "admin": True},
]


@register(
    PLUGIN_NAME,
    "Cupid",
    "折淘客全平台返利助手：美团外卖/闪购、饿了么（淘宝闪购）、淘宝、抖音订单与佣金查询，含 WebUI 仪表盘",
    "1.0.0",
)
class ZheTaoKeFanliPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.client: Optional[ZtkClient] = None
        self.store: Optional[OrderStore] = None
        self.web_runner: Optional[web.AppRunner] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._sync_lock = asyncio.Lock()
        self._backfill_running = False  # 手动回溯与启动回溯互斥
        self._last_push: dict = {}  # orderid -> True，推送去重
        self._push_loop_task = None  # 定时推送任务
        self._push_sent: dict = {}  # 推送任务key -> 已发送日期，防重复

    # ---------- 生命周期 ----------
    async def initialize(self):
        self.client = ZtkClient(self._cfg("appkey", ""))
        await self.client.initialize()
        self.store = OrderStore()
        if _WEB_API_AVAILABLE:
            self._register_web_apis()
        if self._cfg("web_enabled", True):
            await self._start_web()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._push_loop_task = asyncio.create_task(self._scheduled_push_loop())

    def _register_web_apis(self):
        """注册 AstrBot 插件页面后端 API（WebUI「插件页面」入口）"""
        prefix = f"/{PLUGIN_NAME}"
        try:
            self.context.register_web_api(f"{prefix}/stats", self._api_stats, ["GET"], "数据总览")
            self.context.register_web_api(f"{prefix}/orders", self._api_orders, ["GET"], "订单查询")
            self.context.register_web_api(f"{prefix}/bindings", self._api_bindings, ["GET"], "用户绑定与返利")
            self.context.register_web_api(f"{prefix}/convert", self._api_convert, ["GET"], "转链工具")
            self.context.register_web_api(f"{prefix}/shorten", self._api_shorten, ["GET"], "短链接生成")
            self.context.register_web_api(f"{prefix}/commands", self._api_commands, ["GET"], "聊天指令列表")
            self.context.register_web_api(f"{prefix}/config", self._api_config, ["GET"], "读取配置")
            self.context.register_web_api(f"{prefix}/config/save", self._api_config_save, ["GET"], "保存配置")
            self.context.register_web_api(f"{prefix}/sync", self._api_sync,
                                          ["GET", "POST"], "手动同步订单")
            self.context.register_web_api(f"{prefix}/push/tasks", self._api_push_tasks,
                                          ["GET"], "定时推送任务列表")
            self.context.register_web_api(f"{prefix}/push/save", self._api_push_save,
                                          ["GET", "POST"], "保存定时推送任务")
            self.context.register_web_api(f"{prefix}/push/delete", self._api_push_delete,
                                          ["GET", "POST"], "删除定时推送任务")
            self.context.register_web_api(f"{prefix}/push/toggle", self._api_push_toggle,
                                          ["GET", "POST"], "启停定时推送任务")
            self.context.register_web_api(f"{prefix}/push/once", self._api_push_once,
                                          ["GET", "POST"], "手动推送一次")
            self.context.register_web_api(f"{prefix}/push/platforms", self._api_push_platforms,
                                          ["GET"], "AstrBot 平台实例列表")
            self.context.register_web_api(f"{prefix}/push/targets", self._api_push_targets,
                                          ["GET"], "读取群列表/好友列表")
        except Exception as e:
            logger.warning(f"[ztk] 插件页面 API 注册失败（独立 WebUI 不受影响）: {e}")

    # ---------- 插件页面 API handlers ----------
    async def _api_stats(self):
        return json_response(self.store.stats())

    async def _api_orders(self):
        q = web_request.query
        try:
            page = max(1, int(q.get("page", "1") or 1))
            page_size = min(100, max(1, int(q.get("page_size", "20") or 20)))
        except ValueError:
            page, page_size = 1, 20
        total, rows = self.store.query_orders(
            platform=q.get("platform", ""), status=q.get("status", ""),
            q=q.get("q", ""),
            start=self._norm_time_arg(q.get("start", "")),
            end=self._norm_time_arg(q.get("end", ""), is_end=True),
            page=page, page_size=page_size)
        rate = self._rebate_rate()
        for r in rows:
            r["rebate"] = round(r["profit"] * rate, 2)
        return json_response({"total": total, "rows": rows, "rebate_rate": rate})

    async def _api_bindings(self):
        rows = []
        for b in self.store.all_bindings():
            info = self.store.user_rebate(b["bind_id"])
            rows.append({**b, **info,
                         "rebate": round(info["total"] * self._rebate_rate(), 2)})
        return json_response({"rows": rows, "rebate_rate": self._rebate_rate()})

    async def _api_convert(self):
        res = await self._do_convert(
            web_request.query.get("platform", ""),
            web_request.query.get("content", "").strip(),
            web_request.query.get("bind_id", ""))
        return json_response(res)

    async def _api_shorten(self):
        res = await self._shorten(
            web_request.query.get("url", ""),
            web_request.query.get("engine", "sina"))
        return json_response(res)

    async def _api_commands(self):
        """使用说明页动态读取的聊天指令列表（与实际注册指令同源）"""
        return json_response({"commands": CHAT_COMMANDS})

    # ---------- 配置读写（内置配置页） ----------
    @staticmethod
    def _norm_time_arg(val: str, is_end: bool = False) -> str:
        """订单时间筛选智能解析：支持 2026-09-14 / 20260914 / 202609(整月) /
        2026-9(整月) / 2026(全年) / 含时分秒的完整时间；返回可做字符串比较的
        'YYYY-MM-DD HH:MM:SS'。start 取当期起点，end 取当期终点。"""
        s = str(val or "").strip()
        if not s:
            return ""
        # 拆出时分秒部分（如 2026-09-14 10:00:00），日期部分解析成功后原样保留
        time_part = ""
        m = re.search(r"[ T](\d{1,2}:\d{2}(:\d{2})?)?$", s)
        if m:
            time_part = m.group(0).strip()
            s = s[:m.start()]
        d = (s.replace("/", "-").replace("年", "-").replace("月", "-")
             .replace("日", "").replace(".", "-").strip().strip("-"))
        parts = [p for p in d.split("-") if p != ""]
        if not parts or not all(p.isdigit() for p in parts):
            return ""
        try:
            if len(parts) == 1 and len(parts[0]) == 8:
                parts = [parts[0][:4], parts[0][4:6], parts[0][6:]]
            elif len(parts) == 1 and len(parts[0]) == 6:
                parts = [parts[0][:4], parts[0][4:]]
            y = int(parts[0])
            if not 2015 <= y <= 2100:                        # 年份合理范围
                return ""
            if len(parts) == 1:                              # 年 → 全年
                return (f"{y}-12-31 23:59:59" if is_end
                        else f"{y:04d}-01-01 00:00:00")
            m = int(parts[1])
            if not 1 <= m <= 12:
                return ""
            last = calendar.monthrange(y, m)[1]
            if len(parts) == 2:                              # 年月 → 整月
                return (f"{y:04d}-{m:02d}-{last:02d} 23:59:59" if is_end
                        else f"{y:04d}-{m:02d}-01 00:00:00")
            dd = int(parts[2])                               # 年月日 → 整天
            if not 1 <= dd <= last:
                return ""
            if time_part:                                    # 带时分秒 → 原样保留
                return f"{y:04d}-{m:02d}-{dd:02d} {time_part}"
            return (f"{y:04d}-{m:02d}-{dd:02d} 23:59:59" if is_end
                    else f"{y:04d}-{m:02d}-{dd:02d} 00:00:00")
        except (ValueError, TypeError):
            pass
        return ""

    def _load_schema(self) -> dict:
        try:
            path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _schema_items(self) -> dict:
        """展平 schema → {key: item_schema}"""
        out = {}
        for g in self._load_schema().values():
            for k, item in ((g or {}).get("items") or {}).items():
                out[k] = item or {}
        return out

    def _cfg_holder(self, key):
        """定位 key 实际存放的 dict（顶层或分组），没有则返回 None"""
        if key in self.config:
            return self.config
        for v in self.config.values():
            if isinstance(v, dict) and key in v:
                return v
        return None

    def _coerce_cfg(self, key: str, item: dict, raw):
        t = str(item.get("type") or "string").lower()
        s = "" if raw is None else str(raw)
        if t == "bool":
            return s.strip().lower() in ("1", "true", "on", "yes", "是")
        if t in ("int", "float"):
            s = s.strip()
            if s == "":
                return item.get("default", 0)
            try:
                num = int(float(s)) if t == "int" else float(s)
            except (TypeError, ValueError):
                raise ValueError(f"「{item.get('description') or key}」需填写数字")
            lo, hi = item.get("min"), item.get("max")
            desc = item.get("description") or key
            if lo is not None and num < lo:
                raise ValueError(f"「{desc}」不能小于 {lo}")
            if hi is not None and num > hi:
                raise ValueError(f"「{desc}」不能大于 {hi}")
            return num
        if t == "list":
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            parts = re.split(r"[\n,，;；]", s)
            return [p.strip() for p in parts if p.strip()]
        return s

    async def _api_config(self):
        return json_response(self._config_data())

    def _config_data(self) -> dict:
        schema = self._load_schema()
        values = {}
        for g in schema.values():
            for k, item in ((g or {}).get("items") or {}).items():
                values[k] = self._cfg(k, (item or {}).get("default", ""))
        return {"schema": schema, "values": values}

    async def _api_config_save(self):
        try:
            payload = json.loads(web_request.query.get("data", "") or "{}")
        except (ValueError, TypeError):
            return json_response({"error": "data 参数不是合法 JSON"})
        if not isinstance(payload, dict):
            return json_response({"error": "data 参数格式错误"})
        items = self._schema_items()
        saved = 0
        for k, v in payload.items():
            item = items.get(k)
            if item is None:
                continue  # 未知配置项忽略，防止误写
            try:
                val = self._coerce_cfg(k, item, v)
            except ValueError as e:
                return json_response({"error": str(e)})
            holder = self._cfg_holder(k)
            if holder is None:
                self.config[k] = val
            else:
                holder[k] = val
            saved += 1
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:
                return json_response({"error": f"保存失败: {e}"})
        return json_response({"ok": True, "saved": saved})

    async def _api_sync(self):
        result = await self.sync_orders()
        return json_response({"ok": True, "result": result})

    # ---------- 定时推送 API ----------
    def _push_params(self) -> dict:
        """插件页面 API 参数（aiohttp request query；POST 场景也统一走 query）"""
        q = web_request.query
        return {k: q.get(k) for k in q.keys()}

    async def _api_push_tasks(self):
        tasks = []
        for t in self._load_push_tasks():
            row = {**t}
            row["desc"] = _describe_cron(str(t.get("cron") or ""))
            row["kind_name"] = _kind_name(str(t.get("kind") or ""))
            tasks.append(row)
        legacy = []
        for t in self._legacy_push_tasks():
            t["desc"] = _describe_cron(str(t.get("cron") or ""))
            t["kind_name"] = _kind_name(str(t.get("kind") or ""))
            legacy.append(t)
        return json_response({"tasks": tasks, "legacy_tasks": legacy})

    async def _api_push_save(self):
        data = self._push_params()
        cron = str(data.get("cron") or "").strip()
        try:
            _cron_match(cron, datetime.now())
        except ValueError as e:
            return json_response({"error": f"cron 表达式无效: {e}"})
        platform = str(data.get("platform") or "").strip()
        target_type = str(data.get("target_type") or "").strip()
        target_id = str(data.get("target_id") or "").strip()
        kind = str(data.get("kind") or "").strip().lower()
        bind_id = str(data.get("bind_id") or "").strip()
        if not platform:
            return json_response({"error": "请选择平台"})
        if target_type not in ("group", "private"):
            return json_response({"error": "会话类型须为 group 或 private"})
        if not target_id:
            return json_response({"error": "请填写或选择推送目标 ID"})
        if kind not in ("meituan", "eleme", "jd", "orders"):
            return json_response({"error": "推送类型须为 meituan/eleme/jd/orders"})
        if bind_id and not re.fullmatch(r"\d{1,11}", bind_id):
            return json_response({"error": "返利ID须为 11 位以内纯数字"})
        raw_enabled = str(data.get("enabled") or "").strip().lower()
        enabled = raw_enabled not in ("0", "false", "no", "off") \
            if raw_enabled else None  # 未提供时保留原状态
        tasks = self._load_push_tasks()
        tid = str(data.get("id") or "").strip()
        task = {
            "id": tid or f"t{int(time.time() * 1000):x}",
            "cron": " ".join(cron.split()),
            "platform": platform, "target_type": target_type,
            "target_id": target_id, "kind": kind,
            "bind_id": bind_id, "enabled": True,
            "created_at": now_str(),
        }
        replaced = False
        for i, old in enumerate(tasks):
            if old.get("id") == task["id"]:
                task["created_at"] = old.get("created_at") or task["created_at"]
                task["enabled"] = old.get("enabled", True) if enabled is None else enabled
                tasks[i] = task
                replaced = True
                break
        if not replaced:
            task["enabled"] = True if enabled is None else enabled
            tasks.append(task)
        self._write_push_tasks(tasks)
        return json_response({"ok": True, "task": task, "replaced": replaced})

    async def _api_push_delete(self):
        tid = str(self._push_params().get("id") or "").strip()
        tasks = self._load_push_tasks()
        remain = [t for t in tasks if t.get("id") != tid]
        if len(remain) == len(tasks):
            return json_response({"error": f"任务不存在: {tid}"})
        self._write_push_tasks(remain)
        return json_response({"ok": True})

    async def _api_push_toggle(self):
        tid = str(self._push_params().get("id") or "").strip()
        tasks = self._load_push_tasks()
        hit = None
        for t in tasks:
            if t.get("id") == tid:
                t["enabled"] = not t.get("enabled", True)
                hit = t
                break
        if not hit:
            return json_response({"error": f"任务不存在: {tid}"})
        self._write_push_tasks(tasks)
        return json_response({"ok": True, "task": hit})

    async def _api_push_once(self):
        """手动推送一次（忽略 cron，立即按任务配置发送）"""
        tid = str(self._push_params().get("id") or "").strip()
        for t in self._load_push_tasks() + self._legacy_push_tasks():
            if t.get("id") == tid:
                err = await self._send_push(t)
                return json_response(
                    {"error": err} if err else {"ok": True})
        return json_response({"error": f"任务不存在: {tid}"})

    async def _api_push_platforms(self):
        """读取 AstrBot 当前配置并运行中的平台适配器实例"""
        out = []
        insts = []
        try:
            insts = list(self.context.platform_manager.platform_insts)
        except Exception as e:
            logger.warning(f"[ztk] 读取平台实例列表失败: {e}")
        for inst in insts:
            try:
                s = inst.get_stats()
            except Exception:
                try:
                    meta = inst.meta()
                    s = {"id": meta.id, "type": meta.name,
                         "display_name": meta.name, "status": "running", "meta": {}}
                except Exception:
                    continue
            out.append({
                "id": s.get("id") or "",
                # 实例名称（用户在面板里设置的名字，= meta().id = config["id"]）
                "name": s.get("id") or "",
                "type": s.get("type") or "",
                "display_name": s.get("display_name") or s.get("type") or s.get("id"),
                "status": s.get("status") or "unknown",
                "proactive": bool((s.get("meta") or {}).get(
                    "support_proactive_message", True)),
            })
        # 附带机器人账号信息（OneBot 平台读登录账号，让「选哪个机器人」一目了然）
        for item in out:
            pid = item["id"]
            try:
                inst = self.context.get_platform_inst(pid)
                client = inst.get_client() if inst else None
                if client is not None and hasattr(client, "call_action"):
                    info = await asyncio.wait_for(
                        client.call_action("get_login_info"), timeout=5)
                    if isinstance(info, dict) and info.get("user_id"):
                        item["bot_id"] = str(info.get("user_id"))
                        item["bot_name"] = str(
                            info.get("nickname") or item["bot_id"])
            except Exception:
                pass  # 非 OneBot 平台或未连接，忽略
        return json_response({"platforms": out})

    async def _api_push_targets(self):
        """读取指定平台的群列表/好友列表（aiocqhttp/NapCat 等 OneBot 平台支持）"""
        q = web_request.query
        pid = str(q.get("platform_id") or "").strip()
        ttype = str(q.get("target_type") or "group").strip()
        if ttype not in ("group", "private"):
            ttype = "group"
        inst = None
        try:
            inst = self.context.get_platform_inst(pid)
        except Exception:
            pass
        if inst is None:
            return json_response({"error": f"未找到平台实例: {pid}，请刷新平台列表"})
        client = None
        try:
            client = inst.get_client()
        except Exception:
            pass
        if client is None or not hasattr(client, "call_action"):
            return json_response(
                {"error": "该平台不支持自动读取会话列表（仅 aiocqhttp/NapCat 等 "
                          "OneBot 平台支持），可直接手动填写 ID"})
        action = "get_group_list" if ttype == "group" else "get_friend_list"
        try:
            rows = await client.call_action(action)
        except Exception as e:
            return json_response({"error": f"读取会话列表失败（机器人未连接？）: {e}"})
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if ttype == "group":
                out.append({"id": str(r.get("group_id") or ""),
                            "name": str(r.get("group_name") or r.get("group_id") or "")})
            else:
                out.append({"id": str(r.get("user_id") or ""),
                            "name": str(r.get("remark") or r.get("nickname")
                                        or r.get("user_id") or "")})
        out = [t for t in out if t["id"]]
        return json_response({"targets": out})

    async def terminate(self):
        for t in (self._poll_task, self._push_loop_task):
            if t:
                t.cancel()
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.client:
            await self.client.terminate()
        if self.store:
            self.store.close()

    # ---------- 配置快捷读取 ----------
    def _cfg(self, key, default=""):
        """读取配置项：兼容顶层扁平与 object 分组嵌套两种布局。

        AstrBot 的 _conf_schema.json 中 type=object 的分组会以嵌套 dict
        传给插件（config["分组名"]["子项"]），此方法先查顶层，再遍历所有
        分组查找，第一个非空值生效。
        """
        candidates = []
        if key in self.config:
            candidates.append(self.config[key])
        for v in self.config.values():
            if isinstance(v, dict) and key in v:
                candidates.append(v[key])
        for c in candidates:
            if c is not None and c != "":
                return c
        return default

    def _sid(self):
        return self._cfg("sid", "")

    def _pid(self):
        return self._cfg("pid", "")

    def _jd_union_id(self):
        return str(self._cfg("jd_union_id", "") or "").strip()

    def _jd_attribution(self, bind_id: str = "") -> tuple:
        """京东返利归因：返回 (positionId, subUnionId)。
        positionId 只接受数字（自定义推广位，订单里会透出），
        所以纯数字的返利ID直接用作推广位；非数字时回退到配置的默认推广位，
        同时把返利ID放进 subUnionId（支持字母/数字/下划线，订单里同样透出）"""
        bid = str(bind_id or "").strip()[:80]
        pos = bid if (bid.isdigit() and len(bid) <= 11) else \
            str(self._cfg("jd_position_id", "") or "").strip()
        return pos, bid

    @staticmethod
    def _jd_parse(res: dict) -> dict:
        """解析京东转链返回：官方结果在 *_response.result 里且是二次编码的 JSON 字符串"""
        node = res.get("jd_union_open_promotion_byunionid_get_response") or {}
        result = node.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                result = None
        result = result if isinstance(result, dict) else {}
        data = result.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        return {
            "link": str(data.get("shortURL") or data.get("clickURL") or ""),
            "long_link": str(data.get("clickURL") or ""),
            "code": result.get("code") or node.get("code"),
            "message": result.get("message") or "",
        }

    def _jd_act_url(self) -> str:
        """京东红包/活动推广链接（京东外卖活动等），未配置时用内置默认活动"""
        return str(self._cfg("jd_act_url", "") or "").strip() or JD_DEFAULT_ACT_URL

    def _jd_wechat_type(self) -> str:
        """京东转链的微信小程序短链类型：''=不处理，1=京小街，2=京东购物"""
        m = str(self._cfg("jd_wechat_type", "off") or "off").strip().lower()
        return m if m in ("1", "2") else ""

    async def _jd_rp(self, bind_id: str = "", material_id: str = "") -> dict:
        """京东红包（活动转链）：把京东活动链接转成带返利归因的推广链接。
        京东侧没有小程序码/二维码出图能力，返回的是可点击的推广短链。"""
        if not self._jd_union_id():
            return {"ok": False, "error": "未配置京东联盟ID（插件配置「折淘客凭据」→ 京东联盟ID）"}
        material = str(material_id or "").strip() or self._jd_act_url()
        if not material:
            return {"ok": False, "error": "未配置京东红包活动链接（jd_act_url）"}
        pos, sub = self._jd_attribution(bind_id)
        try:
            res = await self.client.jd_convert(
                material, self._jd_union_id(), pos, sub_union_id=sub,
                wechat_type=self._jd_wechat_type())
        except Exception as e:
            return {"ok": False, "error": f"转链出错: {e}"}
        d = self._jd_parse(res)
        if d.get("link"):
            return {"ok": True, "link": d["link"],
                    "long_link": d.get("long_link") or ""}
        return {"ok": False,
                "error": d.get("message") or f"转链失败（{d.get('code') or '未知错误'}）"}

    def _rebate_rate(self):
        try:
            return float(self._cfg("rebate_rate", 0.3))
        except (TypeError, ValueError):
            return 0.3

    # ---------- 定时轮询 ----------
    async def _poll_loop(self):
        # 历史订单回溯（官网老订单不会落入增量窗口，需按天往回拉）
        self._backfill_running = True
        try:
            await self._backfill_orders(
                int(self._cfg("backfill_days", 7) or 0))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[ztk] 历史订单回溯失败: {e}")
        finally:
            self._backfill_running = False
        while True:
            try:
                await self.sync_orders()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ztk] 订单轮询异常: {e}")
            interval = max(2, int(self._cfg("poll_interval_minutes", 5) or 5))
            await asyncio.sleep(interval * 60)

    async def _backfill_orders(self, days: int):
        """按天回溯历史订单（受折淘客限速约束，调用间隔由客户端限速；
        730 天约 1500 次调用，2 秒/次约需 50 分钟，属一次性成本）"""
        days = max(0, min(days, 730))
        if not days or not self._cfg("appkey"):
            return
        total = 0
        logger.info(f"[ztk] 开始回溯最近 {days} 天历史订单..."
                    f"（按 2 秒/次限速预计需要 {days * 4 // 60 + 1} 分钟）")
        for i in range(days, 0, -1):
            end_dt = datetime.now() - timedelta(days=i - 1)
            start_dt = end_dt - timedelta(days=1)
            s = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            e = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            # 美团
            for page in range(1, 6):
                data = await self.client.meituan_orders(
                    query_type="1", page=page, page_size=50,
                    start_time=s, end_time=e)
                content = self.store._normalize_rows(data.get("content") or [])
                total += self.store.upsert_orders("美团", content)
                if len(content) < 50:
                    break
            # 联盟全平台
            for page in range(1, 6):
                data = await self.client.lianmeng_orders(
                    query_type="2", page=page, page_size=50,
                    start_time=s, end_time=e)
                content = self.store._normalize_rows(data.get("content") or [])
                grouped = {}
                for r in content:
                    p = LIANMENG_PLATFORM.get(str(r.get("san_pingtai_id", "")),
                                              r.get("san_pingtai") or "联盟")
                    grouped.setdefault(p, []).append(r)
                for p, rows in grouped.items():
                    total += self.store.upsert_orders(p, rows)
                if len(content) < 50:
                    break
            if i % 30 == 0:
                logger.info(f"[ztk] 回溯进度: 剩余 {i} 天，已入库/更新 {total} 条")
        logger.info(f"[ztk] 历史订单回溯完成，共入库/更新 {total} 条")

    async def sync_orders(self):
        """拉取各平台增量订单并入库，返回 {来源: 新增条数}"""
        async with self._sync_lock:
            if not self._cfg("appkey"):
                return {}
            result = {}
            minutes = max(2, int(self._cfg("poll_interval_minutes", 5) or 5))
            start = (datetime.now() - timedelta(minutes=minutes * 2 + 5)
                     ).strftime("%Y-%m-%d %H:%M:%S")
            end = now_str()

            if self._cfg("poll_meituan", True):
                try:
                    data = await self.client.meituan_orders(
                        query_type="1", page=1, page_size=50,
                        start_time=start, end_time=end)
                    content = data.get("content") or []
                    result["美团"] = self.store.upsert_orders("美团", content)
                except Exception as e:
                    logger.warning(f"[ztk] 美团订单同步失败: {e}")

            if self._cfg("poll_lianmeng", True):
                try:
                    data = await self.client.lianmeng_orders(
                        query_type="2", page=1, page_size=50,
                        start_time=start, end_time=end)
                    # 元素可能仍是 JSON 字符串，先归一再分组
                    content = self.store._normalize_rows(data.get("content") or [])
                    # 按 san_pingtai 分平台入库
                    grouped = {}
                    for r in content:
                        p = LIANMENG_PLATFORM.get(str(r.get("san_pingtai_id", "")),
                                                  r.get("san_pingtai") or "联盟")
                        grouped.setdefault(p, []).append(r)
                    for p, rows in grouped.items():
                        result[p] = result.get(p, 0) + self.store.upsert_orders(p, rows)
                except Exception as e:
                    logger.warning(f"[ztk] 联盟订单同步失败: {e}")

            if self._cfg("poll_taobao", True) and self._sid():
                try:
                    # 折淘客限制：淘宝订单接口单次最多查 3 小时，每 2 分钟 1-3 次
                    data = await self.client.taobao_orders(
                        self._sid(), start, end, query_type="4")
                    dto = (data.get("tbk_sc_order_details_get_response") or {}) \
                        .get("data", {}).get("results", {}) \
                        .get("publisher_order_dto") or []
                    if isinstance(dto, dict):
                        dto = [dto]
                    result["淘宝"] = self.store.upsert_orders("淘宝", dto)
                    if data.get("error_response"):
                        logger.warning(f"[ztk] 淘宝订单接口返回错误: "
                                       f"{data['error_response'].get('sub_msg')}")
                except Exception as e:
                    logger.warning(f"[ztk] 淘宝订单同步失败: {e}")

            if self._cfg("poll_jd", False):
                try:
                    jd_cnt = await self._sync_jd_orders()
                    if jd_cnt:
                        result["京东"] = result.get("京东", 0) + jd_cnt
                except Exception as e:
                    logger.warning(f"[ztk] 京东订单同步失败: {e}")

            new_total = sum(result.values())
            if new_total and self._cfg("push_enabled", False):
                await self._push_new_orders(new_total)
            if new_total:
                logger.info(f"[ztk] 订单同步完成，新增 {new_total} 条: {result}")
            return result

    async def _sync_jd_orders(self, hours: int = 0) -> int:
        """京东订单查询（需京东开放平台应用 appkey/appsecret）。
        接口限制单次时间跨度≤1小时，按 1 小时切片回溯；返回新入库条数"""
        app_key = str(self._cfg("jd_app_key", "") or "").strip()
        app_secret = str(self._cfg("jd_app_secret", "") or "").strip()
        if not (app_key and app_secret):
            logger.info("[ztk] 已开启京东订单同步，但未配置京东开放平台应用的 appkey/appsecret，跳过")
            return 0
        try:
            hours = int(hours or self._cfg("jd_lookback_hours", 3) or 3)
        except (TypeError, ValueError):
            hours = 3
        hours = max(1, min(hours, 24))
        auth_key = str(self._cfg("jd_auth_key", "") or "").strip()
        total = 0
        end_dt = datetime.now().replace(microsecond=0)
        for i in range(hours):
            e = end_dt - timedelta(hours=i)
            s = e - timedelta(hours=1)
            data = await self.client.jd_orders(
                app_key, app_secret, page=1, page_size=100, query_type="3",
                start_time=s.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=e.strftime("%Y-%m-%d %H:%M:%S"), key=auth_key)
            total += self.store.upsert_orders("京东", _jd_norm_rows(_jd_rows(data)))
        return total

    async def _push_new_orders(self, cnt: int):
        for admin in (self._cfg("admin_ids") or []):
            try:
                umo = str(admin)
                if ":" not in umo:
                    continue
                await self.context.send_message(
                    umo, MessageChain().message(
                        f"💰 折淘客返利助手：检测到 {cnt} 笔新订单，回复「我的返利」查看详情"))
            except Exception as e:
                logger.warning(f"[ztk] 推送管理员失败: {e}")

    # ---------- WebUI ----------
    async def _start_web(self):
        app = web.Application()
        app.router.add_get("/", self._web_index)
        app.router.add_get("/api/stats", self._web_stats)
        app.router.add_get("/api/orders", self._web_orders)
        app.router.add_get("/api/bindings", self._web_bindings)
        app.router.add_get("/api/convert", self._web_convert)
        app.router.add_get("/api/shorten", self._web_shorten)
        app.router.add_get("/api/config", self._web_config)
        app.router.add_get("/api/config/save", self._web_config_save)
        app.router.add_post("/api/sync", self._web_sync)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0",
                           int(self._cfg("web_port", 17878) or 17878))
        await site.start()
        logger.info(f"[ztk] WebUI 已启动: http://127.0.0.1:{site._port}/")

    def _check_web_auth(self, request) -> bool:
        pwd = self._cfg("web_password", "") or ""
        if not pwd:
            return True
        return request.query.get("token", "") == pwd

    def _json(self, data, status=200):
        return web.json_response(data, status=status)

    async def _web_index(self, request):
        if not self._check_web_auth(request):
            return web.Response(text="未授权", status=401)
        path = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
        with open(path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def _web_stats(self, request):
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        return self._json(self.store.stats())

    async def _web_orders(self, request):
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        q = request.query
        total, rows = self.store.query_orders(
            platform=q.get("platform", ""), status=q.get("status", ""),
            q=q.get("q", ""),
            start=self._norm_time_arg(q.get("start", "")),
            end=self._norm_time_arg(q.get("end", ""), is_end=True),
            page=max(1, int(q.get("page", 1) or 1)),
            page_size=min(100, max(1, int(q.get("page_size", 20) or 20))))
        rate = self._rebate_rate()
        for r in rows:
            r["rebate"] = round(r["profit"] * rate, 2)
        return self._json({"total": total, "rows": rows, "rebate_rate": rate})

    async def _web_bindings(self, request):
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        rows = []
        for b in self.store.all_bindings():
            info = self.store.user_rebate(b["bind_id"])
            rows.append({**b, **info,
                         "rebate": round(info["total"] * self._rebate_rate(), 2)})
        return self._json({"rows": rows, "rebate_rate": self._rebate_rate()})

    async def _custom_shorturl(self, url: str) -> dict:
        """自定义短链 API：配置里填模板地址，{url}=原文 {url_enc}=URL编码。
        返回文本或 JSON（自动识别 shorturl/short/url/tinyurl 等字段）"""
        tpl = str(self._cfg("shorturl_api", "") or "").strip()
        if not tpl:
            return {}
        api = tpl.replace("{url_enc}", quote(url, safe="")).replace("{url}", url)
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15)) as ses:
                async with ses.get(api) as resp:
                    text = (await resp.text()).strip()
        except Exception as e:
            return {"ok": False, "error": f"自定义短链API请求失败: {e}"}
        if resp.status != 200 or not text:
            return {"ok": False, "error": f"自定义短链API返回异常(HTTP {resp.status})"}
        short = ""
        try:
            data = json.loads(text)
            for k in ("shorturl", "short_url", "shortUrl", "short", "url",
                      "tinyurl", "data"):
                v = data.get(k) if isinstance(data, dict) else None
                if isinstance(v, str) and v.strip():
                    short = v.strip()
                    break
        except (ValueError, TypeError):
            short = text.split()[0] if text else ""
        if short and re.match(r"^https?://", short, re.I):
            return {"ok": True, "short": short, "engine": "custom", "raw": text[:300]}
        return {"ok": False, "error": f"自定义短链API未返回有效链接: {text[:120]}"}

    async def _shorten(self, url: str, engine: str = "sina") -> dict:
        """短链接生成：优先自定义API(若配置 shorturl_api)，否则折淘客 sina/baidu"""
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return {"error": "请传入 http(s) 开头的链接"}
        # 用户配置了自定义短链 API → 优先使用，失败回退折淘客
        if str(self._cfg("shorturl_api", "") or "").strip():
            res = await self._custom_shorturl(url)
            if res.get("ok"):
                return res
            err_custom = res.get("error", "")
        else:
            err_custom = ""
        engine = engine if engine in ("sina", "baidu") else "sina"
        if not self._sid():
            tip = f"（自定义短链API错误: {err_custom}）" if err_custom else ""
            return {"error": f"短链接口需要淘客账号授权SID{tip}，请在插件配置「折淘客凭据 → 淘客账号授权SID」填写，或在「自定义短链API」填入你自己的短链接口"}
        try:
            res = await self.client.shorturl(url, self._sid(), engine)
        except Exception as e:
            err = f"短链生成出错: {e}"
            if err_custom:
                err += f"（自定义短链API错误: {err_custom}）"
            return {"ok": False, "error": err}
        short = str(res.get("shorturl") or "").strip()
        if short:
            return {"ok": True, "short": short, "engine": engine, "raw": res}
        err = (res.get("content") or res.get("error_response")
               or json.dumps(res, ensure_ascii=False))
        msg = f"短链生成失败: {err}"
        if err_custom:
            msg += f"（自定义短链API错误: {err_custom}）"
        else:
            msg += "。折淘客短链不可用时，可在插件配置「折淘客凭据 → 自定义短链API」填入你自己的短链接口"
        return {"ok": False, "error": msg}

    async def _do_convert(self, platform: str, content: str, bind_id: str = "") -> dict:
        """转链核心逻辑，独立 WebUI 与插件页面共用"""
        content = (content or "").strip()
        # 红包/活动转链(meituan/eleme/jd_rp)不需要内容；商品转链才必须传内容
        if not content and platform in ("taobao", "douyin", "jd"):
            return {"error": "content 不能为空"}
        try:
            if platform == "meituan":
                res = await self.client.meituan_convert(
                    self._sid(), self._cfg("meituan_act_id", "33"), bind_id)
                return {"ok": True, "link": res.get("data"),
                        "pic": res.get("wx_mini_pic"), "raw": res}
            if platform == "eleme":
                res = await self.client.eleme_convert(
                    self._sid(), self._cfg("eleme_act_id", "10144"), bind_id)
                data = (res.get("alibaba_alsc_union_eleme_promotion_officialactivity_get_response")
                        or {}).get("data", {})
                return {"ok": True, "raw": data}
            if platform == "taobao":
                if not self._pid():
                    return {"error": "未配置淘宝 PID"}
                res = await self.client.taobao_convert(
                    self._sid(), self._pid(), content, bind_id)
                if res.get("error_response"):
                    return {"ok": False,
                            "error": res["error_response"].get("sub_msg", "转链失败")}
                arr = res.get("content") or []
                return {"ok": bool(arr),
                        "item": arr[0] if arr else None, "raw": res}
            if platform == "douyin":
                res = await self.client.douyin_convert(self._sid(), content, bind_id)
                d = ((res.get("data") or {}).get("data")) or {}
                return {"ok": res.get("code") == 10000,
                        "link": d.get("dy_zlink") or d.get("share_link"),
                        "password": d.get("dy_password"), "raw": d}
            if platform == "jd_rp":
                # 京东红包（活动转链）：content 可选，留空用内置京东外卖活动页
                res = await self._jd_rp(bind_id, content)
                return {"ok": res.get("ok", False),
                        "link": res.get("link"), "error": res.get("error"),
                        "raw": res}
            if platform == "jd":
                if not self._jd_union_id():
                    return {"error": "未配置京东联盟ID（jd_union_id，京东转链必填）"}
                pos, sub = self._jd_attribution(bind_id)
                res = await self.client.jd_convert(
                    content, self._jd_union_id(), pos,
                    str(self._cfg("jd_chain_type", "2") or "2"), sub)
                parsed = self._jd_parse(res)
                return {"ok": bool(parsed["link"]), **parsed, "raw": res}
            return {"error": "未知平台"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _web_convert(self, request):
        """独立 WebUI 转链工具"""
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        res = await self._do_convert(
            request.query.get("platform", ""),
            request.query.get("content", "").strip(),
            request.query.get("bind_id", ""))
        return self._json(res)

    async def _web_shorten(self, request):
        """独立 WebUI 短链接生成"""
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        res = await self._shorten(
            request.query.get("url", ""),
            request.query.get("engine", "sina"))
        return self._json(res)

    async def _web_config(self, request):
        """独立 WebUI 读取配置"""
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        return self._json(self._config_data())

    async def _web_config_save(self, request):
        """独立 WebUI 保存配置（data=JSON 字符串）"""
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        # 复用保存逻辑：临时借用 web_request 上下文
        global web_request
        old = web_request
        web_request = request
        try:
            return self._json(await self._api_config_save())
        finally:
            web_request = old

    async def _web_sync(self, request):
        if not self._check_web_auth(request):
            return self._json({"error": "未授权"}, 401)
        result = await self.sync_orders()
        return self._json({"ok": True, "result": result})

    # ---------- 聊天指令 ----------
    def _bind_key(self, event: AstrMessageEvent) -> str:
        # 平台类型(get_platform_name) + 平台实例ID(get_platform_id) + 发送者，避免多平台/多实例撞号
        return f"{event.get_platform_name()}:{event.get_platform_id()}:{event.get_sender_id()}"

    def _get_bind_or_auto(self, event: AstrMessageEvent) -> str:
        key = self._bind_key(event)
        return self.store.get_bind(key) or auto_bind_id(key)

    @filter.command("ztk帮助", alias={"返利帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """折淘客返利助手指令帮助"""

        if self._chat_blocked(event):
            return
        yield event.plain_result(
            "📖 折淘客返利助手\n"
            "绑定 [编号] — 绑定返利ID(11位内数字)，不填自动分配\n"
            "我的返利 — 查询我的订单与预估返利\n"
            "美团红包 — 获取美团外卖红包链接\n"
            "闪购红包 — 获取饿了么/淘宝闪购红包链接\n"
            "京东红包 [活动链接] — 获取京东外卖红包推广链接(不填用默认活动)\n"
            "淘宝转链 [内容] — 淘宝商品高佣转链(淘口令/链接)\n"
            "抖音转链 [链接/口令] — 抖音商品转链\n"
            "京东转链 [链接/口令] — 京东商品/活动(含京东外卖)转链\n"
            "搜抖音 [关键词] — 抖音选品搜索\n"
            "查订单 — 查看自己最近的订单\n"
            "同步订单 — 管理员手动同步(仅管理员)\n"
            "回溯订单 [天数] — 管理员回溯历史订单，如 回溯订单 730\n"
            "会话黑白名单在插件配置「会话与推送」设置；定时推送在 WebUI「插件页面」→"
            "「⏰ 定时推送」可视化管理（cron 定时、平台/群列表自动读取）\n"
            "WebUI: AstrBot 面板左侧「插件页面」→ 折淘客返利助手")

    @filter.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, bind_id: str = ""):
        """绑定返利ID（11位以内纯数字，用于转链归因与佣金分配）"""

        if self._chat_blocked(event):
            return
        key = self._bind_key(event)
        if not bind_id:
            bind_id = self.store.get_bind(key) or auto_bind_id(key)
        if not re.fullmatch(r"\d{1,11}", bind_id):
            yield event.plain_result("❌ 返利ID必须为 11 位以内的纯数字")
            return
        self.store.set_bind(key, bind_id)
        yield event.plain_result(
            f"✅ 绑定成功！你的返利ID: {bind_id}\n"
            f"之后通过本插件转链下单，佣金将归到你名下（返利比例 "
            f"{self._rebate_rate()*100:.0f}%）。回复「我的返利」查看。")

    @filter.command("我的返利")
    async def cmd_my_rebate(self, event: AstrMessageEvent):
        """查询自己的订单与预估返利"""

        if self._chat_blocked(event):
            return
        bind_id = self._get_bind_or_auto(event)
        info = self.store.user_rebate(bind_id)
        if not info["cnt"]:
            yield event.plain_result(
                f"你还没有订单记录（返利ID: {bind_id}）。\n"
                "发送「美团红包」「闪购红包」领取红包，或「淘宝转链/抖音转链 + 商品链接」"
                "转链后下单，订单会自动归到你名下。")
            return
        rebate = round(info["total"] * self._rebate_rate(), 2)
        settled_rebate = round(info["settled"] * self._rebate_rate(), 2)
        if self._use_markdown(event):
            yield self._text_result(event, "\n".join([
                "**💰 我的返利**",
                f"> 返利ID: {bind_id}",
                "",
                f"📦 订单数 **{info['cnt']}** 笔",
                f"🧾 订单佣金合计 **¥{info['total']:.2f}**",
                f"🎁 预估可获返利({self._rebate_rate()*100:.0f}%) **¥{rebate:.2f}**",
                f"✅ 已结算部分返利 **¥{settled_rebate:.2f}**",
            ]))
            return
        yield event.plain_result(
            f"💰 我的返利（ID: {bind_id}）\n"
            f"订单数: {info['cnt']} 笔\n"
            f"订单佣金合计: ¥{info['total']:.2f}\n"
            f"预估可获返利({self._rebate_rate()*100:.0f}%): ¥{rebate:.2f}\n"
            f"其中已结算部分返利: ¥{settled_rebate:.2f}")

    @filter.command("查订单")
    async def cmd_my_orders(self, event: AstrMessageEvent):
        """查看自己名下最近10笔订单"""

        if self._chat_blocked(event):
            return
        bind_id = self._get_bind_or_auto(event)
        rows = self.store.user_recent_orders(bind_id)
        if not rows:
            yield event.plain_result(f"暂无订单记录（返利ID: {bind_id}）")
            return
        if self._use_markdown(event):
            # Markdown 版式：标题加粗 + 每单一条卡片式排版
            items = []
            for i, r in enumerate(rows, 1):
                items.append("\n".join([
                    f"**{i}. {r['title'][:20]}**",
                    f"{r['platform']} · 实付 **¥{r['pay_price']:.2f}**"
                    f" · 佣金 **¥{r['profit']:.2f}**",
                    f"{r['status_text'] or '未知'} · {r['pay_time']}",
                ]))
            yield self._text_result(event, "\n\n".join(
                ["**📋 我最近的订单**", f"> 共 {len(rows)} 笔 · 返利ID: {bind_id}", ""]
                + items))
            return
        lines = ["📋 我最近的订单:"]
        for r in rows:
            lines.append(
                f"· [{r['platform']}] {r['title'][:16]} ¥{r['pay_price']:.2f} "
                f"佣金¥{r['profit']:.2f} {r['status_text'] or '未知'} {r['pay_time']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("美团红包")
    async def cmd_meituan(self, event: AstrMessageEvent, act_id: str = ""):
        """生成美团外卖红包推广链接（带个人返利归因）"""

        if self._chat_blocked(event):
            return
        if not self._sid():
            yield event.plain_result("❌ 管理员未配置折淘客授权SID")
            return
        bind_id = self._get_bind_or_auto(event)
        try:
            res = await self.client.meituan_convert(
                self._sid(), act_id or self._cfg("meituan_act_id", "33"),
                bind_id)
            if res.get("status") == 0 and res.get("data"):
                async for msg in self._reply_redpacket(
                        event,
                        f"🍔 美团外卖红包（你的返利ID: {bind_id}）",
                        str(res.get("data") or ""),
                        str(res.get("wx_mini_pic") or ""),
                        bind_id=bind_id):
                    yield msg
            else:
                yield event.plain_result(f"转链失败: {res.get('des') or res}")
        except Exception as e:
            yield event.plain_result(f"转链出错: {e}")

    @filter.command("闪购红包", alias={"饿了么红包"})
    async def cmd_eleme(self, event: AstrMessageEvent, act_id: str = ""):
        """生成饿了么/淘宝闪购红包推广链接"""

        if self._chat_blocked(event):
            return
        if not self._sid():
            yield event.plain_result("❌ 管理员未配置折淘客授权SID")
            return
        bind_id = self._get_bind_or_auto(event)
        try:
            res = await self.client.eleme_convert(
                self._sid(), act_id or self._cfg("eleme_act_id", "10144"),
                bind_id)
            data = (res.get("alibaba_alsc_union_eleme_promotion_officialactivity_get_response")
                    or {}).get("data") or {}
            link = (data.get("link") or {})
            if data.get("title"):
                h5 = link.get("h5_short_link") or link.get("h5_url", "")
                # 二维码选择：tb=淘宝独立二维码（淘宝闪购活动推荐，外部可打开），
                # wx=微信小程序码（仅微信内可打开）；所选类型为空时自动用另一张兜底
                tb_qr = str(link.get("tb_mini_qrcode") or link.get("tb_qr_code") or "")
                wx_qr = str(link.get("mini_qrcode") or "")
                qr = (tb_qr or wx_qr) if self._eleme_qr_type() == "tb" else (wx_qr or tb_qr)
                async for msg in self._reply_redpacket(
                        event,
                        f"🛵 {data['title']}（你的返利ID: {bind_id}）",
                        str(h5 or ""),
                        qr,
                        extra=(data.get("description") or ""),
                        bind_id=bind_id):
                    yield msg
            else:
                yield event.plain_result(f"转链失败: {res}")
        except Exception as e:
            yield event.plain_result(f"转链出错: {e}")

    @filter.command("京东红包", alias={"京东外卖红包", "京东外卖"})
    async def cmd_jd_redpacket(self, event: AstrMessageEvent, act_url: str = ""):
        """京东红包/京东外卖活动推广链接（带个人返利归因）。
        京东侧无小程序码出图能力，返回推广短链文字；可附带活动链接自定义活动"""

        if self._chat_blocked(event):
            return
        bind_id = self._get_bind_or_auto(event)
        d = await self._jd_rp(bind_id, act_url)
        if not d.get("ok"):
            yield event.plain_result(f"转链失败: {d.get('error')}")
            return
        extra = ("京东外卖天天领红包，先领券再下单更划算"
                 if str(act_url or "").strip() == "" else "")
        mode = self._redpacket_mode()
        async for msg in self._reply_redpacket(
                event,
                f"🛍 京东外卖红包来啦~（你的返利ID: {bind_id}）",
                str(d.get("link") or ""),
                "",  # 京东接口不返回二维码图片，此项恒为空
                extra=extra if mode != "image" else "",
                bind_id=bind_id):
            yield msg

    # ---------- 会话黑白名单 ----------
    def _chat_blocked(self, event: AstrMessageEvent) -> bool:
        """会话过滤：blacklist 模式名单内禁用；whitelist 模式仅名单内可用。
        名单条目可填 群号/QQ号 或完整 UMO，也可填平台名（对该平台全部会话生效）"""
        mode = str(self._cfg("chat_filter_mode", "off") or "off").strip().lower()
        if mode not in ("blacklist", "whitelist"):
            return False
        entries = self._cfg(
            "chat_blacklist" if mode == "blacklist" else "chat_whitelist", []) or []
        if isinstance(entries, str):
            entries = [entries]
        names = {str(e).strip() for e in entries if str(e).strip()}
        if not names:
            return mode == "whitelist"  # 白名单为空=全部禁用；黑名单为空=全放行
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        gid = str(event.get_group_id() or "")
        sid = str(event.get_sender_id() or "")
        pn = str(event.get_platform_name() or "")
        keys = {umo, gid, sid, pn} - {""}
        # 群聊同时用群号匹配；也允许「平台:群:号」形式
        if gid:
            keys.add(f"{pn}:群:{gid}")
        if not gid and sid:
            keys.add(f"{pn}:私聊:{sid}")
        hit = bool(keys & names)
        return hit if mode == "blacklist" else not hit

    # ---------- 定时推送 ----------
    async def _scheduled_push_loop(self):
        """每 20 秒检查一次推送任务（分钟级 cron 匹配，靠去重键防重复发送）"""
        while True:
            try:
                await self._check_push_tasks()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ztk] 定时推送异常: {e}")
            await asyncio.sleep(20)

    def _push_tasks_file(self) -> str:
        return os.path.join(DATA_DIR, "push_tasks.json")

    def _load_push_tasks(self) -> list:
        """读取插件页面里配置的推送任务（JSON 持久化于 data/plugin_data 下）"""
        try:
            with open(self._push_tasks_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return [t for t in data if isinstance(t, dict) and t.get("id")]
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning(f"[ztk] 读取推送任务失败: {e}")
            return []

    def _write_push_tasks(self, tasks: list):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self._push_tasks_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._push_tasks_file())

    def _legacy_push_tasks(self) -> list:
        """兼容旧配置 push_tasks（HH:MM|平台:群或私聊:ID|类型），转为等价 cron 任务"""
        out = []
        for raw in (self._cfg("push_tasks", []) or []):
            raw = str(raw).strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 3 or ":" not in parts[1]:
                continue
            hhmm, target, kind = parts[0], parts[1], parts[2].lower()
            try:
                hh, mm = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
            except (ValueError, IndexError):
                continue
            try:
                p, t, cid = target.split(":", 2)
            except ValueError:
                continue
            if t not in ("群", "私聊") or not cid:
                continue
            cron = f"{mm} {hh} * * *"
            out.append({
                "id": "cfg:" + raw, "cron": cron, "platform": p,
                "target_type": "group" if t == "群" else "private",
                "target_id": cid, "kind": kind, "bind_id": "",
                "enabled": True, "legacy": True, "desc": _describe_cron(cron),
            })
        return out

    async def _send_push(self, t: dict) -> str:
        """按任务立即推送一次，返回错误信息（成功返回空串）。
        定时调度与手动「推送一次」共用；样式随任务平台走 Markdown 规则"""
        umo = (f"{t.get('platform')}:"
               f"{'GroupMessage' if t.get('target_type') == 'group' else 'FriendMessage'}:"
               f"{t.get('target_id')}")
        # 与聊天回复同一套样式规则：auto/markdown 渠道发 Markdown
        md = self._md_wanted(str(t.get("platform") or ""))
        text = await self._build_push_text(
            str(t.get("kind") or ""), bind_id=str(t.get("bind_id") or ""),
            markdown=md)
        if not text:
            return "生成推送内容失败（检查 appkey/活动配置，或查看日志）"
        try:
            chain = MessageChain().message(text)
            if md:
                chain = chain.use_markdown(True)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(f"[ztk] 推送失败({umo}): {e}")
            return str(e)
        logger.info(f"[ztk] 推送完成: {t.get('cron', '手动')} -> {umo}")
        return ""

    async def _check_push_tasks(self):
        now = datetime.now().replace(second=0, microsecond=0)
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        for t in self._load_push_tasks() + self._legacy_push_tasks():
            if t.get("enabled") is False:
                continue
            try:
                if not _cron_match(str(t.get("cron") or ""), now):
                    continue
            except ValueError as e:
                logger.warning(f"[ztk] 推送任务 {t.get('id')} cron 非法: {e}")
                continue
            key = f"{t.get('id')}@{minute_key}"
            if self._push_sent.get(key):
                continue  # 这一分钟已发过
            if await self._send_push(t):
                continue
            self._push_sent[key] = True
        # 清理过期的去重标记
        if len(self._push_sent) > 2000:
            today = now.strftime("%Y-%m-%d")
            self._push_sent = {k: v for k, v in self._push_sent.items()
                               if k.endswith("@" + today)}

    async def _build_push_text(self, kind: str, bind_id: str = "",
                               markdown: bool = False) -> str:
        bind_id = str(bind_id or self._cfg("push_bind_id", "") or "")
        try:
            if kind == "meituan":
                res = await self.client.meituan_convert(
                    self._sid(), self._cfg("meituan_act_id", "33"), bind_id)
                if res.get("status") == 0 and res.get("data"):
                    link = str(res.get("data"))
                    qr = str(res.get("wx_mini_pic") or "")
                    mode = self._redpacket_mode()
                    return self._rp_text(
                        "🍔 每日美团外卖红包来啦~", link,
                        qrcode=qr if mode != "image" else "",
                        bind_id=bind_id, markdown=markdown)
                logger.warning(f"[ztk] 美团红包推送转链失败: {res.get('des') or res}")
                return ""
            if kind == "eleme":
                res = await self.client.eleme_convert(
                    self._sid(), self._cfg("eleme_act_id", "10144"), bind_id)
                data = (res.get("alibaba_alsc_union_eleme_promotion_officialactivity_get_response")
                        or {}).get("data") or {}
                link = (data.get("link") or {})
                h5 = link.get("h5_short_link") or link.get("h5_url", "")
                if data.get("title") and h5:
                    desc = str(data.get("description", "") or "")
                    mode = self._redpacket_mode()
                    return self._rp_text(
                        f"🛵 {data['title']}", h5,
                        extra=desc if mode != "image" else "",
                        bind_id=bind_id, markdown=markdown)
                logger.warning(f"[ztk] 闪购红包推送转链失败: {res}")
                return ""
            if kind == "jd":
                d = await self._jd_rp(bind_id)
                if d.get("ok"):
                    return self._rp_text(
                        "🛍 每日京东外卖红包来啦~", str(d.get("link") or ""),
                        bind_id=bind_id, markdown=markdown)
                logger.warning(f"[ztk] 京东红包推送转链失败: {d.get('error')}")
                return ""
            if kind == "orders":
                s = self.store.stats()
                plats = "，".join(
                    f"{p['platform']}{p['cnt']}单/¥{p['profit']:.2f}"
                    for p in s.get("by_platform", [])[:6]) or "无"
                head = "**📊 返利订单日报**" if markdown else "📊 返利订单日报"
                return (f"{head}\n"
                        f"今日新增 {s.get('cnt_today', 0)} 单，佣金 ¥{s.get('profit_today', 0):.2f}\n"
                        f"累计 {s.get('cnt', 0)} 单，佣金 ¥{s.get('profit_total', 0):.2f}"
                        f"（已结算 ¥{s.get('profit_settled', 0):.2f}）\n"
                        f"分平台：{plats}\n退款 {s.get('cnt_refund', 0)} 单")
            logger.warning(f"[ztk] 未知推送类型: {kind}")
            return ""
        except Exception as e:
            logger.warning(f"[ztk] 生成推送内容失败({kind}): {e}")
            return ""

    def _redpacket_mode(self) -> str:
        m = str(self._cfg("rp_send_mode", "both") or "both").strip().lower()
        return m if m in ("both", "link", "image") else "both"

    def _eleme_qr_type(self) -> str:
        """闪购二维码类型：tb=淘宝独立二维码（默认），wx=微信小程序码"""
        m = str(self._cfg("eleme_qrcode_type", "tb") or "tb").strip().lower()
        return m if m in ("tb", "wx") else "tb"

    def _md_wanted(self, platform_name: str) -> bool:
        """按目标平台名判断是否发 Markdown：markdown=始终；auto=仅平台列表内渠道"""
        m = str(self._cfg("rp_text_style", "plain") or "plain").strip().lower()
        if m == "markdown":
            return True
        if m == "auto":
            plats = self._cfg("rp_md_platforms", []) or []
            if isinstance(plats, str):
                plats = [plats]
            names = {str(p).strip() for p in plats if str(p).strip()}
            return str(platform_name or "").strip() in names
        return False

    def _use_markdown(self, event: AstrMessageEvent) -> bool:
        """是否用 Markdown 发送：markdown=始终；auto=仅配置的平台列表内渠道；
        plain=始终纯文本。不支持渲染的平台无论如何都会退回纯文本"""
        return self._md_wanted(event.get_platform_name())

    def _text_result(self, event: AstrMessageEvent, text: str):
        """按配置的样式发送文本；markdown 仅对支持的平台生效（如 QQ 官方机器人），
        不支持的平台自动退回纯文本"""
        res = event.plain_result(text)
        return res.use_markdown(True) if self._use_markdown(event) else res

    def _render_template(self, tpl: str, mapping: dict) -> str:
        for k, v in mapping.items():
            tpl = tpl.replace("{" + k + "}", v)
        return tpl

    def _rp_text(self, header: str, link: str, qrcode: str = "",
                 extra: str = "", bind_id: str = "",
                 markdown: bool = False) -> str:
        """红包文案（聊天回复与定时推送共用）：配置了 rp_template 则优先用模板，
        占位符 {标题}{描述}{链接}{二维码}{返利ID}，空值占位符整行移除；
        未配置模板时按 markdown 与否给内置版式；二维码行随 rp_send_mode（仅 both）"""
        tpl = str(self._cfg("rp_template", "") or "").strip()
        if tpl:
            vars_ = {
                "标题": header, "描述": extra or "", "链接": link or "",
                "二维码": qrcode or "", "返利ID": bind_id or "",
            }
            lines = []
            for line in tpl.splitlines():
                empty_ph = any("{" + k + "}" in line and not v
                               for k, v in vars_.items())
                if not empty_ph:
                    lines.append(self._render_template(line, vars_))
            return "\n".join(lines)
        mode = self._redpacket_mode()
        if markdown:
            md = [f"**{header}**"]
            if extra:
                md.append(f"> {extra}")
            if link:
                md.append(f"[👉 点我领红包]({link})")
            md.append("下单后订单自动归你名下~")
            if qrcode and mode == "both":
                md.append(f"[二维码]({qrcode})")
            return "\n\n".join(md)
        lines = [header]
        if extra:
            lines.append(extra)
        if link:
            lines.append(f"领取链接: {link}")
        lines.append("下单后订单自动归你名下~")
        if qrcode and mode == "both":
            lines.append(f"二维码: {qrcode}")
        return "\n".join(lines)

    async def _reply_redpacket(self, event: AstrMessageEvent, header: str,
                               link: str, qrcode: str = "", extra: str = "",
                               bind_id: str = ""):
        """红包结果回复，发送方式由配置 rp_send_mode 控制：
        both=文字(含链接)+二维码图片 / link=仅文字 / image=仅二维码图片
        文案模板由 rp_template 控制，留空用内置默认；支持占位符：
        {标题} {描述} {链接} {二维码} {返利ID}"""
        mode = self._redpacket_mode()
        if mode == "image" and qrcode:
            yield event.image_result(qrcode)
            return
        text = self._rp_text(header, link, qrcode, extra, bind_id,
                             markdown=self._use_markdown(event))
        yield self._text_result(event, text)
        if mode == "both" and qrcode:
            yield event.image_result(qrcode)

    @staticmethod
    def _rest_text(event: AstrMessageEvent, default: str = "") -> str:
        """取指令之后的完整文本（淘口令文案可能含空格）"""
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else default

    @filter.command("淘宝转链")
    async def cmd_taobao(self, event: AstrMessageEvent):
        """淘宝商品高佣转链（淘口令/二合一链接/短链接均可）"""

        if self._chat_blocked(event):
            return
        content = self._rest_text(event)
        if not content:
            yield event.plain_result("用法: 淘宝转链 [淘口令或商品链接]")
            return
        if not (self._sid() and self._pid()):
            yield event.plain_result("❌ 管理员未配置折淘客SID或淘宝PID")
            return
        bind_id = self._get_bind_or_auto(event)
        try:
            res = await self.client.taobao_convert(
                self._sid(), self._pid(), quote(content, safe=""), bind_id)
            if res.get("error_response"):
                yield event.plain_result(
                    f"转链失败: {res['error_response'].get('sub_msg', '未知错误')}")
                return
            arr = res.get("content") or []
            if arr:
                item = arr[0]
                yield event.plain_result(
                    f"🛒 {item.get('title', '')}\n"
                    f"券后价: ¥{item.get('quanhou_jiage', '?')} "
                    f"(佣金{item.get('tkrate3', '?')}%)\n"
                    f"淘口令: {item.get('tkl', '')}\n"
                    f"（你的返利ID: {bind_id}，下单后自动归档）")
            else:
                yield event.plain_result("转链失败: 未返回商品数据")
        except Exception as e:
            yield event.plain_result(f"转链出错: {e}")

    @filter.command("抖音转链")
    async def cmd_douyin(self, event: AstrMessageEvent):
        """抖音商品转链（商品链接/口令/短链均可）"""

        if self._chat_blocked(event):
            return
        content = self._rest_text(event)
        if not content:
            yield event.plain_result("用法: 抖音转链 [商品链接或口令]")
            return
        if not self._sid():
            yield event.plain_result("❌ 管理员未配置折淘客授权SID")
            return
        bind_id = self._get_bind_or_auto(event)
        try:
            res = await self.client.douyin_convert(self._sid(), content, bind_id)
            d = ((res.get("data") or {}).get("data")) or {}
            if res.get("code") == 10000:
                yield event.plain_result(
                    f"🎵 抖音转链成功（你的返利ID: {bind_id}）\n"
                    f"链接: {d.get('dy_zlink') or d.get('share_link', '')}\n"
                    f"口令: {d.get('dy_password', '')}")
            else:
                yield event.plain_result(f"转链失败: {res.get('msg') or res}")
        except Exception as e:
            yield event.plain_result(f"转链出错: {e}")

    @filter.command("京东转链", alias={"京东"})
    async def cmd_jd(self, event: AstrMessageEvent):
        """京东转链（商品/活动链接、短链、口令、SKU ID 均可，含京东外卖活动）"""

        if self._chat_blocked(event):
            return
        content = self._rest_text(event)
        if not content:
            yield event.plain_result(
                "用法: 京东转链 [京东商品链接/活动链接/短链/口令/SKU ID]")
            return
        if not self._jd_union_id():
            yield event.plain_result(
                "❌ 管理员未配置京东联盟ID（插件配置「折淘客凭据」→ 京东联盟ID）")
            return
        bind_id = self._get_bind_or_auto(event)
        try:
            d = await self._do_convert("jd", content, bind_id)
            if not d.get("ok"):
                yield event.plain_result(
                    f"转链失败: {d.get('error') or d.get('message') or d}")
                return
            link = str(d.get("link") or "")
            if self._use_markdown(event):
                yield self._text_result(
                    event,
                    f"**🛍 京东转链成功**\n"
                    f"返利ID: `{bind_id}` · 下单后自动归你名下\n\n"
                    f"[👉 点我打开商品/活动]({link})")
            else:
                yield self._text_result(
                    event,
                    f"🛍 京东转链成功（你的返利ID: {bind_id}）\n"
                    f"推广链接: {link}\n"
                    f"下单后订单自动归你名下~")
        except Exception as e:
            yield event.plain_result(f"转链出错: {e}")

    @filter.command("搜抖音")
    async def cmd_douyin_search(self, event: AstrMessageEvent, keyword: str = ""):
        """抖音选品搜索（按佣金金额排序，返回前5）"""

        if self._chat_blocked(event):
            return
        if not keyword:
            yield event.plain_result("用法: 搜抖音 [关键词]")
            return
        if not self._sid():
            yield event.plain_result("❌ 管理员未配置折淘客授权SID")
            return
        try:
            res = await self.client.douyin_search(self._sid(), keyword,
                                                  page=1, page_size=5)
            products = ((res.get("data") or {}).get("products")) or []
            if not products:
                yield event.plain_result("没搜到相关商品")
                return
            lines = [f"🔍 抖音选品「{keyword}」(按佣金排序):"]
            for i, p in enumerate(products, 1):
                price = p.get("coupon_price") or p.get("price")
                lines.append(
                    f"{i}. {p.get('title', '')[:20]}\n"
                    f"   ¥{price} 佣金¥{p.get('cos_fee', '?')} "
                    f"({float(p.get('cos_ratio', 0))/100:.1f}%) 销量{p.get('sales', '?')}\n"
                    f"   {p.get('detail_url', '')}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"搜索出错: {e}")

    @filter.command("回溯订单")
    async def cmd_backfill(self, event: AstrMessageEvent, days: str = ""):
        """管理员手动回溯历史订单: 回溯订单 [天数]（默认取配置，最大730）"""

        if self._chat_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result(
                "❌ 仅管理员可用。请让管理员在插件配置「admin_ids」"
                f"或 AstrBot 主配置「管理员ID」中添加你的 ID: {event.get_sender_id()}")
            return
        if self._backfill_running:
            yield event.plain_result("⏳ 已有回溯任务在跑，请等它结束（看日志进度）")
            return
        try:
            days = int(days) if days else int(self._cfg("backfill_days", 7) or 0)
        except ValueError:
            yield event.plain_result("用法: 回溯订单 [天数]（如 回溯订单 730）")
            return
        days = max(0, min(days, 730))
        if not days:
            yield event.plain_result("回溯天数为 0（配置 backfill_days 未填），如需回溯请发: 回溯订单 730")
            return
        if not self._cfg("appkey"):
            yield event.plain_result("❌ 未配置折淘客 appkey")
            return
        self._backfill_running = True
        est = days * 4 // 60 + 1
        yield event.plain_result(
            f"⏳ 开始回溯最近 {days} 天历史订单，受限速影响预计约 {est} 分钟，"
            "进度看日志，完成后再发一条结果。期间可正常使用其他指令~")
        try:
            await self._backfill_orders(days)
            yield event.plain_result("✅ 历史订单回溯完成，可在「插件页面」仪表盘查看")
        except Exception as e:
            yield event.plain_result(f"❌ 回溯失败: {e}")
        finally:
            self._backfill_running = False

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员判定：AstrBot 主配置 admins_id（event.is_admin()）
        或插件配置 admin_ids（UID 或 unified_msg_origin 均可）"""
        sender = str(event.get_sender_id())
        if event.is_admin():
            return True
        for a in (self._cfg("admin_ids") or []):
            if sender == str(a) or sender in str(a):
                return True
        return False

    @filter.command("同步订单")
    async def cmd_sync(self, event: AstrMessageEvent):
        """管理员手动触发一次订单同步"""

        if self._chat_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result(
                "❌ 仅管理员可用。请让管理员在插件配置「admin_ids」"
                "或 AstrBot 主配置「管理员ID」中添加你的 ID: "
                f"{event.get_sender_id()}")
            return
        result = await self.sync_orders()
        total = sum(result.values())
        detail = "，".join(f"{k}+{v}" for k, v in result.items()) or "无新增"
        yield event.plain_result(
            f"✅ 订单同步完成（新增 {total} 条）：{detail}" if result
            else "本次同步无新增订单（或未配置 appkey）")
