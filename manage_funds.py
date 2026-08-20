# -*- coding: utf-8 -*-
"""
基金持仓金额/基金管理工具
==========================
配置存储: config/funds.json
功能:
  list                    列出全部基金与持仓金额配置
  add <代码> [--name 名称] [--type ETF|MUTUAL] [--amount 买入金额] [--shares 份额] [--buy-nav 买入净值]
  set <代码> [--amount X] [--shares Y] [--buy-nav Z] [--name N] [--type T]
  remove <代码>
示例:
  python manage_funds.py add 005827 --amount 8000 --shares 5000
  python manage_funds.py set 516670 --amount 10000 --shares 16000
  python manage_funds.py list
"""
import argparse, json, os, sys, datetime, re, time, ssl
import urllib.request, urllib.parse
import fund_db

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config", "funds.json")   # 兼容读取源(迁移用), 数据真相源为数据库
TRADES = os.path.join(BASE, "config", "trades.json")

BUILTIN = {
    "516670": {"name": "招商中证畜牧养殖ETF", "type": "ETF"},
    "003095": {"name": "中欧医疗健康混合A", "type": "MUTUAL"},
}

# 已知联接基金 -> 估算锚(跟踪指数或目标ETF的腾讯行情代码, 名称)
ANCHOR_MAP = {
    "022982": ("sz159362", "工银中证A500ETF"),
    "022951": ("sh512890", "红利低波ETF(512890)"),
    "014414": ("sh516670", "招商中证畜牧养殖ETF"),
}

def load():
    """基金配置: 优先数据库(app_kv), 数据库无数据时回退磁盘 JSON 并自动迁移"""
    v = fund_db.kv_get("funds.json")
    if v is not None:
        return v if isinstance(v, dict) and "funds" in v else {"funds": v}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            fund_db.kv_set("funds.json", cfg)
            return cfg
        except Exception:
            pass
    return {"funds": {}}

def save(cfg):
    """基金配置仅存数据库(app_kv), 不再写磁盘 JSON"""
    fund_db.kv_set("funds.json", cfg)

# ---------------------------------------------------------------- 交易记录(买入/卖出)
def load_trades():
    v = fund_db.kv_get("trades.json")
    if v is not None:
        return v if isinstance(v, dict) and "trades" in v else {"trades": []}
    if os.path.exists(TRADES):
        try:
            with open(TRADES, encoding="utf-8") as f:
                t = json.load(f)
            fund_db.kv_set("trades.json", t)
            return t
        except Exception:
            pass
    return {"trades": []}

def save_trades(t):
    """交易记录仅存数据库(app_kv), 不再写磁盘 JSON"""
    fund_db.kv_set("trades.json", t)

