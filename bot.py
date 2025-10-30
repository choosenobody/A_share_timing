# -*- coding: utf-8 -*-
"""
A-share timing Telegram bot
- 定时在北京时间 21:00 发送“阈值面板”总结
- 支持 Telegram 指令：/status 或 status
- 数据源：优先公开可抓取接口（东财/新浪），并预留可选API（TuShare/TradingEconomics）
- 阈值规则：来自你确认的“part 2 三要素六条线”，同时引用“part 1 关键引用”指标
"""
import os, sys, time, datetime as dt
import json
import math
import traceback
from typing import Optional, Tuple, Dict, Any, List

import requests

# ====== 配置（Secrets & 可选API）======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")  # 可选：若提供，则启用季报广度指标
TE_API_KEY    = os.getenv("TE_API_KEY", "")     # 可选：TradingEconomics 免费key（10Y国债备选）
TIMEZONE_HOURS = 8  # 机器人消息里显示北京时间

# ====== 阈值（来自 part 2）======
THRESHOLDS = {
    "valuation": {  # 估值组
        "green": {"sh_pe_ttm_max": 17.5, "allA_pe_ttm_max": 18.0},
        "red":   {"sh_pe_ttm_min": 18.5, "allA_pe_ttm_min": 19.0, "gro_high_frac_min": 0.95}  # 科创/创业板>95% 分位（此处以占比代理）
    },
    "erp": {  # 风险溢价组
        "green": {"erp_min": 3.8},
        "red":   {"erp_max": 3.2}
    },
    "earn_flow": {  # 盈利/流动性组
        "green": {"profit_breadth_qoq": True, "northbound_5day_inflow": True},
        "red":   {"profit_breadth_qoq": False, "northbound_5day_inflow": False, "leverage_heat": True}
    }
}

# ============ 数据抓取 ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://eastmoney.com/"
}

def _json_get(url: str, params: dict=None, headers: dict=None, timeout: int=10) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
        r.raise_for_status()
        # 东财大多返回 JSONP，需清洗括号
        txt = r.text.strip()
        if txt.startswith("jQuery") or txt.startswith("callback") or txt.startswith("({") is False and txt.find("{")>0:
            # 粗略剥壳
            txt = txt[txt.find("{"): txt.rfind("}")+1]
        return json.loads(txt)
    except Exception:
        return None

def fetch_sh_index_pe_ttm() -> Optional[float]:
    """
    上证综指 PE(TTM)
    方案A（东财指数估值接口，部分场景会变动）：secid=1.000001
    若失败返回 None；你也可改为 TuShare 或其他数据源。
    """
    # 尝试：东财指数详情（可能包含 f162/市盈率等字段，不同环境字段可能调整）
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": "1.000001",
        "fields": "f57,f58,f162,f167"  # 名称, 市场, PE, PB（字段随时间可能调整）
    }
    data = _json_get(url, params=params)
    try:
        if data and "data" in data and data["data"]:
            pe = data["data"].get("f162", None)
            if pe and pe > 0:
                return float(pe)
    except Exception:
        pass
    return None

def fetch_allA_pe_ttm_equally_weighted_proxy() -> Optional[float]:
    """
    全A等权/中位数口径会偏高；市值加权的“东财全A(加权)”较难直接抓。
    这里提供一个“近似代理”：用沪深300估值 + 大盘权重调整（保守地上调 1.0~1.5 倍区间）
    注：这是在公开接口不足时的替代。若你能提供稳定API（如Wind/Choice/雪球/自建抓取），建议替换。
    """
    # 取沪深300估值（secid=1.000300），再做保守系数
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": "1.000300", "fields": "f57,f58,f162"}
    data = _json_get(url, params=params)
    try:
        if data and data.get("data") and data["data"].get("f162"):
            pe_csi300 = float(data["data"]["f162"])
            # 保守上调（全A通常比沪深300更高），取 1.05
            return round(pe_csi300 * 1.05, 2)
    except Exception:
        pass
    return None

