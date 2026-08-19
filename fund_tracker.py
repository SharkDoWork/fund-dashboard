# -*- coding: utf-8 -*-
"""
基金实时涨跌幅跟踪引擎
========================
标的:
  - 招商中证畜牧养殖ETF (516670, 场内ETF, 代码 sh516670)
  - 中欧医疗健康混合A (003095, 场外主动混合)

方法论:
  1) 每日首次运行 -> 获取"前一天收盘后公布"的准确数据作为当日基准(baseline)并落盘锁定
     - ETF  : 昨日收盘价(Sina/push2 昨收) + 官方净值(LSJZ)
     - 混合  : 昨日官方单位净值 + 官方涨跌幅(LSJZ, 天天基金数据源)
  2) 当日后续每次运行 -> 以锁定基线为锚, 用最新持仓实时行情加权估算基金当日涨跌幅:
     est_change = sum(weight_i * stock_change_i) / sum(weight_i)  (按已披露前十大/前二十大口径)
     余量(未披露部分)用行业指数涨跌近似, 记为 residual_change
  3) 交叉校验: ETF 用场内实时价(push2/Sina), 混合基金用新浪估算净值(fu_003095)
  4) 随实时数据更新, 预估结果动态调整; 每次运行输出带时间戳快照

数据源(均为公开接口, 输出中标注):
  - 持仓明细: fundf10.eastmoney.com FundArchivesDatas (天天基金, 2026-06-30 披露)
  - 官方净值: api.fund.eastmoney.com/f10/lsjz
  - 实时行情: hq.sinajs.cn (新浪) / push2.eastmoney.com (东方财富)
  - 盘中估算: hq.sinajs.cn fu_ 基金估算 (新浪)
"""
import json, os, re, ssl, sys, time, urllib.request, datetime
import fund_db
from manage_funds import auto_tags, fetch_fund_meta, merge_tags, auto_detect_anchor

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
SNAP = os.path.join(DATA, "snapshots")
os.makedirs(SNAP, exist_ok=True)
CONFIG = os.path.join(BASE, "config", "funds.json")

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
EM_F10_REF = "https://fundf10.eastmoney.com/"