def do_trade_add(args):
    """添加一笔交易记录。
    买入: 金额/份额/净值/时间  (缺金额=份额×净值; 缺份额=金额÷净值)
    卖出: 份额/净值/时间/金额  (缺金额=份额×净值; 缺份额=金额÷净值)
    分红: 仅 --amount 金额(无份额/净值, 计入已实现收益)"""
    cfg = load()
    code = args.code.strip()
    if code not in cfg["funds"]:
        print(f"基金 {code} 不在跟踪列表, 请先: python manage_funds.py add {code}")
        sys.exit(1)
    ttype = (args.type or "buy").strip().lower()
    if ttype not in ("buy", "sell", "dividend"):
        print("--type 须为 buy(买入) / sell(卖出) / dividend(分红)")
        sys.exit(1)
    nav = args.nav
    shares = args.shares
    amount = args.amount
    if ttype == "dividend":
        # 分红: 只有金额, 无份额/净值
        if amount is None:
            print("分红必须提供 --amount 金额(无份额/净值)")
            sys.exit(1)
        nav = None; shares = None
    else:
        if nav is None and (shares is None or amount is None):
            print("买入/卖出必须提供 --nav 净值, 且 --shares/--amount 至少其一")
            sys.exit(1)
        if shares is None and nav and amount is not None:
            shares = round(amount / nav, 4)
        if amount is None and nav is not None and shares is not None:
            amount = round(shares * nav, 2)
        if amount is None or shares is None or nav is None:
            print("无法推导金额/份额/净值, 请检查输入")
            sys.exit(1)
    date = args.date or datetime.date.today().isoformat()
    fee = args.fee
    if fee is None:
        if ttype == "dividend":
            fee = 0.0  # 分红无手续费
        else:
            # 未指定手续费 -> 按基金配置费率自动计算(买入=金额×申购费率, 卖出=金额×赎回费率)
            fr = cfg["funds"][code].get("buy_fee_rate" if ttype == "buy" else "sell_fee_rate") or 0
            fee = round(amount * fr, 2)
    tr = {"id": f"t{int(datetime.datetime.now().timestamp() * 1000)}",
          "code": code, "type": ttype,
          "amount": round(amount, 2),
          "shares": round(shares, 4) if shares is not None else 0,
          "nav": round(nav, 4) if nav is not None else 0,
          "date": date, "fee": round(fee, 2),
          "clear": True if getattr(args, "clear", False) else False}
    t = load_trades()
    # 幂等保护: 同一 code+type+amount+date+nav+shares 已存在则跳过, 防止重复提交导致双写
    for ex in t["trades"]:
        if (ex.get("code") == code and ex.get("type") == ttype and
                round(float(ex.get("amount", 0) or 0), 2) == round(float(amount), 2) and
                ex.get("date") == date and
                round(float(ex.get("nav", 0) or 0), 4) == round(float(nav if nav is not None else 0), 4) and
                round(float(ex.get("shares", 0) or 0), 4) == round(float(shares if shares is not None else 0), 4)):
            print(f"已存在相同交易(跳过, 未重复写入): {code} {date} {ttype} 金额{amount}")
            return
    t["trades"].append(tr)
    save_trades(t)
    if ttype == "dividend":
        print(f"已添加分红记录: {code} {date} 金额{tr['amount']}")
    else:
        tname = "买入" if ttype == "buy" else ("清仓" if tr.get("clear") else "卖出")
        print(f"已添加{tname}记录: {code} {date} 净值{tr['nav']} 份额{tr['shares']} 金额{tr['amount']} 手续费{tr['fee']}")

def do_trade_list(args):
    t = load_trades()
    lst = [x for x in t["trades"] if not args.code or x["code"] == args.code]
    lst.sort(key=lambda x: x["date"])
    if not lst:
        print("暂无交易记录" + (f"({args.code})" if args.code else ""))
        return
    print(f"{'日期':<12}{'基金':<8}{'类型':<6}{'净值':<10}{'份额':<14}{'金额':<12}{'ID':<18}")
    for x in lst:
        tname = "买入" if x["type"] == "buy" else ("卖出" if x["type"] == "sell" else "分红")
        print(f"{x['date']:<12}{x['code']:<8}{tname:<6}{x['nav']:<10}{x['shares']:<14}{x['amount']:<12}{x['id']:<18}")

def do_trade_del(args):
    t = load_trades()
    before = len(t["trades"])
    t["trades"] = [x for x in t["trades"] if x["id"] != args.id]
    if len(t["trades"]) == before:
        # 幂等: 记录已不存在(可能已删除/重复请求), 视为成功, 避免前端误报"删除失败"且不刷新
        print(f"未找到交易记录 {args.id}（可能已删除, 无需重复删除）")
        return
    save_trades(t)
    print(f"已删除交易记录 {args.id}")

def guess_type(code):
    return "ETF" if code.startswith(("51", "56", "58", "15", "16")) else "MUTUAL"

def parse_tags(s):
    """'A,B,C' -> ['A','B','C']"""
    if not s:
        return []
    return [t.strip() for t in str(s).replace("，", ",").split(",") if t.strip()]

