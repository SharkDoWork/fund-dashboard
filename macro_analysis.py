"""macro_analysis.py — CPI/PPI 读数·变化·剪刀差的规则化解读引擎。

输入: macro_db 内的 cpi/ppi 月序列(dict 列表, 升序)。
产出: 现算指标(最新同比/环比/CPI-PPI 剪刀差及其较上月变化/连续回升月数) + 规则化信号解读 +
      条件式观察方向。全部数值来自库里真实数据; 语义为「统计逻辑推演, 非预测」,
      页面展示时须附口径说明与免责声明。
"""
import datetime


def _f(x, nd=2):
    if x is None:
        return "--"
    return ("%+." + str(nd) + "f") % x


def _pd(r):
    """行内期间字段兼容: fetch 行用 'period', db 行用 'period_date'。"""
    return r.get("period") or r.get("period_date")


def _streak(rows, key, up=True):
    """从最新往回数连续满足条件(上行/下行)的月数。rows 为升序 dict 列表。"""
    n = 0
    for r in reversed(rows):
        v = r.get(key)
        if v is None:
            break
        if up and v > 0:
            n += 1
        elif (not up) and v < 0:
            n += 1
        else:
            break
    return n


def _rising_months(rows, key):
    """同比口径连续逐月抬升/走低的月数。返回 (rising, falling)。"""
    rising = falling = 0
    for i in range(len(rows) - 1, 0, -1):
        cur, prev = rows[i].get(key), rows[i - 1].get(key)
        if cur is None or prev is None:
            break
        if cur > prev:
            rising += 1
            if falling:
                break
        elif cur < prev:
            falling += 1
            if rising:
                break
        else:
            break
    return rising, falling


