# -*- coding: utf-8 -*-
"""
A-share timing Telegram bot
- 每个工作日 北京时间21:00 自动发送“阈值面板”总结
- 支持 Telegram 指令：/status 或 status
- 数据源：公开抓取为主（东财/新浪/HTML兜底），10Y国债可用 TradingEconomics 兜底
- “最新季度盈利覆盖面”需要 TuShare Token；否则显示 N/A
"""

import os, sys, time, datetime as dt, json, math, re, traceback
from typing import Optional, Tuple, Dict, Any
import requests

# ====== Secrets（GitHub Actions 配置）======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")   # 可选：启用“最新季度盈利覆盖面”
TE_API_KEY    = os.getenv("TE_API_KEY", "")      # 可选：10Y国债兜底；若无，可用 'guest:guest'

TIMEZONE_HOURS = 8  # 北京时间

# ====== 阈值（来自 part 2）======
THRESHOLDS = {
    "valuation": {
        "green": {"sh_pe_ttm_max": 17.5, "allA_pe_ttm_max": 18.0},
        "red":   {"sh_pe_ttm_min": 18.5, "allA_pe_ttm_min": 19.0}
    },
    "erp": {
        "green": {"erp_min": 3.8},
        "red":   {"erp_max": 3.2}
    },
    "earn_flow": {
        "green": {"profit_breadth_qoq": True, "northbound_5day_inflow": True},
        "red":   {"profit_breadth_qoq": False, "northbound_5day_inflow": False, "leverage_heat": True}
    }
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://eastmoney.com/"
}

def _json_get(url: str, params: dict=None, headers: dict=None, timeout: int=12) -> Optional[dict]:
    r = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
    r.raise_for_status()
    t = r.text.strip()
    # 可能是 JSONP，剥壳
    if t.startswith("jQuery") or t.startswith("var ") or t.find("{") > 0:
        t = t[t.find("{"): t.rfind("}")+1]
    return json.loads(t)

def _safe_get_json(url, params=None, headers=None, timeout=12, retries=3, sleep_s=0.8) -> Optional[dict]:
    for _ in range(retries):
        try:
            return _json_get(url, params=params, headers=headers, timeout=timeout)
        except Exception:
            time.sleep(sleep_s)
    return None

# ============ 抓取函数（多源兜底） ============

def fetch_sh_index_pe_ttm() -> Optional[float]:
    """
    上证综指PE(TTM) 兜底顺序：
    A. 东财JSON /api/qt/stock/get (字段 f162/f163/f167)
    B. 东财行情页HTML 正则兜底
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid":"1.000001","fields":"f57,f58,f162,f163,f167"}
        data = _safe_get_json(url, params=params)
        if data and data.get("data"):
            for key in ("f162","f163","f167"):
                v = data["data"].get(key)
                if v and float(v) > 0:
                    return round(float(v), 2)
    except Exception:
        pass
    # HTML 兜底
    try:
        html = requests.get("https://quote.eastmoney.com/zs000001.html", headers=HEADERS, timeout=12).text
        m = re.search(r"市盈率\(TTM\)\s*[:：]\s*([0-9]+\.[0-9]+)", html)
        if m:
            return round(float(m.group(1)), 2)
    except Exception:
        pass
    return None

def fetch_allA_pe_ttm_proxy() -> Optional[float]:
    """
    全A市值加权口径难以稳定获取；采用“沪深300 × 1.05”代理，并做多字段/HTML兜底
    """
    pe_csi300 = None
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid":"1.000300","fields":"f57,f58,f162,f163"}
        data = _safe_get_json(url, params=params)
        if data and data.get("data"):
            for key in ("f162","f163"):
                v = data["data"].get(key)
                if v and float(v) > 0:
                    pe_csi300 = float(v); break
    except Exception:
        pass
    if pe_csi300 is None:
        try:
            html = requests.get("https://quote.eastmoney.com/zs000300.html", headers=HEADERS, timeout=12).text
            m = re.search(r"市盈率\(TTM\)\s*[:：]\s*([0-9]+\.[0-9]+)", html)
            if m:
                pe_csi300 = float(m.group(1))
        except Exception:
            pass
    if pe_csi300:
        return round(pe_csi300 * 1.05, 2)
    return None

def fetch_cgb10y_yield() -> Optional[float]:
    """
    中国10Y国债收益率：
    A. 东财 bond.trends2（105.BCNY10Y）
    B. TradingEconomics 兜底（TE_API_KEY 支持 'guest:guest' 演示凭据）
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/bond/trends2/get"
        params = {"secid":"105.BCNY10Y","fields1":"f1,f2,f3,f4,f5,f6","fields2":"f51,f52,f53,f54,f55,f56","iscr":"0"}
        data = _safe_get_json(url, params=params)
        if data and data.get("data") and data["data"].get("trends"):
            last = data["data"]["trends"][-1]
            y = float(last.split(",")[1])  # f52=收益率
            if y > 0:
                return round(y, 2)
    except Exception:
        pass
    # TE 兜底
    if TE_API_KEY:
        try:
            te = requests.get(f"https://api.tradingeconomics.com/bond/china/10y?c={TE_API_KEY}", timeout=12)
            te.raise_for_status()
            arr = te.json()
            if isinstance(arr, list) and arr:
                y = arr[0].get("Last") or arr[0].get("Value")
                if y is not None:
                    return round(float(y), 2)
        except Exception:
            pass
    return None