def auto_tags(name, ftype, anchor, cfg_tags=None):
    """自动分类(用户显式标签优先, 自动补常见类型, 去重)。规则:
       类型(ETF/场外) / 养老金Y份额(Y结尾) / 联接基金(有锚) / 指数基金 /
       QDII / 债券 / 货币 / 指数增强 / LOF / 定期开放"""
    tags = list(cfg_tags or [])
    nm = name or ""
    if ftype == "ETF":
        if "ETF" not in tags: tags.append("ETF")
    elif "场外基金" not in tags:
        tags.append("场外基金")
    if nm.rstrip().endswith("Y"):
        if "养老金Y份额" not in tags: tags.append("养老金Y份额")
    if anchor:
        if "联接基金" not in tags: tags.append("联接基金")
    if "指数增强" in nm or "增强" in nm:
        if "指数增强" not in tags: tags.append("指数增强")
    for kw in ("中证", "指数", "A500", "红利"):
        if kw in nm:
            if "指数基金" not in tags: tags.append("指数基金")
            break
    for kw, tg in (("QDII", "QDII"), ("全球", "QDII"), ("海外", "QDII"), ("标普", "QDII"),
                   ("纳斯达克", "QDII"), ("纳指", "QDII"), ("恒生", "QDII"), ("德国", "QDII"),
                   ("日经", "QDII"), ("美元", "QDII"), ("黄金", "QDII"), ("原油", "QDII"), ("商品", "QDII"),
                   ("债券", "债券基金"), ("纯债", "债券基金"), ("短债", "债券基金"),
                   ("信用债", "债券基金"), ("利率债", "债券基金"),
                   ("货币", "货币基金"), ("现金", "货币基金")):
        if kw in nm:
            if tg not in tags: tags.append(tg)
    if "LOF" in nm:
        if "LOF" not in tags: tags.append("LOF")
    if "定期开放" in nm or "封闭" in nm:
        if "定期开放" not in tags: tags.append("定期开放")
    return tags

