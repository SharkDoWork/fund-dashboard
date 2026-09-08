# -*- coding: utf-8 -*-
"""
猪周期真实数据源抓取 (独立于基金看板, 不依赖 fund_tracker)
======================================================
只做一件事: 抓农业农村部畜牧兽医局「全国500个县集贸市场畜产品和饲料集贸价格周报」,
解析最新一篇的 仔猪 / 生猪 / 猪肉 平均价格(元/公斤) + 文章名 + 链接。

数据源: 农业信息网·监测预警栏目 https://www.agri.cn/sj/jcyj/
该栏目每周发布 "X月第X周畜产品和饲料集贸市场价格情况", 文本中含:
  "全国仔猪平均价格XX.XX元/公斤" / "全国生猪平均价格XX.XX元/公斤" / "全国猪肉平均价格XX.XX元/公斤"
"""
import json, os, re, ssl, time, datetime, urllib.request, urllib.parse
import pigcycle_db

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config", "pigcycle_sources.json")

COLUMN_URL = "https://www.agri.cn/sj/jcyj/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http_get(url, ref="https://www.agri.cn/", tries=3, timeout=15, delay=0.5):
    """带重试/退避的 GET; 东财类站点常限频, 这里目标为农业信息网, 重试更稳。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(delay + i * 0.4)
    raise last or RuntimeError("http_get failed: %s" % url)


def load_sources():
    """读取干净的数据源配置(周度监测地址 + 能繁官方发布列表)。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"weekly_monitor": {"url": COLUMN_URL}, "sow_sources": []}


def _find(text, pat):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _parse_weekly_article(url, title):
    """解析单篇周度监测文章, 返回 {date,piglet,pork,porkmeat,url,title} 或 None。"""
    try:
        art = http_get(url, ref=COLUMN_URL)
    except Exception:
        return None
    piglet = _find(art, r"全国仔猪平均价格([\d.]+)元/公斤")
    pork = _find(art, r"全国生猪平均价格([\d.]+)元/公斤")
    porkmeat = _find(art, r"全国猪肉平均价格([\d.]+)元/公斤")
    # 解析采集日 -> 周期日期
    m = re.search(r"采集日为(\d+)月(\d+)日", art)
    date = None
    if m:
        y = 2026
        ym = re.search(r"/(\d{6})/t", url)
        if ym:
            y = int(ym.group(1)[:4])
        date = "%04d-%02d-%02d" % (y, int(m.group(1)), int(m.group(2)))
    if piglet is None and pork is None:
        return None
    return {"date": date, "piglet": piglet, "pork": pork, "porkmeat": porkmeat,
            "url": url, "title": title}


def _collect_weekly_articles(limit):
    """从栏目页收集最近 limit 篇「畜产品和饲料集贸价格」文章链接(绝对地址)。"""
    html = http_get(COLUMN_URL)
    links = re.findall(r'<a\s+href="([^"]+t20\d{6}_\d+\.htm)"[^>]*>([^<]+)</a>', html)
    arts = [(u, t.strip()) for u, t in links if "畜产品和饲料集贸" in t]
    out = []
    for u, t in arts[:limit]:
        out.append((urllib.parse.urljoin(COLUMN_URL, u), t))
    return out


def fetch_piglet_weekly():
    """抓周度监测最新一篇, 解析仔猪/生猪/猪肉价格。
    返回 {ok, date, piglet, pork, porkmeat, url, title, message}。"""
    try:
        arts = _collect_weekly_articles(1)
    except Exception as e:
        return {"ok": False, "message": "栏目页获取失败: %s" % e}
    if not arts:
        return {"ok": False, "message": "栏目页未找到周度监测文章"}
    url, title = arts[0]
    row = _parse_weekly_article(url, title)
    if not row:
        return {"ok": False, "message": "最新周文章解析失败(页面结构可能变动)",
                "url": url, "title": title}
    row = dict(row)
    row["ok"] = True
    row["message"] = "已解析 %s 的周度价格" % (row["date"] or title)
    return row