def fetch_cgb10y_yield() -> Optional[float]:
    """
    中国10Y 国债收益率（尝试东财债券接口；若失败可用 TradingEconomics 作为备选）
    """
    # 尝试：东财债券 10Y（secid 可能变动：105.BCNY10Y）
    url = "https://push2.eastmoney.com/api/qt/bond/trends2/get"
    params = {"secid": "105.BCNY10Y", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56", "iscr": "0"}
    data = _json_get(url, params=params)
    try:
        if data and data.get("data") and data["data"].get("trends"):
            # 取最后一个点 f52 即收益率
            last = data["data"]["trends"][-1]
            parts = last.split(",")
            y = float(parts[1])
            return y
    except Exception:
        pass

    # 备选：TradingEconomics（需要 TE_API_KEY）
    if TE_API_KEY:
        try:
            te = requests.get(
                f"https://api.tradingeconomics.com/bond/china/10y?c={TE_API_KEY}",
                timeout=10
            )
            te.raise_for_status()
            arr = te.json()
            if isinstance(arr, list) and arr:
                # 取最近一个的 price 或 yield 字段
                y = arr[0].get("Last", None) or arr[0].get("Value", None)
                if y is not None:
                    return float(y)
        except Exception:
            pass

    return None

def fetch_northbound_5day_inflow() -> Optional[Tuple[float, float]]:
    """
    北向资金 近5日合计（亿元）
    使用东财“kamtbs.kline”接口
    返回: (5日合计, 最新日净流入)
    """
    url = "https://push2.eastmoney.com/api/qt/kamtbs.kline/get"
    params = {
        "fields1": "f1,f3,f5",
        "fields2": "f51,f52,f54",  # 日期, 净流入(亿元), 上证涨跌幅?
        "klt": "101",  # 日级别
        "lmt": "6"     # 取最近6天，后面算5日
    }
    data = _json_get(url, params=params)
    try:
        arr = data["data"]["klines"]
        vals = []
        for item in arr[-5:]:
            parts = item.split(",")
            net = float(parts[1])
            vals.append(net)
        five_sum = round(sum(vals), 2)
        last_day = float(arr[-1].split(",")[1])
        return five_sum, last_day
    except Exception:
        return None

def fetch_leverage_heat_ratio() -> Optional[float]:
    """
    两融余额 / 流通市值 近似热度指标
    说明：公开可靠的总“流通市值”接口较难，此处给出“示例抓取”与“可选占位”
    若抓取失败则返回 None，不影响主流程，只在红灯条件中用到
    """
    # 示例：东财融资融券总量（亿元）
    try:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        # 这里没有现成“占比”，仅示范返回余额绝对值（亿元），占比需要另一个“流通市值”口径，故保留None
        # 你若有自建口径，可在此处返回 ratio (0.025 ~ 0.047 对应2.5%~4.7%)
        return None
    except Exception:
        return None

def fetch_profit_breadth_qoq_latest() -> Optional[bool]:
    """
    “最新季度盈利覆盖面环比改善？”（绿灯条件之一）
    - 没有公开、稳定的免认证API可直接全市场逐只汇总季报
    - 若提供 TuShare Token，可实现逐股汇总（建议）
    逻辑（TuShare）：
      1) pro.query('fina_indicator', period=最近季) 与上一季，统计净利润或ROE环比改善的公司占比
      2) 占比 > 上一季占比 → True；否则 False
    这里默认：若无 TUSHARE_TOKEN → 返回 None，并在消息里提示“请配置 TuShare”。
    """
    if not TUSHARE_TOKEN:
        return None
    try:
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)

        # 取最近两个财报季
        today = dt.date.today()
        year = today.year
        q = (today.month-1)//3 + 1
        # 回退：避免当季未完整披露
        # 取 last_q, prev_q
        def quarter_str(y, q):
            return f"{y}Q{q}"

        # 最近完整季：
        if q == 1:
            last_y, last_q = year-1, 4
            prev_y, prev_q = year-1, 3
        else:
            last_y, last_q = year, q-1
            prev_y, prev_q = (year-1, 4) if q-1 == 1 else (year, q-2)

        last_period = quarter_str(last_y, last_q)
        prev_period = quarter_str(prev_y, prev_q)

        # 指标用 roa/roe 或 qoq_netprofit，TuShare口径以实际为准
        df_last = pro.fina_indicator_vip(period=last_period)
        df_prev = pro.fina_indicator_vip(period=prev_period)

        # 定义“改善”标准：q_profit_yoy↑或 q_op_qoq↑，这里以 q_profit_yoy 为例
        def calc_breadth(df):
            # 过滤异常
            if df is None or df.empty or ("q_profit_yoy" not in df.columns):
                return None
            series = df["q_profit_yoy"].dropna()
            if series.empty:
                return None
            # 认为 q_profit_yoy > 0 为“改善”
            return (series > 0).mean()

        b_last = calc_breadth(df_last)
        b_prev = calc_breadth(df_prev)
        if b_last is None or b_prev is None:
            return None
        return b_last > b_prev
    except Exception as e:
        # 若 TuShare 权限不足或接口差异，返回 None
        return None