def compute(cpi_rows, ppi_rows):
    """cpi_rows / ppi_rows: 升序 dict 列表(period/yoy/mom...)。"""
    if not cpi_rows or not ppi_rows:
        return {"ok": False, "message": "库内尚无数据, 请先拉取 CPI/PPI"}

    by_p = {}
    for r in cpi_rows:
        by_p.setdefault(_pd(r), {})["c"] = r
    for r in ppi_rows:
        by_p.setdefault(_pd(r), {})["p"] = r

    series = []
    for period in sorted(by_p):
        c = by_p[period].get("c") or {}
        p = by_p[period].get("p") or {}
        cy = c.get("yoy")
        pp = p.get("yoy")
        if cy is None and pp is None:
            continue
        series.append({
            "period": period,
            "cpi_yoy": cy, "cpi_mom": c.get("mom"),
            "ppi_yoy": pp,
            "ppi_mom": p.get("mom"),
            "gap": (cy - pp) if (cy is not None and pp is not None) else None,
        })
    if not series:
        return {"ok": False, "message": "CPI/PPI 无重叠月份"}

    last = series[-1]
    prev = series[-2] if len(series) >= 2 else None
    latest_period = last["period"]

    cpi_yoy, cpi_mom = last["cpi_yoy"], last["cpi_mom"]
    ppi_yoy = last["ppi_yoy"]
    ppi_mom = last["ppi_mom"]
    gap_now = last["gap"]
    gap_prev = prev["gap"] if prev else None
    gap_chg = (gap_now - gap_prev) if (gap_now is not None and gap_prev is not None) else None

    # 动能: 连续环比上行月数 / 同比连续抬升月数
    cpi_mom_up = _streak(series, "cpi_mom", up=True)
    cpi_mom_dn = _streak(series, "cpi_mom", up=False)
    ppi_rise, ppi_fall = _rising_months(series, "ppi_yoy")
    cpi_rise, cpi_fall = _rising_months(series, "cpi_yoy")
    ppi_mom_up = _streak(series, "ppi_mom", up=True)
    ppi_mom_dn = _streak(series, "ppi_mom", up=False)

    signals = []
    # 1) 通胀温度(CPI 同比水平)
    if cpi_yoy is None:
        temp = "数据缺失"
    elif cpi_yoy >= 3:
        temp = "明显偏热(≥3%)"
    elif cpi_yoy >= 2:
        temp = "偏高(2~3%)"
    elif cpi_yoy >= 0:
        temp = "温和(0~2%)"
    else:
        temp = "负值区(<0)"
    signals.append({
        "tag": "通胀温度(CPI 同比)", "level": "warn" if cpi_yoy is not None and cpi_yoy >= 3 else ("neg" if cpi_yoy is not None and cpi_yoy < 0 else "info"),
        "text": ("CPI 同比 %s%%(%s), 处于%s区间。"
                 % (_f(cpi_yoy), "环比 %s%%" % _f(cpi_mom) if cpi_mom is not None else "环比--", temp)),
    })

    # 2) 工业冷暖(PPI 同比水平 + 环比动能)
    if ppi_yoy is None:
        ppi_txt = "PPI 数据缺失"
    else:
        mom_txt = "环比 %s%%" % _f(ppi_mom) if ppi_mom is not None else "环比--"
        if ppi_yoy >= 3:
            ppi_txt = "PPI 同比 %s%%, 上游涨价明显(≥3%%), 中下游成本端压力上升" % _f(ppi_yoy)
        elif ppi_yoy > 0:
            ppi_txt = "PPI 同比 %s%%, 温和正区间, 上游偏暖" % _f(ppi_yoy)
        else:
            ppi_txt = "PPI 同比 %s%%, 负区间(工业品价格承压)" % _f(ppi_yoy)
        ppi_txt += "；%s" % mom_txt
    if ppi_rise >= 2:
        ppi_txt += "；PPI 同比已连续 %d 个月抬升" % ppi_rise
    elif ppi_fall >= 2:
        ppi_txt += "；PPI 同比已连续 %d 个月走低" % ppi_fall
    if ppi_mom_up >= 2:
        ppi_txt += "；PPI 已连续 %d 个月环比上行" % ppi_mom_up
    elif ppi_mom_dn >= 2:
        ppi_txt += "；PPI 已连续 %d 个月环比走低" % ppi_mom_dn
    signals.append({
        "tag": "工业冷暖(PPI 同比+环比)", "level": "info",
        "text": ppi_txt,
    })

    # 3) 剪刀差(CPI-PPI)及其方向
    if gap_now is None:
        gap_txt = "剪刀差数据不足"
    else:
        gap_txt = "CPI-PPI 剪刀差 = %s 个百分点(剪刀差 = CPI同比 − PPI同比)" % _f(gap_now, 1)
        if gap_chg is not None:
            if gap_chg > 0:
                gap_txt += "，较上月 %s 个百分点(数值走阔: 下游/消费端相对改善)" % _f(gap_chg, 1)
            elif gap_chg < 0:
                gap_txt += "，较上月 %s 个百分点(数值收窄: 上游相对走强)" % _f(gap_chg, 1)
            else:
                gap_txt += "，与上月持平"
        if gap_now > 0:
            gap_txt += "。剪刀差为正: 终端价格涨幅快于出厂价, 理论上下游消费/农业链利润空间相对占优"
        else:
            gap_txt += "。剪刀差为负: 出厂(工业品)涨幅快于终端消费, 历史上通常对应上游资源/周期类盈利占优、中下游成本受挤的格局"
    signals.append({
        "tag": "CPI-PPI 剪刀差", "level": "info", "text": gap_txt,
    })

    # 4) 动能小结
    dyn = []
    if cpi_mom_up >= 2:
        dyn.append("CPI 已连续 %d 个月环比上行" % cpi_mom_up)
    elif cpi_mom_dn >= 2:
        dyn.append("CPI 已连续 %d 个月环比走低" % cpi_mom_dn)
    if ppi_mom_up >= 2:
        dyn.append("PPI 已连续 %d 个月环比上行" % ppi_mom_up)
    elif ppi_mom_dn >= 2:
        dyn.append("PPI 已连续 %d 个月环比走低" % ppi_mom_dn)
    if cpi_rise >= 2:
        dyn.append("CPI 同比连续 %d 个月抬升" % cpi_rise)
    if cpi_fall >= 2:
        dyn.append("CPI 同比连续 %d 个月走低" % cpi_fall)
    signals.append({
        "tag": "边际动能", "level": "info",
        "text": ("；".join(dyn) if dyn else "近月 CPI/PPI 环比与同比均无连续单边动能(横盘整理)") + "。",
    })

    # 5) 条件式观察方向(基于当前读数, 随数据自动更新)
    notes = []
    if cpi_yoy is not None and cpi_yoy < 0:
        notes.append("CPI 处于负值区间时: 需求端偏弱, 若叠加猪价下行, 农业/养殖链条终端提价空间受限。")
    if cpi_mom is not None and cpi_mom >= 0.3:
        notes.append("CPI 环比抬升较快(≥0.3%): 关注食品价格(尤其猪肉)与能源项是否有持续上行驱动的证据。")
    if ppi_yoy is not None and ppi_yoy >= 3:
        notes.append("PPI 高位(≥3%): 若由能源/有色等上游驱动, 上游资源/周期盈利占优; 中下游制造与消费定价端承压, 留意毛利率挤压。")
    if ppi_yoy is not None and ppi_yoy < 0 and ppi_fall >= 3:
        notes.append("PPI 深度负区间且连续走低: 工业品需求弱, 但对成本敏感的中游(如饲料加工/农牧原料)反而是成本端缓压信号。")
    if ppi_mom is not None and ppi_mom >= 0.5:
        notes.append("PPI 环比涨幅较大(≥0.5%): 单月出厂价快速上行, 往往是上游涨价向中下游传导的边际起点, 值得跟踪后续是否扩散。")
    if ppi_mom is not None and ppi_mom <= -0.5:
        notes.append("PPI 环比降幅较大(≤-0.5%): 出厂价明显回落, 上游盈利端边际走弱, 对成本敏感的中游(饲料/农牧原料)构成成本缓压。")
    if gap_now is not None:
        if gap_now >= 2:
            notes.append("剪刀差明显为正(≥2pct): 历史上多对应必选消费/农业(终端涨价能传导)相对占优的窗口。")
        elif gap_now <= -2:
            notes.append("剪刀差明显为负(≤-2pct): 历史上多对应上游强/下游弱的窗口; 农业/养殖的利润弹性更取决于自身猪价与饲料成本差, 而非仅看通胀剪刀差。")
        else:
            notes.append("剪刀差在 ±2pct 内: 上下游利润分配接近均衡, 结构性行情更多由产业自身供需决定(如猪周期由产能决定)。")
    if gap_chg is not None and gap_chg >= 0.5:
        notes.append("剪刀差近一月明显走阔(≥0.5pct): 若延续, 下游消费/农业的定价环境边际改善, 值得跟踪验证。")
    if gap_chg is not None and gap_chg <= -0.5:
        notes.append("剪刀差近一月明显收窄(≤-0.5pct): 上游走强挤压下游的边际信号, 若延续则利好上游、压制下游估值扩张。")

    return {
        "ok": True,
        "latest_period": latest_period,
        "latest": {"cpi_yoy": cpi_yoy, "cpi_mom": cpi_mom,
                   "ppi_yoy": ppi_yoy, "ppi_mom": ppi_mom,
                   "gap": gap_now},
        "prev": {"period": prev["period"] if prev else None, "gap": gap_prev},
        "gap_chg": gap_chg,
        "momentum": {"cpi_mom_up": cpi_mom_up, "cpi_mom_dn": cpi_mom_dn,
                     "cpi_rise": cpi_rise, "cpi_fall": cpi_fall,
                     "ppi_rise": ppi_rise, "ppi_fall": ppi_fall,
                     "ppi_mom_up": ppi_mom_up, "ppi_mom_dn": ppi_mom_dn},
        "signals": signals,
        "notes": notes,
        "series": series,          # 供图表: 同比双线 + 剪刀差
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    import macro_db
    macro_db.init_db()
    r = compute(macro_db.get_series("cpi"), macro_db.get_series("ppi"))
    import json
    print(json.dumps({k: v for k, v in r.items() if k != "series"}, ensure_ascii=False, indent=2))