# ---------------------------------------------------------------- 基金配置(动态, 支持任意基金)
def load_fund_config():
    """基金配置: 优先数据库(app_kv), 回退磁盘 JSON 并自动迁移"""
    v = fund_db.kv_get("funds.json")
    if v is not None:
        return v.get("funds", {}) if isinstance(v, dict) else {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            fund_db.kv_set("funds.json", cfg)
            return cfg.get("funds", {})
        except Exception:
            pass
    return {}

def guess_fund_type(code):
    """代码规则自动判断: 5xxxxx(沪)/15/16xxxx(深) 为场内ETF, 其余为场外混合"""
    return "ETF" if code.startswith(("51", "56", "58", "15", "16")) else "MUTUAL"

def build_funds():
    """合并配置, 构建每只基金的数据源元信息(持仓/净值/行情/估算/行业指数均可动态适配)"""
    cfg = load_fund_config()
    funds = {}
    for code, c in cfg.items():
        code = str(code).strip()
        if not code.isdigit() or len(code) != 6:
            continue
        if c.get("hidden"):
            continue  # 伪删除: 面板不可见, 数据保留在库
        ftype = c.get("type") or guess_fund_type(code)
        is_sh = code.startswith(("5", "6", "9"))
        meta = {"name": c.get("name") or f"基金{code}", "type": ftype,
                "position": {"buy_amount": c.get("buy_amount") or 0,
                             "shares": c.get("shares"), "buy_nav": c.get("buy_nav")},
                "anchor_tencent": c.get("anchor_tencent"), "anchor_name": c.get("anchor_name"),
                "meta": c.get("meta"),
                "buy_fee_rate": c.get("buy_fee_rate") or 0, "sell_fee_rate": c.get("sell_fee_rate") or 0,
                "tags": auto_tags(c.get("name") or "", ftype, c.get("anchor_tencent"), c.get("tags"))}
        if ftype == "ETF":
            meta["sina"] = ("sh" if is_sh else "sz") + code
            meta["secid"] = ("1." if is_sh else "0.") + code
            meta["fu"] = None
            meta["index_name"] = "中证消费(近似)" if code == "516670" else None
            meta["index_sina"] = "sh000932" if code == "516670" else None
            meta["index_sina_candidates"] = []
            meta["fallback_index"] = "sh000932" if code == "516670" else None
        else:
            meta["sina"] = None
            meta["secid"] = None
            meta["fu"] = "fu_" + code
            meta["index_name"] = "中证医疗" if code == "003095" else None
            meta["index_sina"] = "sz399989" if code == "003095" else None
            meta["index_sina_candidates"] = []
            meta["fallback_index"] = "sz399989" if code == "003095" else None
        funds[code] = meta
    return funds

FUNDS = build_funds()

def refresh_fund_meta(ttl_days=7):
    """定期刷新基金元数据(名称/类型/主题标签): meta 缺失或超 TTL 时重新拉取。
    用户手改标签保留(merge_tags 追加不覆盖), 写回 funds.json。返回刷新基金数。"""
    try:
        cfg = fund_db.kv_get("funds.json") or {"funds": {}}
        funds = cfg.get("funds", {})
        n = 0
        for code, c in funds.items():
            if not isinstance(c, dict):
                continue
            m = c.get("meta") or {}
            old = m.get("fetched_ts") or 0
            if old and (time.time() - old) < ttl_days * 86400:
                continue
            meta = fetch_fund_meta(code)
            if not meta:
                continue
            c["meta"] = {k: meta.get(k) for k in
                         ("company", "manager", "ftype", "themes", "nav", "nav_date", "fetched_ts")}
            if meta.get("name") and not c.get("name"):
                c["name"] = meta["name"]
            if meta.get("buy_fee_pct") is not None:
                c["buy_fee_rate"] = round(meta["buy_fee_pct"] / 100.0, 4)  # 申购费率自动更新
            c["tags"] = merge_tags(c.get("tags") or [], meta.get("ftype"), meta.get("themes"))
            n += 1
        if n:
            fund_db.kv_set("funds.json", cfg)
            print(f"  [meta] 已刷新 {n} 只基金元数据/自动标签")
        return n
    except Exception as e:
        print(f"  [warn] 基金元数据刷新失败: {e}")
        return 0

# 交易记录(每基金买入/卖出, 由 run() 填充)
TRADES_BY_CODE = {}
TRADE_SUMMARY = {}

def http_get(url, ref=None, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            h = {"User-Agent": UA, "Accept": "*/*"}
            if ref: h["Referer"] = ref
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk"):
                try: return raw.decode(enc)
                except UnicodeDecodeError: continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e; time.sleep(1.2)
    raise last

def sina_symbol(code):
    """A股/ETF 6位代码 -> 新浪前缀"""
    code = str(code).strip()
    if code.startswith(("60", "68", "90", "51", "50", "52", "56", "58", "11")):  # 60/68 沪股, 51x 沪ETF, 113 可转债
        return "sh" + code
    return "sz" + code

# ---------------------------------------------------------------- 持仓(每日更新: 缓存1天)
def _anchor_etf_code(code):
    """联接基金的锚ETF代码(6位场内ETF), 用于穿透其持仓。指数锚(sh000510等)返回None。"""
    meta = FUNDS.get(code)
    at = (meta or {}).get("anchor_tencent") or ""
    m = re.search(r"\d{6}", at)
    if not m:
        return None
    c = m.group(0)
    # 只认场内ETF代码(51/56/58/15/16开头); 指数代码(000/399等)不算, 避免误穿透
    return c if c.startswith(("51", "56", "58", "15", "16")) else None

def _load_holdings_cache(etf):
    v = fund_db.kv_get(f"holdings_{etf}.json")
    return v if v is not None else None

def fetch_direct_holdings(code):
    """抓取某基金【直接】持仓: 股票(来自jjcc) + 子基金(联接基金锚定ETF, 来自配置)。
    写入 fund_holdings 表(持仓关系表, 每次刷新即更新关联), 供 aggregate_stocks 递归穿透。
    返回 {code, fund_name, quarter, report_date, stock_rows, fund_rows, fetched_ts, source}"""
    cache_key = f"holdings_{code}.json"
    cj = fund_db.kv_get(cache_key)
    if cj is not None:
        # 仅当为新结构(含 stock_rows)且 <1天 时复用缓存
        if isinstance(cj, dict) and "stock_rows" in cj and (time.time() - cj.get("fetched_ts", 0)) / 86400 < 1:
            return cj
    url = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=30&year=&month="
    txt = http_get(url, ref=EM_F10_REF)
    m = re.search(r"截止至：<font class='px12'>([\d\-]+)</font>", txt)
    report_date = m.group(1) if m else ""
    mq = re.search(r"(\d{4})年(\d)季度股票投资明细", txt)
    quarter = f"{mq.group(1)}Q{mq.group(2)}" if mq else ""
    mn = re.search(r"<a title='([^']+)' href='http://fund\.eastmoney\.com/{}\.html'>".format(code), txt)
    fund_name = mn.group(1) if mn else code
    stock_rows = []
    body = re.search(r"<tbody>(.*?)</tbody>", txt, re.S)
    if body:
        for tr in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S):
            mc = re.search(r"unify/r/\d\.(\d{6})", tr)
            if not mc: continue
            mw = re.findall(r"<td class='tor'>([\d\.]+)%</td>", tr)
            mnm = re.search(r"<td class='tol'><a[^>]*>([^<]+)</a></td>", tr)
            weight = float(mw[0]) if mw else None
            stock_rows.append({
                "code": mc.group(1), "name": mnm.group(1) if mnm else mc.group(1),
                "weight_pct": weight, "sina": sina_symbol(mc.group(1)),
            })
    # 子基金持仓(联接基金锚定ETF): 免费F10不披露"持有ETF比例", 由配置 etf_ratio 推导
    # (真实季报比例可在 config 填 etf_ratio + etf_ratio_real:true; 否则用合同下限0.90并标注"估算")
    fund_rows = []
    etf = _anchor_etf_code(code)
    if etf and etf != code:
        ratio = (FUNDS.get(code) or {}).get("etf_ratio")
        ratio_real = (FUNDS.get(code) or {}).get("etf_ratio_real", 0)
        if ratio is None:
            ratio, ratio_real = 0.90, 0
        fund_rows.append({
            "target_code": etf,
            "target_name": (FUNDS.get(code) or {}).get("anchor_name") or etf,
            "weight_pct": round(ratio * 100, 2),
            "ratio_real": 1 if ratio_real else 0,
            "quarter": quarter, "report_date": report_date,
        })
    # 写入持仓关系表(fund_holdings) —— 以表为唯一真相源, 刷新即更新所有关联基金
    db_rows = [("STOCK", r["code"], r["name"], r["weight_pct"], 0, quarter, report_date) for r in stock_rows]
    db_rows += [("FUND", r["target_code"], r["target_name"], r["weight_pct"], r["ratio_real"], r["quarter"], r["report_date"]) for r in fund_rows]
    try:
        fund_db.replace_fund_holdings(code, db_rows)
    except Exception as e:
        print(f"  [warn] fund_holdings 写入失败 {code}: {e}")
    out = {"code": code, "fund_name": fund_name, "quarter": quarter, "report_date": report_date,
           "stock_rows": stock_rows, "fund_rows": fund_rows, "fetched_ts": time.time(),
           "source": "天天基金 FundArchivesDatas + 配置锚定"}
    fund_db.kv_set(cache_key, out)
    return out

def aggregate_stocks(code, direct_map, visited=None):
    """递归穿透聚合某基金的所有底层股票权重(%):
       - 直接股票: 累加其 weight_pct
       - 持有子基金(权重 r%): 递归聚合子基金, 路径权重 = r/100 × 子基金下层权重
       - 同一股票经多条路径持有: 权重求和(解决"多基金持有同一股票比例不同")
       - 环检测: visited 防止 A→B→A 死循环
       返回 {stock_code: 聚合权重%}"""
    if visited is None: visited = set()
    if code in visited:
        return {}
    d = direct_map.get(code)
    if not d:
        return {}
    acc = {}
    for s in d.get("stock_rows", []):
        acc[s["code"]] = acc.get(s["code"], 0) + (s.get("weight_pct") or 0)
    for fr in d.get("fund_rows", []):
        tgt = fr["target_code"]
        link = (fr.get("weight_pct") or 0) / 100.0
        sub = aggregate_stocks(tgt, direct_map, visited | {code})
        for sc, sw in sub.items():
            acc[sc] = acc.get(sc, 0) + link * sw
    return acc

# ---------------------------------------------------------------- 实时行情
def fetch_sina_batch(symbols):
    """批量新浪行情, 返回 {symbol: {name, open, prev_close, current, high, low, date, time}}"""
    out = {}
    syms = [s for s in symbols if s]
    for i in range(0, len(syms), 50):
        chunk = syms[i:i+50]
        txt = http_get(f"https://hq.sinajs.cn/list={','.join(chunk)}", ref="https://finance.sina.com.cn/", timeout=15)
        for line in txt.splitlines():
            m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
            if not m: continue
            sym, payload = m.group(1), m.group(2)
            if not payload: continue
            p = payload.split(",")
            try:
                out[sym] = {
                    "name": p[0], "open": float(p[1]), "prev_close": float(p[2]),
                    "current": float(p[3]), "high": float(p[4]), "low": float(p[5]),
                    "date": p[30] if len(p) > 30 else "", "time": p[31] if len(p) > 31 else "",
                }
            except (ValueError, IndexError):
                continue
    return out

def fetch_push2(secid):
    # 东财 push2 对高频请求较敏感: 请求前稍等, 失败返回 None(仅作交叉校验, 不影响主流程)
    time.sleep(1.0)
    try:
        txt = http_get(f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
                       f"&fields=f43,f57,f58,f60,f169,f170&ut=fa5fd1943c7b386f172d6893dbfba10b&invt=2&fltt=2",
                       ref="https://quote.eastmoney.com/", timeout=15, tries=2)
    except Exception:
        return None
    try:
        j = json.loads(txt)
    except Exception:
        return None
    d = j.get("data")
    if not d: return None
    return {"name": d.get("f58"), "current": d.get("f43"), "prev_close": d.get("f60"),
            "chg": d.get("f169"), "chg_pct": d.get("f170")}

# ---------------------------------------------------------------- 官方净值(基线)
def fetch_lsjz(code, n=5):
    url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize={n}"
           f"&startDate=&endDate=&_={int(time.time()*1000)}")
    txt = http_get(url, ref=EM_F10_REF)
    j = json.loads(txt)
    lst = (j.get("Data") or {}).get("LSJZList") or []
    return [{"date": x.get("FSRQ"), "dwjz": float(x.get("DWJZ")), "ljjz": float(x.get("LJJZ") or 0),
             "chg_pct": float(x.get("JZZZL")) if x.get("JZZZL") not in (None, "") else None} for x in lst]