def fetch_northbound_5day_inflow() -> Optional[Tuple[float, float]]:
    """
    北向资金近5日合计 + 最新日净流；东财 kamtbs.kline
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/kamtbs.kline/get"
        params = {"fields1":"f1,f3,f5","fields2":"f51,f52,f54","klt":"101","lmt":"7"}
        data = _safe_get_json(url, params=params)
        arr = data["data"]["klines"]
        vals = [float(s.split(",")[1]) for s in arr[-5:]]
        five_sum = round(sum(vals), 2)
        last_day = float(arr[-1].split(",")[1])
        return five_sum, last_day
    except Exception:
        return None

def fetch_leverage_heat_ratio() -> Optional[float]:
    """
    两融/流通占比：公开稳定“占比”接口较难；此处返回 None（仅作为红灯提示的可选项）。
    如你后续提供自家口径，在此返回0.025~0.047之间的数即可。
    """
    return None

def fetch_profit_breadth_qoq_latest() -> Optional[bool]:
    """
    “最新季度盈利覆盖面环比改善？”
    需要 TuShare token；逻辑：比较“最近完整季度 vs 前一季度”，q_profit_yoy > 0 的公司占比是否提升。
    无 token → 返回 None（消息中会提示）。
    """
    if not TUSHARE_TOKEN:
        return None
    try:
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)

        today = dt.date.today()
        y, q = today.year, (today.month-1)//3 + 1
        def qstr(yy, qq): return f"{yy}Q{qq}"

        if q == 1:
            last_y, last_q = y-1, 4
            prev_y, prev_q = y-1, 3
        else:
            last_y, last_q = y, q-1
            prev_y, prev_q = (y-1, 4) if q-1 == 1 else (y, q-2)

        last_p, prev_p = qstr(last_y,last_q), qstr(prev_y,prev_q)

        # 使用 fina_indicator_vip（若权限不足可改为 fina_indicator 并调整字段）
        df_last = pro.fina_indicator_vip(period=last_p)
        df_prev = pro.fina_indicator_vip(period=prev_p)

        def calc_breadth(df):
            if df is None or df.empty: return None
            col = "q_profit_yoy" if "q_profit_yoy" in df.columns else None
            if not col: return None
            s = df[col].dropna()
            if s.empty: return None
            return (s > 0).mean()

        b_last = calc_breadth(df_last)
        b_prev = calc_breadth(df_prev)
        if b_last is None or b_prev is None:
            return None
        return b_last > b_prev
    except Exception:
        return None

# ============ 计算与消息 ============

def compute_erp(pe: Optional[float], cgb10y: Optional[float]) -> Optional[float]:
    if pe and pe > 0 and cgb10y is not None:
        earning_yield = 100.0 / pe
        return round(earning_yield - cgb10y, 2)
    return None

def build_summary() -> Tuple[str, Dict[str, Any]]:
    sh_pe = fetch_sh_index_pe_ttm()
    allA_pe = fetch_allA_pe_ttm_proxy()
    cgb10y = fetch_cgb10y_yield()
    nb5 = fetch_northbound_5day_inflow()
    lev_ratio = fetch_leverage_heat_ratio()
    profit_breadth_ok = fetch_profit_breadth_qoq_latest()
    erp = compute_erp(sh_pe, cgb10y)

    greens, reds = [], []

    # 估值组
    if sh_pe is not None and sh_pe <= THRESHOLDS["valuation"]["green"]["sh_pe_ttm_max"]:
        greens.append("估值-上证PE≤17.5 ✅")
    if allA_pe is not None and allA_pe <= THRESHOLDS["valuation"]["green"]["allA_pe_ttm_max"]:
        greens.append("估值-全A(代理)≤18x ✅")
    if sh_pe is not None and sh_pe >= THRESHOLDS["valuation"]["red"]["sh_pe_ttm_min"]:
        reds.append("估值-上证PE≥18.5 ❌")
    if allA_pe is not None and allA_pe >= THRESHOLDS["valuation"]["red"]["allA_pe_ttm_min"]:
        reds.append("估值-全A(代理)≥19x ❌")

    # 风险溢价组
    if erp is not None and erp >= THRESHOLDS["erp"]["green"]["erp_min"]:
        greens.append("ERP≥3.8% ✅")
    if erp is not None and erp <= THRESHOLDS["erp"]["red"]["erp_max"]:
        reds.append("ERP≤3.2% ❌")

    # 盈利/流动性组
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
    else:
        # 若抓不到北向，保持中性不计分，避免误判
        pass

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
    if nb5 is not None:
        five_sum, last_day = nb5
        lines.append(f"• 北向资金：近5日合计 {five_sum:.2f} 亿元；最新日 {last_day:.2f} 亿元")
    else:
        lines.append("• 北向资金：N/A（接口临时不可用）")
    if lev_ratio is not None:
        lines.append(f"• 两融/流通占比：{lev_ratio*100:.2f}%")
    else:
        lines.append("• 两融/流通占比：N/A（公开口径缺失）")
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
    lines.append("\n注：公开抓取接口可能变动；若东财字段更新，请在 bot.py 中调整对应函数。")
    msg = "\n".join(lines)

    payload = {
        "sh_pe": sh_pe, "allA_pe_proxy": allA_pe, "cgb10y": cgb10y, "erp": erp,
        "northbound_5d": nb5, "lev_ratio": lev_ratio,
        "profit_breadth_qoq_ok": profit_breadth_ok,
        "greens": greens, "reds": reds, "action": action
    }
    return msg, payload

# ============ Telegram ============

def tg_send_message(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: BOT_TOKEN/CHAT_ID not set.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
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
        print("ERROR: BOT_TOKEN/CHAT_ID not set.")
        return
    end_time = time.time() + poll_minutes * 60
    offset = None
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    while time.time() < end_time:
        try:
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset
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
        print("Usage: python bot.py [run|status|poll <minutes>]")
        sys.exit(0)
    cmd = args[0]
    try:
        if cmd == "run":
            msg, _ = build_summary()
            ok = tg_send_message(msg)
            print("sent:", ok)
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