# ---------------------------------------------------------------- 元数据自动获取(东财搜索接口)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _fund_search(key):
    """东财基金搜索: 返回 [{code,name,company,manager,ftype,themes,nav,nav_date}], 失败返回 []"""
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        u = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key="
             + urllib.parse.quote(str(key).strip()))
        req = urllib.request.Request(u, headers={"User-Agent": _UA, "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            txt = r.read().decode("utf-8", "ignore")
        j = json.loads(txt)
        out = []
        for it in (j.get("Datas") or []):
            fb = it.get("FundBaseInfo") or {}
            if not fb.get("FCODE"):
                continue
            out.append({
                "code": fb["FCODE"],
                "name": fb.get("SHORTNAME") or fb["FCODE"],
                "company": fb.get("JJGS"), "manager": fb.get("JJJL"),
                "ftype": fb.get("FTYPE"),
                "themes": [z.get("TTYPENAME") for z in (it.get("ZTJJInfo") or []) if z.get("TTYPENAME")],
                "nav": fb.get("DWJZ"), "nav_date": fb.get("FSRQ"),
            })
        return out
    except Exception:
        return []

def _fetch_buy_fee_pct(code):
    """pingzhongdata 拉打折后申购费率%(fund_Rate=支付宝/天天基金1折价, 014414=0.12)。失败返回 None"""
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        u = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        req = urllib.request.Request(u, headers={"User-Agent": _UA, "Referer": f"https://fund.eastmoney.com/{code}.html"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            t = r.read().decode("utf-8", "ignore")
        m = re.search(r"var\s+fund_Rate\s*=\s*\"?([\d.]+)\"?\s*;", t)
        return float(m.group(1)) if m else None
    except Exception:
        return None

def fetch_fund_meta(code):
    """自动拉取基金真实元数据: 名称/公司/经理/类型(FTYPE)/主题标签(ZTJJInfo)/最新净值/申购费率。失败返回 None"""
    for it in _fund_search(code):
        if it["code"] == str(code).strip():
            it["buy_fee_pct"] = _fetch_buy_fee_pct(it["code"])
            it["fetched_ts"] = time.time()
            return it
    return None

def is_index_code(code):
    """识别指数代码: 399 开头 6 位 = 深交所国证/深证指数(如 399365 国证粮食)。返回 True/False"""
    s = str(code).strip()
    return s.isdigit() and len(s) == 6 and s.startswith("399")

def fetch_index_meta(code):
    """新浪指数行情元数据: 名称/最新点位/昨收/涨跌幅。
    指数格式: 名称,今开,昨收,最新,最高,最低,...(与股票同位置布局)。失败返回 None"""
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    s = str(code).strip()
    sym = ("sz" if s.startswith("399") else "sh") + s
    try:
        u = f"https://hq.sinajs.cn/list={sym}"
        req = urllib.request.Request(u, headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            raw = r.read()
        txt = raw.decode("gbk", "ignore")
        m = re.search(r'"(.*)"', txt)
        if not m or not m.group(1):
            return None
        p = m.group(1).split(",")
        if not p or not p[0]:
            return None
        def _f(x):
            try:
                return float(x)
            except (ValueError, TypeError):
                return None
        cur, pc = _f(p[3]) if len(p) > 3 else None, _f(p[2]) if len(p) > 2 else None
        chg = round((cur - pc) / pc * 100.0, 2) if (cur is not None and pc) else None
        return {"code": s, "name": p[0], "ftype": "指数",
                "company": None, "manager": None, "themes": ["指数"],
                "nav": cur, "nav_date": None, "fetched_ts": time.time(),
                "index_price": cur, "index_prev_close": pc, "index_chg_pct": chg}
    except Exception:
        return None

def _etf_prefix_ok(code):
    return str(code).startswith(("51", "56", "58", "15", "16"))

def auto_detect_anchor(name, company=""):
    """联接基金 -> 底层场内ETF代码(普适化自动识别):
       名称解析(去'联接'+后缀) + 多关键词递进搜索(去公司前缀/去指数前缀) + 公司名匹配。
       返回 (etf_code, etf_name) 或 None(交给 ANCHOR_MAP / 名称匹配 / 手动填)。"""
    m = re.search(r"(.+?ETF)\s*联接", name or "")
    if not m:
        return None
    base = m.group(1).strip()
    comp = (company or "").replace("基金", "").strip()
    keys = [base]
    if comp and base.startswith(comp):
        keys.append(base[len(comp):])
    for k in list(keys):
        k2 = re.sub(r"^(中证|上证|深证|国证|中债|中盘|小盘)", "", k)
        if k2 and k2 not in keys:
            keys.append(k2)
    for k in keys:
        cands = [it for it in _fund_search(k) if _etf_prefix_ok(it["code"])]
        if comp:
            cc = [it for it in cands if comp in (it["name"] or "")]
            if cc:
                cands = cc
        if len(cands) == 1:
            return cands[0]["code"], cands[0]["name"]
    return None

def merge_tags(existing, ftype, themes):
    """合并标签: 现有(用户手改优先) + 自动(FTYPE映射 + 主题标签), 去重保序"""
    tags = list(existing or [])
    if ftype:
        tmap = {"指数型-股票": "指数基金", "指数型-债券": "指数基金", "指数型-商品": "指数基金",
                "股票型": "股票基金", "混合型": "混合基金", "债券型": "债券基金",
                "货币型": "货币基金", "QDII": "QDII", "FOF": "FOF", "REITs": "REITs"}
        for k, v in tmap.items():
            if k in (ftype or "") and v not in tags:
                tags.append(v)
    for th in (themes or []):
        if th and th not in tags:
            tags.append(th)
    return tags

def do_list():
    cfg = load()
    funds = cfg["funds"]
    if not funds:
        print("暂无基金配置。可用: python manage_funds.py add <代码> --amount X")
        return
    print(f"{'代码':<8}{'名称':<22}{'类型':<6}{'状态':<8}{'分类':<28}{'买入金额':<12}")
    for code, f in funds.items():
        st = "已隐藏" if f.get("hidden") else "跟踪中"
        tg = "/".join(auto_tags(f.get("name",""), f.get("type",""), f.get("anchor_tencent"), f.get("tags")))
        print(f"{code:<8}{f.get('name','')[:20]:<22}{f.get('type',''):<6}{st:<8}{tg[:27]:<28}{f.get('buy_amount') or 0:<12}")

def do_add(args):
    cfg = load()
    code = args.code.strip()
    if not code.isdigit() or len(code) != 6:
        print("基金代码须为 6 位数字")
        sys.exit(1)
    if code in cfg["funds"]:
        if cfg["funds"][code].get("hidden"):
            # 伪删除的基金重新添加: 恢复可见, 保留历史数据, 引擎会自动补数据到最新
            f = cfg["funds"][code]
            f["hidden"] = False
            if args.name: f["name"] = args.name
            if args.amount: f["buy_amount"] = args.amount
            if args.shares: f["shares"] = args.shares
            if args.buy_nav: f["buy_nav"] = args.buy_nav
            if args.tags is not None: f["tags"] = parse_tags(args.tags)
            save(cfg)
            print(f"已恢复可见 {code} {f.get('name','')} (历史数据保留在库, 重新跟踪, 数据自动补到最新)")
        else:
            print(f"基金 {code} 已存在, 用 set 修改; 或先 hide/remove")
            sys.exit(1)
        return
    name = args.name or f"基金{code}"
    ftype = args.type or BUILTIN.get(code, {}).get("type") or guess_type(code)
    is_idx = is_index_code(code)
    if is_idx and not args.type:
        ftype = "INDEX"
    entry = {"name": name, "type": ftype,
             "buy_amount": args.amount or 0, "shares": args.shares, "buy_nav": args.buy_nav}
    # 自动拉取真实元数据(名称/公司/类型/主题标签/最新净值), 失败则回退规则
    # 指数代码走指数行情元数据(名称/点位), 基金走基金搜索接口
    meta = fetch_index_meta(code) if is_idx else fetch_fund_meta(code)
    if meta:
        if not args.name:
            entry["name"] = name = meta.get("name") or name
        entry["meta"] = {k: meta.get(k) for k in
                         ("company", "manager", "ftype", "themes", "nav", "nav_date", "fetched_ts")}
        if not is_idx and meta.get("buy_fee_pct") is not None:
            entry["buy_fee_rate"] = round(meta["buy_fee_pct"] / 100.0, 4)  # 申购费率(小数), 买入自动算手续费
    themes = (meta or {}).get("themes") or []
    mftype = (meta or {}).get("ftype") or ""
    # 联接基金自动映射估算锚(穿透底层ETF): 显式 --anchor > ANCHOR_MAP > 名称自动识别 > 已添加ETF名称匹配
    # 指数自身即标的, 无需估算锚
    if not is_idx:
        if args.anchor:
            a = args.anchor.strip()
            if re.match(r"^\d{6}$", a):  # 纯6位数字代码 -> 补市场前缀(sh/sz), 保证新浪行情symbol有效
                a = ("sh" if a.startswith(("5", "6", "9")) else "sz") + a
            entry["anchor_tencent"], entry["anchor_name"] = a, args.anchor_name or f"跟踪锚{a}"
        elif code in ANCHOR_MAP:
            entry["anchor_tencent"], entry["anchor_name"] = ANCHOR_MAP[code]
        else:
            anchor = auto_detect_anchor(name, (meta or {}).get("company"))
            if anchor:
                entry["anchor_tencent"] = ("sh" if anchor[0].startswith(("5", "6", "9")) else "sz") + anchor[0]
                entry["anchor_name"] = anchor[1]
            else:
                # 兜底: 联接基金名称含某已添加ETF名 -> 自动锚定(无需手动--anchor)
                for c, ex in cfg["funds"].items():
                    en = ex.get("name", "")
                    if en and en in name and ex.get("type") == "ETF":
                        entry["anchor_tencent"] = ("sh" if c.startswith(("5", "6", "9")) else "sz") + c
                        entry["anchor_name"] = en
                        break
    entry["tags"] = merge_tags(auto_tags(name, ftype, entry.get("anchor_tencent"), parse_tags(args.tags)),
                               mftype, themes)
    cfg["funds"][code] = entry
    save(cfg)
    if meta:
        extra = (f"  [指数 {meta.get('name') or code}]" if is_idx
                 else f"  [{meta.get('company') or '?'}/{meta.get('manager') or '?'}/{mftype or '?'}]")
    else:
        extra = "  (元数据获取失败)"
    print(f"已添加 {code} {name} ({ftype})  买入金额 {entry['buy_amount']}"
          + (f"  估算锚: {entry['anchor_name']}({entry['anchor_tencent']})" if entry.get("anchor_tencent") else "")
          + f"  分类: {'/'.join(entry['tags'])}" + extra)

def do_set(args):
    cfg = load()
    code = args.code.strip()
    if code not in cfg["funds"]:
        # 允许直接 set 新基金
        cfg["funds"][code] = {"name": BUILTIN.get(code, {}).get("name") or f"基金{code}",
                              "type": BUILTIN.get(code, {}).get("type") or guess_type(code),
                              "buy_amount": 0, "shares": None, "buy_nav": None}
    f = cfg["funds"][code]
    if args.amount is not None: f["buy_amount"] = args.amount
    if args.shares is not None: f["shares"] = args.shares
    if args.buy_nav is not None: f["buy_nav"] = args.buy_nav
    if args.name: f["name"] = args.name
    if args.type: f["type"] = args.type
    if args.tags is not None: f["tags"] = parse_tags(args.tags)
    if args.tags is None and not f.get("tags"):
        f["tags"] = auto_tags(f.get("name", ""), f.get("type", ""), f.get("anchor_tencent"), None)
    if args.buy_fee_rate is not None: f["buy_fee_rate"] = args.buy_fee_rate
    if args.sell_fee_rate is not None: f["sell_fee_rate"] = args.sell_fee_rate
    if args.anchor is not None:
        f["anchor_tencent"] = args.anchor or None
        if args.anchor and args.anchor_name:
            f["anchor_name"] = args.anchor_name
        elif args.anchor is None:
            f.pop("anchor_name", None)
    if args.anchor and not args.anchor_name and not f.get("anchor_name"):
        f["anchor_name"] = f"跟踪锚{args.anchor}"
    save(cfg)
    print(f"已更新 {code}: {json.dumps(f, ensure_ascii=False)}")

def do_remove(args):
    """真删除: 仅从配置移除(数据库历史数据仍保留, 重新添加后可见并自动补数据)"""
    cfg = load()
    code = args.code.strip()
    if code in cfg["funds"]:
        del cfg["funds"][code]
        save(cfg)
        print(f"已从列表移除 {code} (数据库历史数据保留; 重新 add 即恢复)")
    else:
        print(f"{code} 不存在")

def do_hide(args):
    """伪删除: 面板不可见, 数据保留在库, 重新添加即恢复"""
    cfg = load()
    code = args.code.strip()
    if code not in cfg["funds"]:
        print(f"{code} 不存在")
        sys.exit(1)
    cfg["funds"][code]["hidden"] = True
    save(cfg)
    print(f"已伪删除(隐藏) {code} — 面板不再显示; 数据保留在库; 重新 add {code} 即恢复并自动补数据到最新")

def do_unhide(args):
    cfg = load()
    code = args.code.strip()
    if code not in cfg["funds"]:
        print(f"{code} 不存在")
        sys.exit(1)
    cfg["funds"][code].pop("hidden", None)
    save(cfg)
    print(f"已恢复显示 {code}")

def do_purge(args):
    """彻底删除(从库里抹除): 基础信息 + 买卖记录 + 历史净值, 后期重新 add 走全新加载"""
    code = args.code.strip()
    n = fund_db.purge_fund(code)
    print(f"已彻底删除 {code} 的全部数据(基础信息/买卖记录/历史净值{n and ', 库表记录 ' + str(n) + ' 条' or ''}); 重新 add {code} 将重新抓取")

def do_est_correction(args):
    """开启/关闭某基金的'预估修正'开关(默认关闭)。off 时清理该基金已有修正记录与快照。"""
    code = args.code.strip()
    enabled = args.state == "on"
    cfg = load()
    if code not in cfg["funds"]:
        print(f"{code} 不存在于基金列表")
        sys.exit(1)
    cfg["funds"][code]["est_correction"] = enabled
    save(cfg)
    if not enabled:
        nc = fund_db.delete_corrections_for_fund(code)
        ns = fund_db.delete_snapshots_for_fund(code)
        fund_db.vacuum_db()
        print(f"已关闭 {code} 的预估修正; 清理修正记录 {nc} 条 / 快照 {ns} 条, 已回收数据库空间")
    else:
        print(f"已开启 {code} 的预估修正(下次同步将生成'模型预估 vs 官方实际'修正记录)")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="管理基金与持仓金额配置")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    pa = sub.add_parser("add"); pa.add_argument("code"); pa.add_argument("--name"); pa.add_argument("--type", choices=["ETF", "MUTUAL"])
    pa.add_argument("--amount", type=float); pa.add_argument("--shares", type=float); pa.add_argument("--buy-nav", type=float)
    pa.add_argument("--anchor", help="估算锚腾讯代码, 如 sh000510(指数)/sh512890(ETF)"); pa.add_argument("--anchor-name")
    pa.add_argument("--tags", help="分类标签, 逗号分隔, 如: 养老金专属,指数基金")
    ps = sub.add_parser("set"); ps.add_argument("code"); ps.add_argument("--name"); ps.add_argument("--type", choices=["ETF", "MUTUAL"])
    ps.add_argument("--amount", type=float); ps.add_argument("--shares", type=float); ps.add_argument("--buy-nav", type=float)
    ps.add_argument("--anchor", help="估算锚腾讯代码(传空串移除)"); ps.add_argument("--anchor-name")
    ps.add_argument("--tags", help="分类标签, 逗号分隔(传空串清空)")
    ps.add_argument("--buy-fee-rate", type=float, help="申购费率(小数, 如0.0012=0.12%)")
    ps.add_argument("--sell-fee-rate", type=float, help="赎回费率(小数, 默认0)")
    pr = sub.add_parser("remove"); pr.add_argument("code")
    ph = sub.add_parser("hide"); ph.add_argument("code")
    pu = sub.add_parser("unhide"); pu.add_argument("code")
    pp = sub.add_parser("purge"); pp.add_argument("code")
    pt = sub.add_parser("trade"); pt.add_argument("code")
    pt.add_argument("--type", choices=["buy", "sell", "dividend"], default="buy", help="buy=买入 sell=卖出 dividend=分红(仅金额)")
    pt.add_argument("--amount", type=float, help="金额(元); 分红必填, 买卖缺省=份额×净值")
    pt.add_argument("--shares", type=float, help="份额, 缺省=金额÷净值")
    pt.add_argument("--nav", type=float, help="成交净值(买卖必填, 分红无需)")
    pt.add_argument("--date", help="成交日期 YYYY-MM-DD, 缺省今天")
    pt.add_argument("--fee", type=float, help="手续费(元), 缺省按基金配置费率自动计算")
    pt.add_argument("--clear", action="store_true", help="清仓: 标记该笔卖出为清空全部剩余份额(用于消除四舍五入碎片残余)")
    ptl = sub.add_parser("trades"); ptl.add_argument("code", nargs="?")
    ptd = sub.add_parser("tradedel"); ptd.add_argument("id")
    pe = sub.add_parser("est-correction", help="开启/关闭某基金的'预估修正'记录(默认关闭)")
    pe.add_argument("code")
    pe.add_argument("state", choices=["on", "off"], help="on=开启预估修正, off=关闭(关闭后不生成且不显示修正, 并清理该基金已有修正与快照)")
    args = ap.parse_args()
    if not args.cmd:
        do_list()
    elif args.cmd == "list": do_list()
    elif args.cmd == "add": do_add(args)
    elif args.cmd == "set": do_set(args)
    elif args.cmd == "remove": do_remove(args)
    elif args.cmd == "hide": do_hide(args)
    elif args.cmd == "unhide": do_unhide(args)
    elif args.cmd == "purge": do_purge(args)
    elif args.cmd == "trade": do_trade_add(args)
    elif args.cmd == "trades": do_trade_list(args)
    elif args.cmd == "tradedel": do_trade_del(args)
    elif args.cmd == "est-correction": do_est_correction(args)