def fetch_piglet_history(limit=16):
    """抓周度监测最近 limit 篇, 解析每篇仔猪/生猪/猪肉价格, 返回历史序列。
    单篇失败不影响其他(温和限速避免被限频)。"""
    try:
        arts = _collect_weekly_articles(limit)
    except Exception as e:
        return {"ok": False, "message": "栏目页获取失败: %s" % e, "rows": []}
    if not arts:
        return {"ok": False, "message": "栏目页未找到周度监测文章", "rows": []}
    rows = []
    for url, title in arts:
        row = _parse_weekly_article(url, title)
        if row:
            rows.append(row)
        time.sleep(0.3)  # 温和限速, 避免被限频
    return {"ok": True, "count": len(rows), "rows": rows,
            "message": "已抓取 %d 周历史价格" % len(rows)}


# ---------------------------------------------------------- 周期分析指标抓取(尽力而为)
# 说明: 猪粮比价/指数PE 的"官方稳定源"指发布机构(发改委/中证指数公司), 但二者均无干净免费
# 编程接口。下列适配器尽力尝试免费源, 失败则降级为"手动录入官方数值"的引导, 不抛异常。

INDEX_SECID = "1.930707"          # 中证畜牧养殖指数(东财 secid: 市场.代码, 1=上交所)
INDEX_NAME = "中证畜牧(930707)"


