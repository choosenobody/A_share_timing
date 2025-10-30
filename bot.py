# -*- coding: utf-8 -*-
"""
A-share timing Telegram bot (robust edition)
- 每个工作日 北京时间21:00 自动发送“阈值面板”总结
- 支持 /status 或 status
- 数据抓取：东财 JSON -> 东财 HTML -> 备用源（10Y 用 TradingEconomics，可用 guest:guest）
- 最新季度盈利覆盖面：需要 TuShare Token（无则 N/A，不影响其它项）
- 增强：多字段兜底、重试、随机查询参数、防 403 头、可选 DEBUG 日志
"""

import os, sys, time, datetime as dt, json, re, traceback, random
from typing import Optional, Tuple, Dict, Any
import requests

# ====== Secrets & Options ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")      # 可选：启用“最新季度盈利覆盖面”
TE_API_KEY    = os.getenv("TE_API_KEY", "guest:guest")  # 允许使用 demo 凭据
TIMEZONE_HOURS = 8
DEBUG = int(os.getenv("DEBUG", "0"))

# ====== 阈值 ======
THRESHOLDS = {
    "valuation": {"green": {"sh_pe_ttm_max": 17.5, "allA_pe_ttm_max": 18.0},
                  "red":   {"sh_pe_ttm_min": 18.5, "allA_pe_ttm_min": 19.0}},
    "erp":       {"green": {"erp_min": 3.8}, "red": {"erp_max": 3.2}},
    "earn_flow": {"green": {"profit_breadth_qoq": True, "northbound_5day_inflow": True},
                  "red":   {"profit_breadth_qoq": False, "northbound_5day_inflow": False, "leverage_heat": True}}
}

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15",
]
def _headers():
    return {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://eastmoney.com/",
        "Connection": "keep-alive",
    }

def _json_get(url: str, params: dict=None, timeout: int=12) -> Optional[dict]:
    p = dict(params or {})
    p["_t"] = str(int(time.time()*1000))  # 防缓存
    r = requests.get(url, params=p, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    t = r.text.strip()
    if DEBUG:
        print(f"[DEBUG] GET {url} -> status={r.status_code}, len={len(t)}")
    # 剥 JSONP
    if t.startswith("jQuery") or t.startswith("var ") or (t.find("{") > 0 and not t.strip().startswith("{")):
        t = t[t.find("{"): t.rfind("}")+1]
    return json.loads(t)

def _safe_get_json(url, params=None, timeout=12, retries=4, sleep_s=1.0) -> Optional[dict]:
    for i in range(retries):
        try:
            return _json_get(url, params=params, timeout=timeout)
        except Exception as e:
            if DEBUG: print(f"[DEBUG] JSON try {i+1} failed:", repr(e))
            time.sleep(sleep_s)
    return None

def _safe_get_text(url, timeout=12, retries=3, sleep_s=0.8) -> Optional[str]:
    for i in range(retries):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            r.raise_for_status()
            if DEBUG:
                print(f"[DEBUG] GET {url} -> status={r.status_code}, len={len(r.text)}")
            return r.text
        except Exception as e:
            if DEBUG: print(f"[DEBUG] HTML try {i+1} failed:", repr(e))
            time.sleep(sleep_s)
    return None

# ============ 抓取：上证 PE ============
def fetch_sh_index_pe_ttm() -> Optional[float]:
    # A: 东财 JSON
    try:
        data = _safe_get_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid":"1.000001","fields":"f57,f58,f162,f163,f167"}
        )
        if data and data.get("data"):
            for key in ("f162","f163","f167"):
                v = data["data"].get(key)
                if v and float(v) > 0:
                    return round(float(v), 2)
    except Exception:
        pass
    # B: HTML 兜底
    html = _safe_get_text("https://quote.eastmoney.com/zs000001.html")
    if html:
        m = re.search(r"市盈率\(TTM\)\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", html)
        if m:
            return round(float(m.group(1)), 2)
    return None

# ============ 抓取：全A 代理（沪深300×1.05） ============
def fetch_allA_pe_ttm_proxy() -> Optional[float]:
    pe300 = None
    try:
        data = _safe_get_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid":"1.000300","fields":"f57,f58,f162,f163"}
        )
        if data and data.get("data"):
            for key in ("f162","f163"):
                v = data["data"].get(key)
                if v and float(v) > 0:
                    pe300 = float(v); break
    except Exception:
        pass
    if pe300 is None:
        html = _safe_get_text("https://quote.eastmoney.com/zs000300.html")
        if html:
            m = re.search(r"市盈率\(TTM\)\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", html)
            if m:
                pe300 = float(m.group(1))
    if pe300:
        return round(pe300 * 1.05, 2)
    return None