# ============ 计算 & 拼装 ============
def compute_erp(pe: Optional[float], cgb10y: Optional[float]) -> Optional[float]:
    if pe and pe > 0 and cgb10y is not None:
        earning_yield = 100.0 / pe  # 百分比
        erp = earning_yield - cgb10y
        return round(erp, 2)
    return None

def build_summary() -> Tuple[str, Dict[str, Any]]:
    # 抓取
    sh_pe = fetch_sh_index_pe_ttm()
    allA_pe = fetch_allA_pe_ttm_equally_weighted_proxy()
    cgb10y = fetch_cgb10y_yield()
    nb5 = fetch_northbound_5day_inflow()
    lev_ratio = fetch_leverage_heat_ratio()
    profit_breadth_ok = fetch_profit_breadth_qoq_latest()

    erp = compute_erp(sh_pe, cgb10y)

    # 阈值判定
    greens = []
    reds = []

    # 估值组
    if sh_pe is not None and sh_pe <= THRESHOLDS["valuation"]["green"]["sh_pe_ttm_max"]:
        greens.append("估值-上证PE≤17.5 ✅")
    if allA_pe is not None and allA_pe <= THRESHOLDS["valuation"]["green"]["allA_pe_ttm_max"]:
        greens.append("估值-全A(代理)≤18x ✅")

    if sh_pe is not None and sh_pe >= THRESHOLDS["valuation"]["red"]["sh_pe_ttm_min"]:
        reds.append("估值-上证PE≥18.5 ❌")
    if allA_pe is not None and allA_pe >= THRESHOLDS["valuation"]["red"]["allA_pe_ttm_min"]:
        reds.append("估值-全A(代理)≥19x ❌")
    # 科创/创业板分位>95%：此处缺少无钥数据，采用“结构热度”在消息里提示，不计入硬红灯

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

    # 杠杆热度（若有）
    if lev_ratio is not None and lev_ratio >= 0.03:
        reds.append("两融/流通 ≥3%（热） ❌")

    # 打分
    green_count = len(greens)
    red_count = len(reds)

    # 决策建议（与“≥3绿推进、≥2红降回”一致）
    action = "保持中性"
    if green_count >= 3 and red_count <= 1:
        action = "可逐步推进至 35% 权益（结构偏红利/龙头）"
    if red_count >= 2:
        action = "回落至 ≤30% 权益，并降低高估赛道敞口"

    # 文本
    now_bj = dt.datetime.utcnow() + dt.timedelta(hours=TIMEZONE_HOURS)
    lines = []
    lines.append(f"📊 A股阈值面板 {now_bj.strftime('%Y-%m-%d %H:%M')} (UTC+8)")
    lines.append("— 基于你的“part1+part2”规则 —\n")

    def fmt(v):
        return "N/A" if v is None else f"{v:.2f}"

    lines.append(f"• 上证PE(TTM)：{fmt(sh_pe)}")
    lines.append(f"• 全A(代理)PE(TTM)：{fmt(allA_pe)}  ← 无公开稳定口径时以沪深300估值×1.05近似")
    lines.append(f"• 10Y国债收益率(%)：{fmt(cgb10y)}")
    lines.append(f"• ERP(估算, %)：{fmt(erp)}")
    if nb5 is not None:
        five_sum, last_day = nb5
        lines.append(f"• 北向资金：近5日合计 {five_sum:.2f} 亿元；最新日 {last_day:.2f} 亿元")
    else:
        lines.append("• 北向资金：N/A")
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
    msg, payload = build_summary()
    tg_send_message(msg)

def poll_updates_for_status(poll_minutes: int = 8):
    """
    轮询 Telegram updates，捕捉 '/status' 或 'status' 指令并回复。
    为兼容 GitHub Actions 短时Runner，这里默认跑几分钟后自然退出。
    """
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
    # 用法：
    # python bot.py run            → 发送定时报文
    # python bot.py status         → 立即计算并发送
    # python bot.py poll N         → 轮询N分钟，响应 /status
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
