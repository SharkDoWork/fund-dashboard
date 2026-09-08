"""macro_fetch.py — 宏观月度指标(CPI/PPI)真实数据拉取。

数据源: 东方财富数据中心 (datacenter-web.eastmoney.com)
  - RPT_ECONOMY_CPI 居民消费价格指数(国家统计局口径, 东财转引): 全国/城市/农村 同比/环比/指数/累计
  - RPT_ECONOMY_PPI 工业生产者出厂价格指数(同上): 同比/指数/累计
口径: 国家统计局月度发布(CPI/PPI 一般于次月 9~12 日左右发布), 本站转引自东财数据中心,
      展示时须标注「东财数据中心 · 转引国家统计局 + 数据月份」, 不冒充一手。

国家统计局官网 data.stats.gov.cn 对程序化访问返回 403(反爬), 故采用东财链路(实测可用)。
"""
import json
import ssl
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CPI_REF = "https://data.eastmoney.com/cjsj/cpi.html"
PPI_REF = "https://data.eastmoney.com/cjsj/ppi.html"
CPI_SRC_URL = "https://data.eastmoney.com/cjsj/cpi.html"
PPI_SRC_URL = "https://data.eastmoney.com/cjsj/ppi.html"
SOURCE_TXT = "国家统计局月度发布(东财数据中心转引)"


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _http_get_json(url, referer):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    raw = urllib.request.urlopen(req, timeout=25, context=_ctx()).read()
    return json.loads(raw.decode("utf-8", "ignore"))


def fetch_cpi(page_size=500):
    """拉取全国 CPI 月度序列(升序)。rows: {period, yoy, mom, base_index, cum_index,
    city_yoy, rural_yoy}。yoy/mom 单位 %(如 0.5=+0.5%), base/cum 为指数(上年同月=100)。"""
    url = ("%s?reportName=RPT_ECONOMY_CPI&columns=ALL&pageNumber=1&pageSize=%d"
           "&sortColumns=REPORT_DATE&sortTypes=1" % (_EM, page_size))
    try:
        d = _http_get_json(url, CPI_REF)
    except Exception as e:
        return {"ok": False, "feed": "cpi", "message": str(e)}
    if not d.get("success") or not (d.get("result") or {}).get("data"):
        return {"ok": False, "feed": "cpi", "message": "东财 CPI 接口无数据"}
    rows = []
    for r in d["result"]["data"]:
        period = str(r.get("REPORT_DATE") or "")[:7]  # YYYY-MM
        if len(period) != 7:
            continue
        def f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        rows.append({
            "period": period,
            "yoy": f(r.get("NATIONAL_SAME")),        # 全国同比 %
            "mom": f(r.get("NATIONAL_SEQUENTIAL")),  # 全国环比 %
            "base_index": f(r.get("NATIONAL_BASE")), # 全国指数(上年同月=100)
            "cum_index": f(r.get("NATIONAL_ACCUMULATE")),
            "city_yoy": f(r.get("CITY_SAME")),
            "rural_yoy": f(r.get("RURAL_SAME")),
        })
    rows = [x for x in rows if x["yoy"] is not None or x["mom"] is not None]
    return {"ok": True, "feed": "cpi", "count": len(rows),
            "rows": rows, "latest": rows[-1]["period"] if rows else None,
            "source": SOURCE_TXT, "source_url": CPI_SRC_URL}


def fetch_ppi(page_size=500):
    """拉取 PPI(工业生产者出厂价格)月度序列(升序)。rows: {period, yoy, base_index,
    cum_index}。yoy 单位 %; base/cum 为指数。"""
    url = ("%s?reportName=RPT_ECONOMY_PPI&columns=ALL&pageNumber=1&pageSize=%d"
           "&sortColumns=REPORT_DATE&sortTypes=1" % (_EM, page_size))
    try:
        d = _http_get_json(url, PPI_REF)
    except Exception as e:
        return {"ok": False, "feed": "ppi", "message": str(e)}
    if not d.get("success") or not (d.get("result") or {}).get("data"):
        return {"ok": False, "feed": "ppi", "message": "东财 PPI 接口无数据"}
    rows = []
    for r in d["result"]["data"]:
        period = str(r.get("REPORT_DATE") or "")[:7]
        if len(period) != 7:
            continue
        def f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        rows.append({
            "period": period,
            "yoy": f(r.get("BASE_SAME")),        # 当月同比 %
            "base_index": f(r.get("BASE")),      # 出厂价格指数(上年同月=100)
            "cum_index": f(r.get("BASE_ACCUMULATE")),
        })
    rows = [x for x in rows if x["yoy"] is not None]
    return {"ok": True, "feed": "ppi", "count": len(rows),
            "rows": rows, "latest": rows[-1]["period"] if rows else None,
            "source": SOURCE_TXT, "source_url": PPI_SRC_URL}


def fetch_macro(page_size=500):
    """组合拉取 CPI + PPI。返回 {cpi: {...}, ppi: {...}}。"""
    return {"cpi": fetch_cpi(page_size=page_size),
            "ppi": fetch_ppi(page_size=page_size)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    r = fetch_macro()
    for k in ("cpi", "ppi"):
        v = r[k]
        print(k, "ok=", v.get("ok"), "count=", v.get("count"),
              "latest=", v.get("latest"), "msg=", v.get("message", ""))
        if v.get("ok") and v["rows"]:
            print("   latest row:", v["rows"][-1])
            print("   earliest  :", v["rows"][0])