# ============ 抓取：10Y 国债 ============
def fetch_cgb10y_yield() -> Optional[float]:
    # A: 东财 JSON
    try:
        data = _safe_get_json(
            "https://push2.eastmoney.com/api/qt/bond/trends2/get",
            params={"secid":"105.BCNY10Y","fields1":"f1,f2,f3,f4,f5,f6","fields2":"f51,f52,f53,f54,f55,f56","iscr":"0"}
        )
        if data and data.get("data") and data["data"].get("trends"):
            last = data["data"]["trends"][-1]
            y = float(last.split(",")[1])  # f52
            if y > 0:
                return round(y, 2)
    except Exception:
        pass
    # B: TradingEconomics 兜底（允许 guest:guest）
    if TE_API_KEY:
        try:
            te = requests.get(f"https://api.tradingeconomics.com/bond/china/10y?c={TE_API_KEY}", timeout=12)
            te.raise_for_status()
            arr = te.json()
            if isinstance(arr, list) and arr:
                y = arr[0].get("Last") or arr[0].get("Value")
                if y is not None:
                    return round(float(y), 2)
        except Exception as e:
            if DEBUG: print("[DEBUG] TE 10Y error:", repr(e))
    return None

# ============ 抓取：北向 5 日 ============
def fetch_northbound_5day_inflow() -> Optional[Tuple[float, float]]:
    try:
        data = _safe_get_json(
            "https://push2.eastmoney.com/api/qt/kamtbs.kline/get",
            params={"fields1":"f1,f3,f5","fields2":"f51,f52,f54","klt":"101","lmt":"7"}
        )
        arr = data["data"]["klines"]
        vals = [float(s.split(",")[1]) for s in arr[-5:]]
        five_sum = round(sum(vals), 2)
        last_day = float(arr[-1].split(",")[1])
        return five_sum, last_day
    except Exception as e:
        if DEBUG: print("[DEBUG] northbound error:", repr(e))
        return None

# ============ 抓取：两融热度（占位） ============
def fetch_leverage_heat_ratio() -> Optional[float]:
    return None  # 暂无稳定免登“占比”口径

# ============ 抓取：盈利覆盖面（需要 TuShare） ============
def fetch_profit_breadth_qoq_latest() -> Optional[bool]:
    if not TUSHARE_TOKEN:
        return None
    try:
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)
        today = dt.date.today()
        y, q = today.year, (today.month-1)//3 + 1
        def Q(yy, qq): return f"{yy}Q{qq}"
        if q == 1:
            last_y, last_q = y-1, 4
            prev_y, prev_q = y-1, 3
        else:
            last_y, last_q = y, q-1
            prev_y, prev_q = (y-1, 4) if q-1 == 1 else (y, q-2)
        last_p, prev_p = Q(last_y,last_q), Q(prev_y,prev_q)

        # 若 vip 不可用可尝试 fina_indicator
        try:
            df_last = pro.fina_indicator_vip(period=last_p)
            df_prev = pro.fina_indicator_vip(period=prev_p)
        except Exception:
            df_last = pro.fina_indicator(period=last_p)
            df_prev = pro.fina_indicator(period=prev_p)

        def breadth(df):
            if df is None or df.empty: return None
            col = "q_profit_yoy" if "q_profit_yoy" in df.columns else None
            if not col: return None
            s = df[col].dropna()
            if s.empty: return None
            return (s > 0).mean()
        b_last, b_prev = breadth(df_last), breadth(df_prev)
        if b_last is None or b_prev is None:
            return None
        return b_last > b_prev
    except Exception as e:
        if DEBUG: print("[DEBUG] tushare breadth error:", repr(e))
        return None

# ============ 计算与消息 ============
def compute_erp(pe: Optional[float], cgb10y: Optional[float]) -> Optional[float]:
    if pe and pe > 0 and cgb10y is not None:
        return round(100.0/pe - cgb10y, 2)
    return None