def _eastmoney_json(url):
    """拉取东财 push2 接口 JSON, 失败抛异常(由调用方捕获降级)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_index_pe(secid=INDEX_SECID, name=INDEX_NAME):
    """尽力抓取指数 PE(TTM)/PB。东财 push2 在用户本机通常可达; 沙箱可能被限频->降级。
    返回 {ok, date, pe, pb, name, message}。"""
    fields = "f43,f57,f58,f9,f23,f162,f167,f168"   # 含市盈率/市净率候选字段
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=%s&invt=2&fltt=2"
           % (secid, fields))
    try:
        data = _eastmoney_json(url).get("data") or {}
    except Exception as e:
        return {"ok": False,
                "message": "自动抓取失败(东财接口不可达或为沙箱限频): 请手动从中证指数公司/东财读取 PE 后录入。"
                           " 错误: %s" % e}
    if not data:
        return {"ok": False, "message": "自动抓取返回空(东财接口限频), 请稍后重试或手动录入。"}
    # 候选字段: f167=市盈率TTM, f9=市盈率(静), f162=市净率
    pe = data.get("f167")
    if pe in (None, "-", ""):
        pe = data.get("f9")
    pb = data.get("f162")
    try:
        pe = float(pe) if pe not in (None, "-", "") else None
    except Exception:
        pe = None
    try:
        pb = float(pb) if pb not in (None, "-", "") else None
    except Exception:
        pb = None
    if pe is None and pb is None:
        return {"ok": False, "message": "自动抓取未解析到 PE/PB(字段可能变动), 请手动录入。"}
    return {"ok": True, "date": datetime.date.today().isoformat(),
            "pe": pe, "pb": pb, "name": data.get("f58") or name,
            "message": "已抓取 %s 的 PE/PB" % (data.get("f58") or name)}


def fetch_index_pe_history(secid=INDEX_SECID, beg="20230826", end=None):
    """尽力抓取中证畜牧指数(930707) PE/PB 历史(约3年)。东财估值历史接口, 沙箱/限频可能失败->降级。
    返回 {ok, count, rows:[{date,pe,pb}], message}。本机通常可达, 仅供 '拉取3年历史' 按钮调用。"""
    if end is None:
        end = datetime.date.today().strftime("%Y%m%d")
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_IDX_PE_HISTORY&columns=DATE,PE,PB,INDEXCODE,INDEXNAME"
           "&filter=(INDEX_CODE=\"930707\")&sortColumns=DATE&sortTypes=1"
           "&pageSize=2000&client=PC")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"})
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        rows0 = (data.get("result") or {}).get("data") or []
        rows = []
        for it in rows0:
            d = it.get("DATE") or it.get("date")
            pe = it.get("PE") or it.get("pe")
            pb = it.get("PB") or it.get("pb")
            if not d:
                continue
            try:
                rows.append({"date": str(d)[:10], "pe": float(pe) if pe not in (None, "", "-") else None,
                             "pb": float(pb) if pb not in (None, "", "-") else None})
            except Exception:
                continue
        if not rows:
            return {"ok": False, "message": "东财估值历史接口未返回数据(字段可能变动), 请手动录入或稍后重试。"}
        return {"ok": True, "count": len(rows), "rows": rows,
                "message": "已抓取 %d 条 PE/PB 历史" % len(rows)}
    except Exception as e:
        return {"ok": False, "message": "自动抓取失败(东财估值历史接口不可达/限频): 请手动录入。错误: %s" % e}


def fetch_grain_pig_ratio():
    """猪粮比价: 无干净免费编程接口(发改委价格监测中心仅每周发布于官网/新闻)。
    返回 ok:False + 手动录入引导, 不抛异常。"""
    return {"ok": False,
            "message": "猪粮比价无免费编程接口: 请每周从「国家发改委价格监测中心」或财经新闻读取最新值后手动录入"
                       "（盈亏平衡≈7:1, <5:1 为一级预警）。",
            "source_url": "https://www.ndrc.gov.cn/"}


# ---------------------------------------------------------- 实时喂价抓取(自动每日)
# 1) 生猪现货日度价(外三元): 中国养猪网走势图历史接口, 含 ~1 年日度序列(可一次回Fill)
SPOT_URL = "https://www.zhuwang.com.cn/ajax_zhujia.php?m=zhujia&c=ajax&a=chartData"


def fetch_spot_hog_daily():
    """抓中国养猪网 外三元 日度历史序列(数组 oldest->newest, 末值=今日)。
    按"末值=今日、向前逐日倒推"映射日期, 一次回Fill全部历史。"""
    try:
        raw = http_get(SPOT_URL, ref="https://www.zhuwang.com.cn/", tries=3, timeout=20)
    except Exception as e:
        return {"ok": False, "message": "中国养猪网历史接口抓取失败: %s" % e}
    try:
        d = json.loads(raw)
    except Exception as e:
        return {"ok": False, "message": "中国养猪网返回非 JSON: %s" % e}
    arr = d.get("pigprice") or []
    n = len(arr)
    if not n:
        return {"ok": False, "message": "未解析到日度价格序列(pigprice 为空)"}
    today = datetime.date.today()
    rows = []
    for i, v in enumerate(arr):
        try:
            fv = float(v)
        except Exception:
            continue
        day = today - datetime.timedelta(days=(n - 1 - i))
        rows.append({"date": day.isoformat(), "value": fv})
    if not rows:
        return {"ok": False, "message": "日度价格序列无有效数值"}
    return {"ok": True, "count": len(rows), "rows": rows,
            "latest": rows[-1]["value"], "latest_date": rows[-1]["date"],
            "message": "已抓取 %d 个交易日的生猪(外三元)现货价(含历史回Fill)" % len(rows),
            "source_url": "https://www.zhuwang.com.cn/"}


# 2) 周度屠宰开工率: 卓创资讯生猪周评(中国畜牧业协会官网 caaa.cn 免费转载)
CAAA_LIST = "https://www.caaa.cn/html/fw/market/zhu/"


def _caaa_collect_articles(max_pages=60):
    """扫描 caaa 生猪栏目列表页, 收集所有'卓创周评'文章 (url, title)。"""
    arts, seen = [], set()
    for p in range(1, max_pages + 1):
        url = CAAA_LIST if p == 1 else "%s%d.html" % (CAAA_LIST, p)
        time.sleep(2.5)  # 协会站点软限频: 列表页之间留间隔
        try:
            html = http_get(url, ref=CAAA_LIST, tries=2, timeout=15)
        except Exception:
            break
        items = re.findall(
            r'<a href="(https://www\.caaa\.cn/html/fw/market/zhu/[^"]+\.html)"[^>]*>.*?'
            r'<div class="news_title_fz ell">([^<]+)</div>', html, re.S)
        if not items and p > 1:
            break
        for u, t in items:
            if "周评" in t and "卓创" in t and u not in seen:
                seen.add(u)
                arts.append((u, t.strip()))
        time.sleep(0.2)
    return arts


def _parse_slaughter_article(url, title):
    """解析单篇卓创周评: 周内平均开工率 / 日均屠宰量 / 白条猪肉均价 / 周截止日。
    带重试(卓创/协会站点偶发限频), 正则放宽以兼容不同表述。"""
    html = None
    for attempt in range(3):
        try:
            time.sleep(2.5 + attempt * 1.5)  # 协会站点软限频: 间隔请求避免命中"访问频繁"拦截页
            html = http_get(url, ref=CAAA_LIST, tries=2, timeout=15)
            if html and "平均开工率" in html:
                break
            html = None
        except Exception:
            time.sleep(1.0 + attempt)
    if not html:
        return None
    m = re.search(r"平均开工率([\d.]+)%", html)
    rate = float(m.group(1)) if m else None
    if rate is None:
        return None
    m2 = re.search(r"日均屠宰量([\d.]+)万头", html)
    kill = float(m2.group(1)) if m2 else None
    m3 = re.search(r"白条猪肉均价为?([\d.]+)元/公斤", html)
    white = float(m3.group(1)) if m3 else None
    # 周截止日: 标题可能用全角括号, 形如 （20260828-0903）或 (20260828-0903)
    #   group1=起始YYYYMMDD(8位), group2=截止MMDD(4位) -> 截止日=起始年 + MMDD
    dm = re.search(r"[（(](\d{8})-(\d{4})[）)]", title)
    end = None
    if dm:
        y = dm.group(1)[:4]
        end = "%s-%s-%s" % (y, dm.group(2)[:2], dm.group(2)[2:4])
    else:
        ym = re.search(r"/(\d{4})/(\d{4})/", url)  # 兜底: 链接形如 /2026/0903/25400.html
        if ym:
            end = "%s-%s-%s" % (ym.group(1)[:4], ym.group(2)[:2], ym.group(2)[2:4])
    return {"date": end, "rate": rate, "kill_volume": kill, "pork_white": white,
            "url": url, "title": title}


def fetch_slaughter_weekly_history(max_pages=60):
    """抓卓创周评历史(多页扫描), 返回周度开工率序列(含日均屠宰量/白条价)。"""
    arts = _caaa_collect_articles(max_pages=max_pages)
    rows = []
    for url, title in arts:
        r = _parse_slaughter_article(url, title)
        if r and r.get("date"):
            rows.append(r)
        time.sleep(0.2)
    if not rows:
        return {"ok": False, "message": "未从 caaa 解析到任何卓创周评开工率数据", "rows": []}
    return {"ok": True, "count": len(rows), "rows": rows,
            "message": "已抓取 %d 周卓创周评屠宰开工率历史" % len(rows)}


def fetch_slaughter_weekly_latest():
    """仅扫描最新 1-2 页, 返回最近一篇卓创周评的周度开工率(每日刷新用)。"""
    arts = _caaa_collect_articles(max_pages=2)
    for url, title in arts:
        r = _parse_slaughter_article(url, title)
        if r and r.get("date"):
            return {"ok": True, "date": r["date"], "rate": r["rate"],
                    "kill_volume": r.get("kill_volume"), "pork_white": r.get("pork_white"),
                    "url": url, "title": title,
                    "message": "已解析 %s 的周度开工率" % (r["date"] or title)}
    return {"ok": False, "message": "未找到最新卓创周评"}


# 3) 月度生猪价格(农业农村部·生猪专题, 权威, 覆盖近 3 年以上)
MOA_BASE = "https://www.moa.gov.cn/ztzl/szcpxx/jdsj/"


def _moa_month_text(ym):
    """抓取某月农业农村部生猪专题页, 返回纯文本(标签去净、空白压缩)。"""
    y, mo = ym[:4], ym[4:6]
    url = "%s%s/%s%s/" % (MOA_BASE, y, y, mo)
    try:
        html = http_get(url, ref=MOA_BASE, tries=2, timeout=20)
    except Exception:
        return None, url
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    return txt, url


def _moa_grab(txt, label, unit):
    """在压缩文本中抓取 'label（unit） 数值'。"""
    if not txt:
        return None
    m = re.search(label + r"（" + re.escape(unit) + r"）\s*([\d.]+)", txt)
    return float(m.group(1)) if m else None


def fetch_hog_monthly_moa(start_ym="202301", end_ym=None):
    """抓取农业农村部《生猪专题》月度数据(生猪出场价/仔猪价/白条价/定点屠宰量),
    按月份从 start_ym 到 end_ym(默认上月)逐月回Fill, 覆盖近 3 年以上。
    月份页用线程池并行抓取以缩短墙钟时间。返回 {ok,count,rows:[{period(YYYY-MM),...}],message}。"""
    today = datetime.date.today()
    if not end_ym:
        # 上月(当月数据一般未发布)
        first = today.replace(day=1)
        end = first - datetime.timedelta(days=1)
        end_ym = "%04d%02d" % (end.year, end.month)
    # 生成月份区间
    y = int(start_ym[:4]); m = int(start_ym[4:6])
    ey = int(end_ym[:4]); em = int(end_ym[4:6])
    months = []
    while (y, m) <= (ey, em):
        months.append("%04d%02d" % (y, m))
        m += 1
        if m > 12:
            m = 1; y += 1
    # 并行抓取各月文本
    texts = {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_moa_month_text, ym): ym for ym in months}
            for fu in as_completed(futs):
                ym = futs[fu]
                try:
                    txt, url = fu.result()
                    texts[ym] = (txt, url)
                except Exception:
                    texts[ym] = (None, "")
    except Exception:
        for ym in months:
            texts[ym] = _moa_month_text(ym)
    rows = []
    skipped = 0
    for ym in months:
        txt, url = texts.get(ym, (None, ""))
        if not txt:
            skipped += 1
            continue
        spot = _moa_grab(txt, "全国生猪出场价格", "元/公斤")
        piglet = _moa_grab(txt, "全国仔猪价格", "元/公斤")
        pork = _moa_grab(txt, "全国批发市场白条猪价格", "元/公斤")
        kill = _moa_grab(txt, r"\d{4}年\d{1,2}月份生猪定点屠宰企业屠宰量", "万头")
        if spot is None and piglet is None and pork is None:
            skipped += 1
            continue
        rows.append({
            "period": "%s-%s" % (ym[:4], ym[4:6]),
            "value": spot, "piglet": piglet, "pork_white": pork, "kill_volume": kill,
            "url": url,
        })
    if not rows:
        return {"ok": False, "message": "未解析到任何月度数据(可能站点改版或月份区间无数据)"}
    return {"ok": True, "count": len(rows), "rows": rows, "skipped": skipped,
            "start": rows[0]["period"], "end": rows[-1]["period"],
            "message": "已抓取 %d 个月度(农业农村部), 区间 %s~%s" % (len(rows), rows[0]["period"], rows[-1]["period"])}


if __name__ == "__main__":
    pigcycle_db.init_db()
    import pprint
    pprint.pprint(fetch_piglet_weekly())
