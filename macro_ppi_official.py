"""macro_ppi_official.py — PPI 全国环比(统计局官方发布稿)抓取器。

背景
  东方财富 RPT_ECONOMY_PPI 只转引了 PPI 同比/指数/累计, **不含环比**; 且 PPI 环比
  无法由同比序列数学反推(同比是滚动12月连乘, 环比信息欠定)。因此环比需另取官方一手源。

数据源
  国家统计局 www.stats.gov.cn/sj/zxfb/ 「最新发布」栏目, 每月上旬发布的
  《YYYY年M月份工业生产者出厂价格同比上涨/下降X%》稿, 正文附主数据表:
      环比涨跌幅(%) | 同比涨跌幅(%) | 1—N月同比涨跌幅(%)
    一、工业生产者出厂价格  -0.7   3.5   1.8
  首行 = 全国 PPI 口径(环比/同比/累计)。早年(约2013年前)指标名称为「工业品出厂价格」。

注意
  - 栏目为分页列表(index.html 最新, index_1.html 次页, ...), 全站 zxfb 共 4202 条/约210页。
  - 发布时间: 上月数据于次月 9~12 日发布(与 CPI/PPI 月度发布窗口一致)。
  - 官网 SSL 偶发握手失败, 统一带重试; 对 gov.cn 保持温和限速(0.4s)。
"""
import datetime
import re
import ssl
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://www.stats.gov.cn/sj/zxfb/"
SOURCE_TXT = "国家统计局《最新发布》月度稿(一手)"
# 发布稿标题的关键词: 2014 前后名称由「工业品出厂价格」改为「工业生产者出厂价格」
_TITLE_KW = ("工业生产者出厂价格", "工业品出厂价格")
# 正文表格首行定位关键词(兼容新旧名称)
_ROW_KW = ("一、工业生产者出厂价格", "一、工业品出厂价格")


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def http_get(url, tries=3, timeout=25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            return urllib.request.urlopen(req, timeout=timeout, context=_ctx()).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _period_from_title(title):
    """从标题提取数据所属月份 'YYYY-MM'。形如 '2026年7月份工业生产者出厂价格同比上涨3.5%'。"""
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", title)
    if not m:
        return None
    return "%s-%02d" % (m.group(1), int(m.group(2)))


def parse_article(html):
    """解析发布稿正文 → {period, mom, yoy, cum}。找不到主表首行返回 None。"""
    txt = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.S)
    for r in rows:
        raw_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)
        cells = []
        for c in raw_cells:
            t = re.sub(r"<[^>]+>", "", c)     # 去标签(td>p>span)
            t = t.replace("&nbsp;", "").replace("&#160;", "")
            t = re.sub(r"\s+", "", t)
            cells.append(t)
        if not cells:
            continue
        head = cells[0]
        # 兼容标题所在行带序号/空格/说明性括号
        if any(k in head for k in _ROW_KW) and len(cells) >= 3:
            def f(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            return {"mom": f(cells[1]), "yoy": f(cells[2]),
                    "cum": f(cells[3]) if len(cells) >= 4 else None}
    return None


def _extract_title_links(html):
    """栏目页 → [(title, abs_url)]，只留含发布稿关键词的文章。"""
    out = []
    base = BASE
    for m in re.finditer(r'<a[^>]+href="([^"]+\.html)"[^>]*>(.*?)</a>', html, re.S):
        href, body = m.group(1), m.group(2)
        title = re.sub(r"<[^>]+>", "", body)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        if any(k in title for k in _TITLE_KW) and re.search(r"20\d{2}\s*年", title):
            url = href if href.startswith("http") else (base.rstrip("/") + "/" + href.lstrip("./"))
            out.append((title, url))
    return out


def scan_list_pages(start=0, pages=10):
    """扫栏目列表页(start 为偏移页码, index.html=0), 返回全部 (title,url,period)。"""
    hits = []
    for off in range(start, start + pages):
        url = BASE if off == 0 else "%sindex_%d.html" % (BASE, off)
        try:
            html = http_get(url, tries=2)
        except Exception:
            break  # 页码耗尽或网络失败即停
        page_hits = _extract_title_links(html)
        hits.extend((t, u, _period_from_title(t)) for t, u in page_hits)
        time.sleep(0.4)
    # 去重(同一 href 多次出现), 保序
    seen, dedup = set(), []
    for t, u, p in hits:
        if u in seen:
            continue
        seen.add(u)
        dedup.append((t, u, p))
    return dedup


def fetch_history(max_pages=260, until="200501", progress=None):
    """扫描官方发布稿并解析为行列表(降序时间, 便于看进度)。
    返回 {ok, count, rows:[{period,mom,yoy,cum,source_url,title}], scanned_pages, earliest}。
    """
    rows, scanned = [], 0
    for off in range(max_pages):
        url = BASE if off == 0 else "%sindex_%d.html" % (BASE, off)
        try:
            html = http_get(url, tries=2)
        except Exception:
            break
        scanned = off + 1
        # 注: 列表页侧栏/推荐区会混入旧稿链接, 不能以"页面出现早于 until 的稿"作边界,
        # 统一以 max_pages 为界扫描, per-article 用 until 过滤。
        for title, link in _extract_title_links(html):
            period = _period_from_title(title)
            if period and period < until:
                continue
            try:
                art = http_get(link, tries=2)
                d = parse_article(art)
            except Exception:
                d = None
            if d and period:
                d.update({"period": period, "source_url": link, "title": title})
                rows.append(d)
            time.sleep(0.3)
        time.sleep(0.4)
        if progress and off % 20 == 0:
            progress(off + 1, len(rows))
    # 去重按 period
    by_p = {}
    for r in rows:
        by_p.setdefault(r["period"], r)
    rows = [by_p[k] for k in sorted(by_p)]
    return {"ok": True, "count": len(rows), "rows": rows,
            "scanned_pages": scanned,
            "earliest": rows[0]["period"] if rows else None,
            "latest": rows[-1]["period"] if rows else None,
            "source": SOURCE_TXT}


def fetch_latest(scan_pages=3):
    """增量: 只抓最新一期 PPI 发布稿(栏目前若干页内必有当月/上月稿)。
    返回单行 dict 或 None。"""
    for off in range(scan_pages):
        url = BASE if off == 0 else "%sindex_%d.html" % (BASE, off)
        try:
            html = http_get(url, tries=2)
        except Exception:
            continue
        hits = _extract_title_links(html)
        if hits:
            title, link = hits[0][0], hits[0][1]
            period = _period_from_title(title)
            try:
                d = parse_article(http_get(link, tries=3))
            except Exception:
                d = None
            if d and period:
                d.update({"period": period, "source_url": link, "title": title})
                return d
        time.sleep(0.2)
    return None


if __name__ == "__main__":
    # 自检: 解析最新一篇
    latest = fetch_latest(scan_pages=6)
    print("latest:", latest)