def fetch_nav_full(code):
    """全量历史净值(成立以来): fund.eastmoney.com/pingzhongdata 一次拉全, 升序"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    txt = http_get(url, ref=f"https://fund.eastmoney.com/{code}.html", timeout=30, tries=2)
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m:
        return []
    arr = json.loads(m.group(1))
    out = []
    for it in arr:
        try:
            d = (datetime.datetime.utcfromtimestamp(it["x"] / 1000) + datetime.timedelta(hours=8)).date().isoformat()
            out.append({"trade_date": d, "dwjz": float(it["y"]),
                        "official_chg_pct": it.get("equityReturn")})
        except (KeyError, TypeError, ValueError):
            continue
    return out

# ---------------------------------------------------------------- 新浪基金估算净值
def fetch_fu(fu_code):
    txt = http_get(f"https://hq.sinajs.cn/list={fu_code}", ref="https://finance.sina.com.cn/", timeout=15)
    m = re.search(r'"(.*)"', txt)
    if not m or not m.group(1): return None
    p = m.group(1).split(",")
    if len(p) < 7: return None
    try:
        return {"name": p[0], "time": p[1], "gsz": float(p[2]), "dwjz": float(p[3]),
                "ljjz": float(p[4]), "gszzl": float(p[6]) if len(p) > 6 and p[6] else None,
                "date": p[7] if len(p) > 7 else ""}
    except ValueError:
        return None

# ---------------------------------------------------------------- 基线管理(每日首次锁定)
def _nav_to_db(r, code):
    """LSJZ 记录(date/dwjz/ljjz/chg_pct) -> 库表字段(trade_date/official_chg_pct)"""
    return {"trade_date": r.get("date"), "fund_code": code, "dwjz": r.get("dwjz"),
            "ljjz": r.get("ljjz"), "official_chg_pct": r.get("chg_pct"),
            "source": "天天基金 api.fund.eastmoney.com/f10/lsjz"}

def ensure_nav_history(code, cached_navs=None):
    """保证历史库有全量净值:
       - 新基金(记录<100)或 多日未同步(末条净值距今>5天) -> pingzhongdata 全量拉取补齐
       - 否则 -> 增量拉最近30天。返回 (records_upserted, is_full)"""
    _mn, mx, cnt = fund_db.get_nav_range(code)
    stale_days = 999
    if mx:
        try:
            stale_days = (datetime.date.today() - datetime.date.fromisoformat(mx)).days
        except Exception:
            stale_days = 999
    if cnt < 100 or stale_days > 5:
        full = fetch_nav_full(code)
        if full:
            fund_db.upsert_nav([dict(r, fund_code=code, source="东财 pingzhongdata 全量历史")
                                for r in full])
            return len(full), True
    nav = cached_navs or (fetch_lsjz(code, n=30) if False else None)
    if nav is None:
        try:
            nav = fetch_lsjz(code, n=30)
        except Exception:
            nav = []
    fund_db.upsert_nav([_nav_to_db(r, code) for r in nav])
    return len(nav), False

def baseline_path(day):
    """兼容保留: 基线数据已入库(app_kv), 此函数返回磁盘路径仅用于说明"""
    return os.path.join(DATA, f"baseline_{day}.json")

def _baseline_key(day):
    return f"baseline_{day}.json"

def load_or_create_baseline(day, funds_meta):
    """每日首次运行: 拉取前一日收盘准确数据并落盘; 之后运行: 读取锁定基线。
    同时把拉取到的官方净值全量 upsert 进历史库(只增不删)。
    基线数据统一存数据库(app_kv: baseline_<day>.json), 兼容迁移自磁盘 JSON。"""
    p = baseline_path(day)
    base = fund_db.kv_get(_baseline_key(day))
    if base is None and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = None
    if base is not None:
        changed = False
        for code, meta in funds_meta.items():
            if meta.get("type") == "INDEX":
                # 指数标的无基金净值: 跳过净值拉取(基线为空, 实时点位由 INDEX 分支提供)
                if code not in base["funds"]:
                    base["funds"][code] = {"fund_code": code, "fund_name": meta["name"], "baseline": {},
                                           "nav_history": [], "source": "指数实时点位(无基金净值)"}
                    changed = True
                continue
            if code not in base["funds"]:
                # 当日中途新增基金: 补拉净值并锁定基线, 并入当日基线
                try:
                    nav = fetch_lsjz(code, n=30)
                except Exception:
                    nav = []
                prev = nav[0] if nav else None
                entry = {"fund_code": code, "fund_name": meta["name"], "baseline": {}, "nav_history": nav,
                         "source": "天天基金 api.fund.eastmoney.com/f10/lsjz"}
                if prev:
                    entry["baseline"] = {"date": prev["date"], "nav": prev["dwjz"], "ljjz": prev["ljjz"],
                                         "official_chg_pct": prev["chg_pct"],
                                         "desc": f"{prev['date']} 官方单位净值 {prev['dwjz']:.4f}, 官方涨跌幅 {prev['chg_pct']}%"}
                base["funds"][code] = entry
                changed = True
            # 历史净值持续累积: 全量不足时拉全量(pingzhongdata), 否则增量拉最近30天
            try:
                ensure_nav_history(code)
            except Exception:
                pass
        if changed:
            fund_db.kv_set(_baseline_key(day), base)
        return base, False
    base = {"day": day, "funds": {}, "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
    for code, meta in funds_meta.items():
        if meta.get("type") == "INDEX":
            base["funds"][code] = {"fund_code": code, "fund_name": meta["name"], "baseline": {},
                                   "nav_history": [], "source": "指数实时点位(无基金净值)"}
            continue
        nav = fetch_lsjz(code, n=30)
        prev = nav[0] if nav else None  # 最近一个已公布净值日 = 前一日收盘(若今天盘中未公布今日净值)
        entry = {"fund_code": code, "fund_name": meta["name"], "baseline": {}, "nav_history": nav,
                 "source": "天天基金 api.fund.eastmoney.com/f10/lsjz"}
        if prev:
            entry["baseline"] = {"date": prev["date"], "nav": prev["dwjz"], "ljjz": prev["ljjz"],
                                 "official_chg_pct": prev["chg_pct"],
                                 "desc": f"{prev['date']} 官方单位净值 {prev['dwjz']:.4f}, 官方涨跌幅 {prev['chg_pct']}%"}
        base["funds"][code] = entry
        # 历史净值全量入库(持续累积, 留作判断数据/曲线绘制)
        fund_db.upsert_nav([_nav_to_db(r, code) for r in nav])
        try:
            ensure_nav_history(code, cached_navs=nav)
        except Exception:
            pass
    fund_db.kv_set(_baseline_key(day), base)
    return base, True

# ---------------------------------------------------------------- 市场状态
def market_status(now=None):
    now = now or datetime.datetime.now()
    wd = now.weekday()
    hm = now.hour * 60 + now.minute
    if wd >= 5:
        return "休市(周末)"
    if 9 * 60 + 15 <= hm <= 11 * 60 + 30:
        return "盘中(上午)"
    if 13 * 60 <= hm <= 15 * 60:
        return "盘中(下午)"
    if 9 * 60 + 30 <= hm < 9 * 60 + 15:
        return "集合竞价"
    if 11 * 60 + 30 < hm < 13 * 60:
        return "午间休市"
    if hm > 15 * 60:
        return "已收盘"
    return "盘前"

# ---------------------------------------------------------------- 交易记录(买入/卖出)
def load_trades_summary():
    """读交易记录(数据库 app_kv, 兼容迁移自 config/trades.json): 按基金分组(日期升序) + 汇总"""
    by_code, summary = {}, {}
    v = fund_db.kv_get("trades.json")
    if v is not None:
        trades = v.get("trades", []) if isinstance(v, dict) else []
    else:
        try:
            with open(os.path.join(BASE, "config", "trades.json"), encoding="utf-8") as f:
                t = json.load(f)
            fund_db.kv_set("trades.json", t)
            trades = t.get("trades", [])
        except Exception:
            trades = []
    for tr in sorted(trades, key=lambda x: (x.get("date", ""), x.get("id", ""))):
        code = tr.get("code")
        if not code:
            continue
        by_code.setdefault(code, []).append(tr)
    for code, lst in by_code.items():
        buy_sh = sum(x["shares"] for x in lst if x["type"] == "buy")
        sell_sh = sum(x["shares"] for x in lst if x["type"] == "sell")
        buy_amt = sum(x["amount"] for x in lst if x["type"] == "buy")
        sell_amt = sum(x["amount"] for x in lst if x["type"] == "sell")
        div_amt = sum(x.get("amount", 0) for x in lst if x["type"] == "dividend")
        summary[code] = {"buy_amount": round(buy_amt, 2), "sell_amount": round(sell_amt, 2),
                         "buy_shares": round(buy_sh, 4), "sell_shares": round(sell_sh, 4),
                         "dividend_amount": round(div_amt, 2),
                         "remain_shares": round(buy_sh - sell_sh, 4), "count": len(lst)}
    return by_code, summary

# ---------------------------------------------------------------- 个人持仓金额指标
def compute_position(code, meta, fobj, nav_history):
    """
    个人持仓金额指标 —— 优先按历史买卖记录(买入/卖出/分红)推导, 无记录时回退手动配置。
    交易推导(移动加权平均成本法, 按成交日期升序):
      - 剩余份额   = Σ买入份额 - Σ卖出份额
      - 持仓成本   = 剩余份额 × 移动加权平均成本单价(买入更新成本, 卖出只减份额不减单价)
      - 当前金额(昨收) = 剩余份额 × 昨日官方净值
      - 持有收益   = 当前金额 - 持仓成本;  持有收益率 = 持有收益 / 持仓成本
      - 已实现收益 = Σ(卖出金额 - 卖出份额×当时平均成本) + Σ分红金额(现金分红为纯已实现收入)
      - 累计收益   = 已实现收益 + 持有收益(未实现);  累计收益率 = 累计收益 / 累计买入金额
      - 昨日收益   = 剩余份额 × (昨日净值 - 前日净值)
      - 当日金额变化 = 预估涨跌幅% × 当前金额(用户定义)
      分红(dividend): 只有金额, 无份额/净值; 计入已实现收益, 不动份额与成本, 手续费为0
    """
    trades = TRADES_BY_CODE.get(code, [])
    cfg = meta.get("position") or {}
    buy_amount = cfg.get("buy_amount") or 0
    shares = cfg.get("shares")
    buy_nav = cfg.get("buy_nav")

    # ---- 路径1: 有买卖记录 -> 按交易推导 ----
    if trades:
        sh = 0.0; cost = 0.0; avg = 0.0; realized = 0.0; buy_amt = 0.0
        total_fee = 0.0; dividend_total = 0.0
        fee_buy = meta.get("buy_fee_rate") or 0
        fee_sell = meta.get("sell_fee_rate") or 0
        for tr in trades:
            ttype = tr["type"]
            fee = tr.get("fee")
            if fee is None:
                if ttype == "dividend":
                    fee = 0.0  # 分红无手续费
                else:
                    fee = tr["amount"] * (fee_buy if ttype == "buy" else fee_sell)  # 历史无费记录按费率估算
            total_fee += fee or 0
            if ttype == "buy":
                sh += tr["shares"]; cost += tr["amount"]; buy_amt += tr["amount"]
                avg = cost / sh if sh > 0 else 0.0
            elif ttype == "sell":
                realized += tr["amount"] - tr["shares"] * avg
                sh = max(0.0, sh - tr["shares"])
                cost = sh * avg
            elif ttype == "dividend":
                # 现金分红: 纯已实现现金收入, 不影响份额/成本
                realized += tr["amount"]
                dividend_total += tr["amount"]
        # 浮点除尘: 残余份额小于 1e-6 视为精确归零(清理/清仓后浮点舍入残留易触发无意义的持有收益率,
        # 如 0.0014-1.0 等; 1e-6 份额≈0 价值, 远低于真实持仓, 不影响任何实际持仓计算)
        if sh < 1e-6:
            sh = 0.0
            cost = 0.0
        pos = {"configured": sh > 0 or buy_amt > 0, "source": "trades",
               "buy_amount": round(buy_amt, 2),
               "shares": round(sh, 4), "avg_cost_nav": round(avg, 4),
               "cost_amount": round(cost, 2), "realized_gain": round(realized, 2),
               "dividend_total": round(dividend_total, 2),
               "total_fee": round(total_fee, 2)}
        if not nav_history:
            return pos
        d0 = nav_history[0]; d1 = nav_history[1] if len(nav_history) > 1 else None
        pos["last_nav_date"] = d0.get("date"); pos["last_nav"] = d0.get("dwjz")
        if d1: pos["prev_nav"] = d1.get("dwjz")
        if sh > 0 and d0.get("dwjz"):
            cur = sh * d0["dwjz"]
            pos["current_amount"] = round(cur, 2)
            if d1 and d1.get("dwjz"):
                pos["yesterday_gain"] = round(sh * (d0["dwjz"] - d1["dwjz"]), 2)
            if cost > 0:
                pos["hold_gain"] = round(cur - cost, 2)
                pos["hold_gain_pct"] = round((cur - cost) / cost * 100, 2)
            pos["total_gain"] = round(realized + (cur - cost) - total_fee, 2)  # 累计收益=已实现+浮动-累计手续费
            if buy_amt > 0:
                pos["total_gain_pct"] = round((realized + cur - cost - total_fee) / buy_amt * 100, 2)
            est = fobj["est"].get("model_change_pct")
            if fobj["est"].get("anchor_based") and fobj["official"].get("anchor"):
                est = fobj["official"]["anchor"]["chg_pct"]
            if est is None:
                est = fobj["est"].get("adjusted_model_change_pct")
            if est is not None:
                pos["today_est_change"] = round(cur * est / 100, 2)
        elif buy_amt > 0:
            # 全部卖出清仓: 累计收益=已实现收益(当前金额为0)
            pos["current_amount"] = 0.0
            pos["total_gain"] = round(realized - total_fee, 2)  # 清仓: 已实现-累计手续费
            if buy_amt > 0:
                pos["total_gain_pct"] = round((realized - total_fee) / buy_amt * 100, 2)
        return pos

    # ---- 路径2: 无买卖记录 -> 回退手动配置(原逻辑) ----
    pos = {"configured": bool(buy_amount or shares or buy_nav), "source": "manual",
           "buy_amount": round(buy_amount, 2) if buy_amount else 0,
           "shares": shares, "buy_nav": buy_nav}
    if not nav_history:
        return pos
    d0 = nav_history[0]  # 最近一个已发布净值日(昨收)
    d1 = nav_history[1] if len(nav_history) > 1 else None
    pos["last_nav_date"] = d0.get("date")
    pos["last_nav"] = d0.get("dwjz")
    if d1:
        pos["prev_nav"] = d1.get("dwjz")
    eff_shares = shares
    if not eff_shares and buy_nav and buy_amount:
        eff_shares = buy_amount / buy_nav
        pos["shares_derived"] = round(eff_shares, 4)
    if eff_shares and d0.get("dwjz"):
        cur = eff_shares * d0["dwjz"]
        pos["current_amount"] = round(cur, 2)
        if d1 and d1.get("dwjz"):
            pos["yesterday_gain"] = round(eff_shares * (d0["dwjz"] - d1["dwjz"]), 2)
        if buy_amount:
            pos["hold_gain"] = round(cur - buy_amount, 2)
            pos["hold_gain_pct"] = round((cur - buy_amount) / buy_amount * 100, 2) if buy_amount else None
        pos["total_gain"] = pos.get("hold_gain")
        est = fobj["est"].get("model_change_pct")
        # 联接基金(披露股票覆盖极低)以跟踪锚(指数/目标ETF)实时为准
        if fobj["est"].get("anchor_based") and fobj["official"].get("anchor"):
            est = fobj["official"]["anchor"]["chg_pct"]
        if est is None:
            est = fobj["est"].get("adjusted_model_change_pct")
        if est is not None:
            pos["today_est_change"] = round(cur * est / 100, 2)  # 当日金额变化 = 预估涨跌幅 × 金额
    return pos


def _read_nav_history_desc(code):
    """只读库取净值历史(绝不联网), 返回 DESC(最新在前) 且键为 date/dwjz 的列表, 供 compute_position 使用。"""
    try:
        rows = fund_db.get_nav_series(code)  # ASC: trade_date, dwjz, official_chg_pct
    except Exception:
        rows = []
    out = [{"date": r[0], "dwjz": r[1], "chg_pct": r[2]} for r in rows]
    out.reverse()
    return out


# ---------------------------------------------------------------- 指数标的(399xxx)
def fetch_index_constituents(code):
    """腾讯行情: 指数成分股及实时行情(涨跌幅/最新价)。
    返回 [{code, name, chg_pct, price}, ...] 或 []"""
    sym = ("sz" if str(code).startswith("399") else "sh") + str(code)
    try:
        txt = http_get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?"
                       f"board_code={sym}&sort_type=price&direct=down&offset=0&count=100",
                       ref="https://stockapp.finance.qq.com/", timeout=20, tries=2)
        j = json.loads(txt)
        rows = ((j.get("data") or {}).get("rank_list")) or []
        out = []
        for r in rows:
            c = r.get("code") or ""
            m = re.match(r"^(sh|sz)(\d{6})$", c)
            if not m:
                continue
            def _f(x):
                try:
                    return float(x)
                except (ValueError, TypeError):
                    return None
            out.append({"code": m.group(2), "name": r.get("name") or m.group(2),
                        "chg_pct": _f(r.get("zdf")), "price": _f(r.get("zxj"))})
        return out
    except Exception:
        return []


def build_index_fund(code, meta, now, st, base_entry):
    """指数型标的(399xxx): 指数实时点位/涨跌 + 成分股列表及实时行情。返回 fobj"""
    sym = ("sz" if str(code).startswith("399") else "sh") + str(code)
    q = fetch_sina_batch([sym]).get(sym)
    constituents = fetch_index_constituents(code) or []
    # 成分股实时行情由腾讯 getBoardRankList 自带(zdf涨跌幅/zxj最新价)
    stocks = []
    for it in constituents:
        stocks.append({"code": it["code"], "name": it["name"], "weight_pct": None,
                       "sina": sina_symbol(it["code"]), "price": it["price"],
                       "prev_close": None, "chg_pct": it["chg_pct"], "quote_time": ""})
    official = {"type": "指数实时点位", "quote_time": "", "source": f"新浪行情 {sym}"}
    realtime = False
    if q and q["prev_close"] > 0 and q["current"] > 0:
        ichg = round((q["current"] - q["prev_close"]) / q["prev_close"] * 100.0, 2)
        official.update({"name": q["name"], "price": q["current"], "prev_close": q["prev_close"],
                         "chg_pct": ichg, "quote_time": f"{q['date']} {q['time']}"})
        realtime = True
    else:
        official["chg_pct"] = None
    be = base_entry or {}
    fobj = {
        "fund_code": code, "fund_name": meta.get("name") or code, "type": "INDEX",
        "self_sina": sym, "tags": meta.get("tags") or [],
        "holdings_quarter": "", "holdings_report_date": "",
        "holdings_penetrated": False, "holdings_direct": [],
        "baseline": be.get("baseline") or {"desc": "指数标的, 无基金净值基线"},
        "baseline_source": be.get("source") or "指数实时点位",
        "trades": TRADES_BY_CODE.get(code, []),
        "trade_summary": TRADE_SUMMARY.get(code, {}),
        "stocks": stocks, "est": {}, "official": official,
        "position": {"configured": False},
        "realtime": realtime,
    }
    fobj["est"]["disclosed_coverage_pct"] = 0.0
    fobj["est"]["model_change_pct"] = None
    if realtime:
        fobj["est"]["index"] = {"name": official["name"], "chg_pct": official["chg_pct"]}
        fobj["est"]["adjusted_model_change_pct"] = official["chg_pct"]
    else:
        fobj["est"]["adjusted_model_change_pct"] = None
    return fobj


def refresh_positions_only():
    """轻量刷新(交易增删/保存/添加基金后秒级更新看板, 不触发全量联网净值同步):
    仅用库内最新 trades.json 重算各基金 trades/trade_summary/position 并写回 latest.json 快照,
    保留原 nav_history/holdings/official/est 等, 避免 refresh_engine 的 ~13s 全量联网阻塞;
    对 funds.json 中尚未进入快照的新增基金, 仅抓取该基金净值(ensure_nav_history, ~1 请求)后
    补建最小可用快照条目(仓位/净值曲线立即可见, 持仓明细/实时行情待下次完整同步补充)。
    返回 True; 全新空库(无快照)时退化为完整引擎(run)。
    全程不重跑行情/估算建模, 因此保存/添加基金由 ~13s 降到 ~1s 级。"""
    global TRADES_BY_CODE, TRADE_SUMMARY
    fund_db.init_db()
    TRADES_BY_CODE, TRADE_SUMMARY = load_trades_summary()
    snap = fund_db.kv_get("latest.json")
    fc = fund_db.kv_get("funds.json") or {}
    FUNDS_CFG = fc.get("funds", {}) if isinstance(fc, dict) else {}
    # BUILD: 与 run() 一致的 meta(持仓金额在 meta["position"] 下), compute_position 才能正确读取手动配置;
    # 此处重算需用 BUILD 而非原始 funds.json, 否则手动(无交易)基金的持仓金额会被算成 0。
    BUILD = build_funds()
    if not snap or not (snap.get("funds")):
        # 无快照则退化为完整引擎(会联网建快照)
        return run()
    snap_funds = snap.setdefault("funds", {})
    # 1) 已有快照: 仅重算交易/持仓(不联网)
    for code, fobj in snap_funds.items():
        meta = BUILD.get(code) or FUNDS_CFG.get(code, {}) or {}
        if meta.get("type") == "INDEX":
            # 指数标的: 成分股/行情由完整引擎(run)或新增基金分支维护, 轻量刷新不动
            continue
        fobj.setdefault("est", {})
        fobj.setdefault("official", {})
        nav_history = _read_nav_history_desc(code)
        fobj["trades"] = TRADES_BY_CODE.get(code, [])
        fobj["trade_summary"] = TRADE_SUMMARY.get(code, {})
        fobj["position"] = compute_position(code, meta, fobj, nav_history)
    # 2) 新增基金(在 funds.json 但不在快照): 仅抓取该基金净值, 补建最小可用快照条目
    for code, meta_cfg in FUNDS_CFG.items():
        if meta_cfg.get("hidden"):
            continue
        if code in snap_funds:
            continue
        meta = BUILD.get(code) or {}
        if not meta:
            continue
        if meta.get("type") == "INDEX":
            # 新增指数: 直接抓成分股+行情建条目(联网, ~2 请求, 单指数秒级)
            try:
                snap_funds[code] = build_index_fund(code, meta, datetime.datetime.now(),
                                                    market_status(datetime.datetime.now()), {})
            except Exception:
                continue
            continue
        try:
            ensure_nav_history(code)  # 该基金净值入历史库(已有则增量更新, 仅 ~1 次请求)
        except Exception:
            # 抓取失败(离线/代码无效) -> 跳过该基金, 保留其他基金刷新成功
            continue
        nav_history = _read_nav_history_desc(code)
        name = meta.get("name") or f"基金{code}"
        fobj = {
            "fund_code": code, "fund_name": name, "type": meta.get("type") or "MUTUAL",
            "tags": meta.get("tags") or [],
            "holdings_quarter": "", "holdings_report_date": "",
            "holdings_penetrated": False, "holdings_direct": [], "holdings": [],
            "realtime": False,
            "baseline": {"desc": "新增基金, 待下次完整同步补充基线/持仓/实时行情"},
            "est": {}, "official": {}, "stocks": [],
            "trades": TRADES_BY_CODE.get(code, []),
            "trade_summary": TRADE_SUMMARY.get(code, {}),
            "position": compute_position(code, meta, {"est": {}, "official": {}}, nav_history),
        }
        snap_funds[code] = fobj
    snap["generated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    fund_db.kv_set("latest.json", snap)
    return True

# ---------------------------------------------------------------- 主流程
def run():
    fund_db.init_db()
    # 定期刷新基金元数据/标签(meta 缺失或超7天重新拉取, 用户手改标签保留)
    refresh_fund_meta()
    global FUNDS, TRADES_BY_CODE, TRADE_SUMMARY
    FUNDS = build_funds()  # 元数据/名称/锚更新后重建, 保证本次运行使用最新配置
    day = datetime.date.today().isoformat()
    now = datetime.datetime.now()
    st = market_status(now)
    # 交易记录(买入/卖出): 按基金分组升序 + 汇总
    TRADES_BY_CODE, TRADE_SUMMARY = load_trades_summary()
    base, is_first = load_or_create_baseline(day, FUNDS)
    print(f"== 基金实时涨跌幅跟踪 ==  {now:%Y-%m-%d %H:%M:%S}  [{st}]  首次运行(锁定基线)={is_first}")

    # 1) 直接持仓 -> fund_holdings 表 + 内存 direct_map(含被锚定的ETF, 即便不在主列表)
    direct_map = {}
    ref_etfs = set()
    for c in FUNDS:
        e = _anchor_etf_code(c)
        if e and e != c:
            ref_etfs.add(e)
    for c in list(FUNDS.keys()) + list(ref_etfs):
        if c not in direct_map:
            direct_map[c] = fetch_direct_holdings(c)

    # 2) 收集需要实时行情的所有代码(所有底层股票 + 指数近似)
    stock_meta = {}   # code -> {name, sina}
    for c, d in direct_map.items():
        for s in d.get("stock_rows", []):
            stock_meta[s["code"]] = {"name": s["name"], "sina": s["sina"]}
    all_syms = set()
    for sm in stock_meta.values():
        all_syms.add(sm["sina"])
    for c, meta in FUNDS.items():
        if meta.get("sina"): all_syms.add(meta["sina"])
        if meta.get("anchor_tencent"): all_syms.add(meta["anchor_tencent"])
        all_syms.add(meta.get("index_sina") or meta.get("fallback_index"))
        for cand in meta.get("index_sina_candidates", []):
            all_syms.add(cand)
    quotes = fetch_sina_batch(list(all_syms))

    # 3) ETF 场内实时价(东方财富 push2 交叉验证, 失败不影响主流程; 支持多只ETF)
    etf_push = {}
    for c, m in FUNDS.items():
        if m.get("type") == "ETF" and m.get("secid"):
            try:
                etf_push[c] = fetch_push2(m["secid"])
            except Exception:
                etf_push[c] = None

    # 4) 混合基金盘中估算净值(新浪, 支持多只)
    fu_map = {}
    for c, m in FUNDS.items():
        if m.get("type") == "MUTUAL" and m.get("fu"):
            try:
                fu_map[c] = fetch_fu(m["fu"])
            except Exception:
                fu_map[c] = None

    # 5) 逐基金建模
    result = {"generated_at": now.isoformat(timespec="seconds"), "market_status": st,
              "day": day, "first_run_of_day": is_first, "funds": {}, "sources": {
                  "holdings": "天天基金持仓明细(2026-06-30披露)",
                  "nav": "东方财富 api.fund.eastmoney.com/f10/lsjz(官方净值)",
                  "quotes": "新浪行情 hq.sinajs.cn / 东财 push2(实时)",
                  "estimate": "新浪 fu_ 估算净值",
              }}
    for code, meta in FUNDS.items():
        if meta.get("type") == "INDEX":
            # 指数标的: 实时点位/涨跌 + 成分股列表及行情
            fobj = build_index_fund(code, meta, now, st, base["funds"].get(code))
            off = fobj["official"]
            fund_db.add_snapshot({
                "ts": now.isoformat(timespec="seconds"), "fund_code": code, "trade_date": day,
                "model_chg_pct": None, "adjusted_chg_pct": off.get("chg_pct"),
                "official_live_chg_pct": off.get("chg_pct"),
                "live_price": off.get("price"), "baseline_date": None, "baseline_nav": None,
                "market_status": st, "quote_time": off.get("quote_time"),
            })
            result["funds"][code] = fobj
            continue
        d = direct_map.get(code, {"stock_rows": [], "fund_rows": [], "quarter": "", "report_date": ""})
        # 递归穿透: 聚合所有底层股票权重(同股票多路径求和, 含环检测)
        agg = aggregate_stocks(code, direct_map)
        fund_rows = d.get("fund_rows", [])
        penetrated = len(fund_rows) > 0
        # 穿透基金的披露期应取自目标ETF(底层股票来源), 而非联接基金自身陈旧的一行jjcc
        if fund_rows:
            tgt = direct_map.get(fund_rows[0]["target_code"], {})
            pen_quarter = tgt.get("quarter") or fund_rows[0]["quarter"] or d.get("quarter", "")
            pen_report = tgt.get("report_date") or fund_rows[0]["report_date"] or d.get("report_date", "")
        else:
            pen_quarter = d.get("quarter", "")
            pen_report = d.get("report_date", "")
        fobj = {"fund_code": code, "fund_name": meta["name"], "type": meta["type"],
                "self_sina": meta.get("sina") or "",
                "tags": meta.get("tags") or [],
                "holdings_quarter": pen_quarter or d.get("quarter", ""),
                "holdings_report_date": pen_report or d.get("report_date", ""),
                "holdings_penetrated": penetrated,
                "penetrated_via": [fr["target_code"] for fr in fund_rows],
                "holdings_direct": [
                    {"type": "FUND", "code": fr["target_code"], "name": fr["target_name"],
                     "weight": fr["weight_pct"], "ratio_real": fr.get("ratio_real", 0)}
                    for fr in fund_rows
                ] + [
                    {"type": "STOCK", "code": s["code"], "name": s["name"],
                     "weight": s["weight_pct"], "ratio_real": 0}
                    for s in d.get("stock_rows", [])
                ],
                "baseline": base["funds"][code]["baseline"], "baseline_source": base["funds"][code]["source"],
                "trades": TRADES_BY_CODE.get(code, []),
                "trade_summary": TRADE_SUMMARY.get(code, {}),
                "stocks": [], "est": {}, "official": {}}

        # 聚合后的底层股票实时数据(穿透求和后的权重)
        total_w = 0.0
        for sc, w in agg.items():
            sm = stock_meta.get(sc)
            if not sm:
                continue
            q = quotes.get(sm["sina"])
            if not q or q["prev_close"] <= 0:
                # 无行情: 仍计入披露权重, 不参与加权(保持披露覆盖度)
                fobj["stocks"].append({"code": sc, "name": sm["name"], "weight_pct": round(w, 4),
                                        "sina": sm["sina"], "price": None, "prev_close": None,
                                        "chg_pct": None, "quote_time": ""})
                continue
            chg = (q["current"] - q["prev_close"]) / q["prev_close"] * 100.0
            total_w += w
            fobj["stocks"].append({
                "code": sc, "name": sm["name"], "weight_pct": round(w, 4), "sina": sm["sina"],
                "price": q["current"], "prev_close": q["prev_close"], "chg_pct": round(chg, 2),
                "quote_time": f"{q['date']} {q['time']}",
            })
        fobj["est"]["disclosed_coverage_pct"] = round(total_w, 2)

        # 加权估算(按披露持仓, 仅含有效行情)
        if total_w > 0:
            est = sum(s["weight_pct"] * s["chg_pct"] for s in fobj["stocks"] if s["chg_pct"] is not None) / total_w
        else:
            est = None
        fobj["est"]["model_change_pct"] = round(est, 2) if est is not None else None

        # 余量: 用行业指数近似
        idx_sym = meta.get("index_sina") or meta.get("fallback_index")
        idx_q = None
        for cand in meta.get("index_sina_candidates", []):
            if cand in quotes and quotes[cand].get("name"):
                idx_q = quotes[cand]; idx_sym = cand; break
        if not idx_q:
            idx_q = quotes.get(idx_sym)
        if idx_q and idx_q["prev_close"] > 0 and idx_q.get("name"):
            idx_chg = (idx_q["current"] - idx_q["prev_close"]) / idx_q["prev_close"] * 100.0
            residual_w = max(0.0, 100.0 - total_w)
            fobj["est"]["index"] = {"name": idx_q["name"], "chg_pct": round(idx_chg, 2)}
            fobj["est"]["residual_weight_pct"] = round(residual_w, 2)
            if est is not None:
                adj = (total_w * est + residual_w * idx_chg) / 100.0
                fobj["est"]["adjusted_model_change_pct"] = round(adj, 2)
            else:
                fobj["est"]["adjusted_model_change_pct"] = round(idx_chg, 2)
        else:
            fobj["est"]["adjusted_model_change_pct"] = fobj["est"]["model_change_pct"]

        # 基金本身实时表现: 能实时则实时, 不能实时则兜底显示最近官方净值(历史)
        nav = base["funds"][code].get("nav_history") or []
        realtime = False
        if meta["type"] == "ETF":
            sin = quotes.get(meta["sina"])
            if sin and sin["prev_close"] > 0:
                etf_chg = (sin["current"] - sin["prev_close"]) / sin["prev_close"] * 100.0
                fobj["official"] = {"type": "场内实时价", "price": sin["current"],
                                    "prev_close": sin["prev_close"], "chg_pct": round(etf_chg, 2),
                                    "quote_time": f"{sin['date']} {sin['time']}",
                                    "source": f"新浪行情 {meta['sina']}"}
                realtime = True
            pu = etf_push.get(code)
            if pu:
                fobj["official"]["push2"] = {"price": pu["current"], "prev_close": pu["prev_close"],
                                             "chg_pct": pu["chg_pct"], "source": "东财 push2"}
        else:
            f = fu_map.get(code)
            if f:
                fobj["official"] = {"type": "新浪估算净值", "gsz": f["gsz"], "dwjz": f["dwjz"],
                                    "chg_pct": round((f["gsz"] - f["dwjz"]) / f["dwjz"] * 100.0, 2),
                                    "estimate_time": f"{f['date']} {f['time']}",
                                    "source": f"新浪 fu_{code}"}
                realtime = True
            # 联接基金/指数型基金: 配置了估算锚(跟踪指数或目标ETF)则以锚实时涨跌为准
            anchor_q = quotes.get(meta.get("anchor_tencent")) if meta.get("anchor_tencent") else None
            if anchor_q and anchor_q["prev_close"] > 0 and anchor_q.get("name"):
                achg = round((anchor_q["current"] - anchor_q["prev_close"]) / anchor_q["prev_close"] * 100.0, 2)
                fobj["official"]["anchor"] = {"name": meta["anchor_name"] or anchor_q["name"],
                                              "price": anchor_q["current"], "prev_close": anchor_q["prev_close"],
                                              "chg_pct": achg, "quote_time": f"{anchor_q['date']} {anchor_q['time']}",
                                              "source": f"腾讯行情 {meta['anchor_tencent']}"}
                realtime = True
                # 披露股票覆盖极低(联接基金特征)时, 以跟踪锚为准
                if total_w < 20:
                    fobj["est"]["anchor_based"] = True
        # 历史兜底: 无任何实时通道 -> 展示最近官方净值
        fobj["realtime"] = realtime
        if not realtime:
            if nav:
                latest = nav[0]
                fobj["official"] = {"type": "最近官方净值(非实时)", "chg_pct": latest.get("chg_pct"),
                                    "nav": latest.get("dwjz"), "nav_date": latest.get("date"),
                                    "source": "东方财富 lsjz 官方净值"}
        else:
            if nav:
                fobj["official"]["nav_history"] = nav[:2]
                fobj["official"]["last_official"] = {"date": nav[0]["date"], "nav": nav[0]["dwjz"],
                                                     "chg_pct": nav[0]["chg_pct"]}

        # 个人持仓金额指标(买入金额/份额/买入净值来自 config/funds.json)
        fobj["position"] = compute_position(code, meta, fobj, base["funds"][code].get("nav_history") or [])

        # 每次运行写入历史库快照(全量保留, 留作判断数据)
        baseline = fobj["baseline"] or {}
        off = fobj["official"]
        fund_db.add_snapshot({
            "ts": now.isoformat(timespec="seconds"), "fund_code": code, "trade_date": day,
            "model_chg_pct": fobj["est"].get("model_change_pct"),
            "adjusted_chg_pct": fobj["est"].get("adjusted_model_change_pct"),
            "official_live_chg_pct": off.get("chg_pct"),
            "live_price": off.get("price") or off.get("gsz"),
            "baseline_date": baseline.get("date"), "baseline_nav": baseline.get("nav"),
            "market_status": st, "quote_time": off.get("quote_time") or off.get("estimate_time"),
        })
        result["funds"][code] = fobj

    # 延迟补生成修正记录(某交易日官方净值已发布 -> 生成"模型预估 vs 官方实际")
    # 仅对开启"预估修正"开关的基金生成(默认关闭); 生成后清理冗余快照并 VACUUM
    fc_cfg = fund_db.kv_get("funds.json") or {}
    funds_cfg = fc_cfg.get("funds", {}) if isinstance(fc_cfg, dict) else {}
    enabled_codes = {c for c, f in funds_cfg.items() if (f or {}).get("est_correction")}
    made_corr, purged_snaps = fund_db.generate_corrections(enabled_codes=enabled_codes)
    result["corrections_generated"] = made_corr
    result["snapshots_purged"] = purged_snaps
    result["db_stats"] = fund_db.stats()

    # 统一入库(app_kv): 不再写磁盘 JSON(数据真相源为数据库)
    fund_db.kv_set("latest.json", result)
    return result

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refreshpositions":
        ok = refresh_positions_only()
        print("OK refresh_positions_only" if ok is not False else "FAILED refresh_positions_only")
    else:
        res = run()
        # 控制台摘要
        print("-" * 100)
        for code, f in res["funds"].items():
            b = f["baseline"]
            print(f"\n【{f['fund_name']}】 {f['type']}  持仓披露: {f['holdings_quarter']} ({f['holdings_report_date']})")
            if b:
                print(f"  基线(前一日收盘准确数据): {b['desc']}   [{f['baseline_source']}]")
            est = f["est"]
            print(f"  披露持仓覆盖: {est.get('disclosed_coverage_pct')}% | 加权模型涨跌幅: {est.get('model_change_pct')}%"
                  + (f" | 行业指数[{est['index']['name']}] {est['index']['chg_pct']}% 修正后: {est.get('adjusted_model_change_pct')}%" if est.get('index') else ""))
            off = f["official"]
            if off.get("type"):
                print(f"  基金本身实时: [{off['type']}] {off.get('chg_pct')}% @ {off.get('quote_time') or off.get('estimate_time')}  [{off.get('source')}]")
                if off.get("push2"):
                    print(f"    东财push2交叉: 现价{off['push2']['price']} 昨收{off['push2']['prev_close']} 涨跌{off['push2']['chg_pct']}%")
            if off.get("anchor"):
                a = off["anchor"]
                print(f"  跟踪锚[{a['name']}]: {a['chg_pct']}% @ {a['quote_time']} (披露股票覆盖低, 以锚为准)" if f["est"].get("anchor_based")
                      else f"  跟踪锚[{a['name']}](参考): {a['chg_pct']}% @ {a['quote_time']}")
            pos = f.get("position") or {}
            if pos.get("configured"):
                print(f"  个人持仓: 买入 {pos.get('buy_amount')} | 当前(昨收) {pos.get('current_amount', '—')} | "
                      f"昨日收益 {pos.get('yesterday_gain', '—')} | 持有收益 {pos.get('hold_gain', '—')} "
                      f"({pos.get('hold_gain_pct', '—')}%) | 累计 {pos.get('total_gain', '—')} | "
                      f"当日预估变化 {pos.get('today_est_change', '—')}")
            else:
                print(f"  个人持仓: 未配置金额 (用: python manage_funds.py set {code} --amount X [--shares Y])")
            print(f"  前十大/主要持仓实时表现:")
            for s in f["stocks"][:12]:
                if s["chg_pct"] is None:
                    print(f"    {s['name']}({s['code']}) 权重{s['weight_pct']}%  现价—  无实时行情")
                else:
                    arrow = "+" if s["chg_pct"] >= 0 else ""
                    print(f"    {s['name']}({s['code']}) 权重{s['weight_pct']}%  现价{s['price']}  {arrow}{s['chg_pct']}%")
        print("\n数据来源: 天天基金持仓明细(2026-06-30) / 东财官方净值LSJZ / 新浪实时行情 / 新浪盘中估算净值")
        print("快照已写入: data/latest.json 及 data/snapshots/")
        ds = res.get("db_stats", {})
        print(f"历史库: {ds.get('db')}  净值记录 {ds.get('nav_history')} 条 / 快照 {ds.get('snapshots')} 条 / 修正记录 {ds.get('corrections')} 条 (本次新增修正 {res.get('corrections_generated', 0)} 条)")
        print("提示: 修正记录在官方净值发布后自动补生成; 导出用 export_data.py, 导入用 import_data.py")