def build_summary():
    sh_pe = fetch_sh_index_pe_ttm()
    allA_pe = fetch_allA_pe_ttm_proxy()
    cgb10y = fetch_cgb10y_yield()
    nb5 = fetch_northbound_5day_inflow()
    lev_ratio = fetch_leverage_heat_ratio()
    profit_breadth_ok = fetch_profit_breadth_qoq_latest()
    erp = compute_erp(sh_pe, cgb10y)

    greens, reds = [], []
    # 估值
    if sh_pe is not None and sh_pe <= THRESHOLDS["valuation"]["green"]["sh_pe_ttm_max"]:
        greens.append("估值-上证PE≤17.5 ✅")
    if allA_pe is not None and allA_pe <= THRESHOLDS["valuation"]["green"]["allA_pe_ttm_max"]:
        greens.append("估值-全A(代理)≤18x ✅")
    if sh_pe is not None and sh_pe >= THRESHOLDS["valuation"]["red"]["sh_pe_ttm_min"]:
        reds.append("估值-上证PE≥18.5 ❌")
    if allA_pe is not None and allA_pe >= THRESHOLDS["valuation"]["red"]["allA_pe_ttm_min"]:
        reds.append("估值-全A(代理)≥19x ❌")
    # ERP
    if erp is not None and erp >= THRESHOLDS["erp"]["green"]["erp_min"]:
        greens.append("ERP≥3.8% ✅")
    if erp is not None and erp <= THRESHOLDS["erp"]["red"]["erp_max"]:
        reds.append("ERP≤3.2% ❌")
    # 盈利/流动性
    if profit_breadth_ok is True:
        greens.append("盈利覆盖面(最新季度)环比改善 ✅")
    elif profit_breadth_ok is False:
        reds.append("盈利覆盖面(最新季度)环比转弱 ❌")
    if nb5 is not None:
        five_sum, last_day = nb5
        if five_sum > 0:
            greens.append("北向5日净流入为正 ✅")
        else:
            reds.append("北向5日净流入为负 ❌")

    if lev_ratio is not None and lev_ratio >= 0.03:
        reds.append("两融/流通 ≥3%（热） ❌")

    green_count, red_count = len(greens), len(reds)
    action = "保持中性"
    if green_count >= 3 and red_count <= 1:
        action = "可逐步推进至 35% 权益（结构偏红利/龙头）"
    if red_count >= 2:
        action = "回落至 ≤30% 权益，并降低高估赛道敞口"

    now_bj = dt.datetime.utcnow() + dt.timedelta(hours=TIMEZONE_HOURS)
    def fmt(v): return "N/A" if v is None else f"{v:.2f}"

    lines = []
    lines.append(f"📊 A股阈值面板 {now_bj.strftime('%Y-%m-%d %H:%M')} (UTC+8)")
    lines.append("— 基于你的“part1+part2”规则 —\n")
    lines.append(f"• 上证PE(TTM)：{fmt(sh_pe)}")
    lines.append(f"• 全A(代理)PE(TTM)：{fmt(allA_pe)}  ← 以沪深300估值×1.05近似（公开稳定口径难直接获取）")
    lines.append(f"• 10Y国债收益率(%)：{fmt(cgb10y)}")
    lines.append(f"• ERP(估算, %)：{fmt(erp)}")
    lines.append(f"• 北向资金：{'N/A' if nb5 is None else f'近5日合计 {nb5[0]:.2f} 亿元；最新日 {nb5[1]:.2f} 亿元'}")
    lines.append(f"• 两融/流通占比：{'N/A（公开口径缺失）' if lev_ratio is None else f'{lev_ratio*100:.2f}%'}")
    if profit_breadth_ok is None:
        lines.append("• 盈利覆盖面(最新季度)：N/A（如需启用，请在Secrets配置 TUSHARE_TOKEN）")
    else:
        lines.append(f"• 盈利覆盖面(最新季度)环比改善？ {'是' if profit_breadth_ok else '否'}")

    if greens:
        lines.append("\n✅ 绿灯：")
        lines.extend([f"  - {g}" for g in greens])
    if reds:
        lines.append("\n❌ 红灯：")
        lines.extend([f"  - {r}" for r in reds])

    lines.append("\n🎯 动作建议：" + action)
    lines.append("\n注：如仍见 N/A，请在本仓库 Actions 日志启用 DEBUG=1 复查抓取片段；如需“盈利覆盖面”，请配置 TuShare Token。")
    msg = "\n".join(lines)

    payload = {"sh_pe": sh_pe, "allA_pe_proxy": allA_pe, "cgb10y": cgb10y, "erp": erp,
               "northbound_5d": nb5, "lev_ratio": lev_ratio, "profit_breadth_qoq_ok": profit_breadth_ok,
               "greens": greens, "reds": reds, "action": action}
    return msg, payload

# ============ Telegram ============
def tg_send_message(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: BOT_TOKEN/CHAT_ID not set.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print("sendMessage error:", e)
        return False

def handle_status():
    msg, _ = build_summary()
    tg_send_message(msg)

def poll_updates_for_status(poll_minutes: int = 8):
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: BOT_TOKEN/CHAT_ID not set."); return
    end_time = time.time() + poll_minutes * 60
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    offset = None
    while time.time() < end_time:
        try:
            params = {"timeout": 20}
            if offset: params["offset"] = offset
            r = requests.get(f"{base}/getUpdates", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    message = upd.get("message") or upd.get("edited_message") or {}
                    chat = message.get("chat", {})
                    text = (message.get("text") or "").strip().lower()
                    if str(chat.get("id")) == str(CHAT_ID) and (text == "/status" or text == "status"):
                        handle_status()
        except Exception:
            pass
        time.sleep(2)

def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python bot.py [run|status|poll <minutes>]"); sys.exit(0)
    cmd = args[0]
    try:
        if cmd == "run":
            msg, _ = build_summary(); print("sent:", tg_send_message(msg))
        elif cmd == "status":
            handle_status()
        elif cmd == "poll":
            mins = int(args[1]) if len(args) > 1 else 8
            poll_updates_for_status(poll_minutes=mins)
        else:
            print("Unknown command.")
    except Exception:
        print(traceback.format_exc())

if __name__ == "__main__":
    main()
