# -*- coding: utf-8 -*-
"""
基金跟踪看板本地服务 (可视化管理入口)
=====================================
在浏览器中打开 http://127.0.0.1:8123 即可:
  - 页面 = 实时看板(自动刷新行情)
  - 页面上的"持仓管理"表单可直接修改每只基金的 买入金额/份额/买入净值,
    并可直接添加任意基金(输入代码+金额) —— 保存后自动重跑引擎刷新数据

接口:
  GET  /                    看板页面
  GET  /api/funds           基金配置列表
  POST /api/funds/save      保存持仓 {code, buy_amount, shares, buy_nav}
  POST /api/funds/add       添加基金 {code, name?, buy_amount?, shares?, buy_nav?}
  POST /api/refresh         手动触发引擎+看板刷新
  GET  /api/settings        读取自动刷新设置 {auto_refresh, refresh_seconds}
  POST /api/settings        保存自动刷新设置
  GET  /api/export          导出数据备份(基金配置+交易记录+设置) 为 JSON bundle
  POST /api/import          导入数据备份(覆盖写入并重建看板, 用于空库初始化/迁移)

启动: python live_server.py [端口, 默认8123]
"""
import json, os, subprocess, sys, threading, time, urllib.parse, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fund_db
import fund_tracker  # 板块行情看板数据层(东财缓存/解析/资金流反解)
import pigcycle_db    # 猪周期独立模块数据层(物理隔离的独立数据库)
import pigcycle_fetch  # 猪周期真实数据源适配器框架(东财宏观/CSV导入/猪股K线)
import pigcycle_refresh  # 猪周期实时喂价每日自动拉取(pigcycle_refresh.py)
import macro_db       # 宏观 CPI/PPI 数据层(独立 data/macro.db)
import macro_fetch    # 宏观 CPI/PPI 拉取(东财数据中心, 转引统计局)
import macro_analysis  # CPI/PPI 读数/环比/剪刀差解读引擎
import macro_ppi_official  # PPI 全国环比(统计局官方发布稿一手源, 东财 PPI 无环比列)

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DASHBOARD = os.path.join(BASE, "dashboard.html")
MARKET = os.path.join(BASE, "market.html")
SECTOR = os.path.join(BASE, "sector.html")           # 板块行情看板查询页
PIGCYCLE = os.path.join(BASE, "pigcycle.html")        # 猪周期独立模块页面
MACRO = os.path.join(BASE, "macro.html")              # 宏观 CPI/PPI 独立页面
CONFIG = os.path.join(BASE, "config", "funds.json")   # 兼容读取源(迁移用), 数据真相源为数据库
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")  # set DASHBOARD_HOST=0.0.0.0 on server deployment
SYNC_LOCK = threading.Lock()
SYNC_STATE = os.path.join(BASE, "data", "sync_state.json")  # 兼容备份路径
BOARDS_REFRESH_LOCK = threading.Lock()


def trigger_boards_refresh(force=False):
    """后台刷新东财板块缓存(行情+资金流), 避免首个 /api/sector 请求阻塞 60-90s。
    返回是否实际启动了刷新线程(已在刷新中则 False)。"""
    if BOARDS_REFRESH_LOCK.locked():
        return False
    if not force and (fund_db.kv_get(fund_tracker._EM_BOARDS_KEY) or {}).get("items"):
        return False  # 已有缓存, 无需后台刷新

    def _run():
        with BOARDS_REFRESH_LOCK:
            try:
                fund_tracker.fetch_em_all_boards(force=True)
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


# ===== 猪周期实时喂价: 常驻每日定时刷新 =====
# 只要 8123 服务在跑, 每天 08:00 自动刷新三路喂价, 不依赖任何外部调度器。
# 用 kv('feed_scheduler_date') 记录最近执行日期, 同日只跑一次;
# 若服务当天 08:00 之后才启动, 启动后会立即补跑一次(当日 catch-up)。
_PIGCYCLE_SCHED_HOUR = 8
_PIGCYCLE_SCHED_KV = "feed_scheduler_date"


def _pigcycle_daily_scheduler():
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            last = (pigcycle_db.kv_get(_PIGCYCLE_SCHED_KV) or {}).get("date")
            if now.hour >= _PIGCYCLE_SCHED_HOUR and last != today:
                pigcycle_db.kv_set(_PIGCYCLE_SCHED_KV,
                                   {"date": today, "ts": now.isoformat(timespec="seconds")})
                try:
                    pigcycle_refresh.refresh_all(slaughter_history=False)
                except Exception:
                    pass
        except Exception:
            pass
        # 每 10 分钟检查一次(轻量轮询; 真正刷新每天最多一次)
        time.sleep(600)


def open_read_shared(path):
    """以 FILE_SHARE_DELETE 共享方式读文件(仅 Windows 生效), 允许 make_dashboard 并发
    os.replace 覆盖目标而不报 WinError 5 共享冲突; 非 Windows 或异常时回退普通读。"""
    try:
        if os.name != "nt":
            return open(path, "rb")
        import ctypes, msvcrt
        k32 = ctypes.windll.kernel32
        h = k32.CreateFileW(ctypes.c_wchar_p(path), 0x80000000,
                           0x00000001 | 0x00000002 | 0x00000004,  # READ|WRITE|DELETE
                           None, 3, 0, None)  # OPEN_EXISTING=3
        if h in (0, -1, None):
            return open(path, "rb")
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY)
        return os.fdopen(fd, "rb")
    except Exception:
        return open(path, "rb")

# ---------------------------------------------------------------- 页面通用悬浮"回到顶部"按钮
# 所有服务 HTML 页面统一注入(服务端处理, 页面文件/生成脚本无需各自维护, 新增页面自动带上)。
# 若页面已自带回顶控件(id="fab-top" 如 dashboard / id="back-top"), 则跳过避免重复。
_BACK_TOP_BTN = (
    '<button id="back-top" type="button" title="回到顶部" aria-label="回到顶部">&#8593;</button>\n'
    "<style>\n"
    "#back-top{position:fixed;right:22px;bottom:26px;z-index:99999;width:44px;height:44px;"
    "border-radius:50%;border:1px solid rgba(67,56,202,.4);background:#fff;color:#4338ca;"
    "font-size:22px;font-weight:700;line-height:1;cursor:pointer;padding:0;"
    "box-shadow:0 4px 14px rgba(30,41,59,.2);display:flex;align-items:center;justify-content:center;"
    "opacity:0;visibility:hidden;transform:translateY(10px);"
    "transition:opacity .22s ease,transform .22s ease,background .22s ease,color .22s ease}\n"
    "#back-top.show{opacity:1;visibility:visible;transform:translateY(0)}\n"
    "#back-top:hover{background:#4338ca;color:#fff}\n"
    "</style>\n"
    "<script>\n"
    "(function(){var b=document.getElementById(\"back-top\");if(!b)return;var s=false;\n"
    "function u(){var y=window.pageYOffset||document.documentElement.scrollTop||document.body.scrollTop||0;\n"
    "if(y>240){if(!s){b.classList.add(\"show\");s=true;}}else if(s){b.classList.remove(\"show\");s=false;}}\n"
    "window.addEventListener(\"scroll\",u,{passive:true});u();\n"
    "b.addEventListener(\"click\",function(){try{window.scrollTo({top:0,behavior:\"smooth\"});}"
    "catch(e){window.scrollTo(0,0);}});\n"
    "})();\n"
    "</script>\n"
)

def _inject_backtop(raw):
    """把悬浮回到顶部按钮注入 HTML 页面: 返回 bytes。

    - 输入为 bytes(rb 读盘结果); 无 </body> / 非 UTF-8 / 页面已自带回顶按钮时原样返回。
    """
    if not raw or b"fab-top" in raw or b"back-top" in raw:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    idx = text.lower().rfind("</body>")
    if idx == -1:
        return raw
    return (text[:idx] + _BACK_TOP_BTN + text[idx:]).encode("utf-8")

def read_sync_state():
    """读取每日同步记账: {"date":"2026-08-13","status":"ok|failed|running","ts":...}"""
    v = fund_db.kv_get("sync_state.json")
    if v is not None:
        return v if isinstance(v, dict) else {}
    try:
        with open(SYNC_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def write_sync_state(date, status):
    """记录当日同步状态(每日只自动执行一次的依据; 成功/失败都记账)。仅存数据库。"""
    fund_db.kv_set("sync_state.json", {"date": date, "status": status,
                                       "ts": datetime.datetime.now().isoformat(timespec="seconds")})

def daily_sync_done():
    """当日是否已处理过同步(无论成功/失败, 当日不再自动重复)"""
    return read_sync_state().get("date") == datetime.date.today().isoformat()

def need_sync():
    """页面打开是否需要同步: 无快照 / 快照不是今天生成 / 历史库有缺口"""
    v = fund_db.kv_get("latest.json")
    if v is None:
        lp = os.path.join(BASE, "data", "latest.json")
        if not os.path.exists(lp):
            return True
        try:
            with open(lp, encoding="utf-8") as f:
                v = json.load(f)
        except Exception:
            return True
    try:
        gen = (v or {}).get("generated_at", "")
        if gen[:10] != datetime.date.today().isoformat():
            return True
    except Exception:
        return True
    return False

def _run_script(script, *extra, attempts=3):
    """运行引擎子脚本, 失败重试(应对 Windows 瞬时文件锁/只读位); 返回 (ok, log)。
    extra: 透传给脚本的额外参数(如 fund_tracker.py refreshpositions)。"""
    last = ""
    for i in range(attempts):
        r = subprocess.run([PY, os.path.join(BASE, script)] + list(extra), capture_output=True, text=True,
                           timeout=180, close_fds=True)
        last = (r.stdout[-700:] + "\n[stderr]\n" + r.stderr[-700:]).strip()
        if r.returncode == 0:
            return True, last
        # 瞬时失败: 写错误日志 + 短暂停顿后重试(让占用方释放句柄)
        _log_sync_error(script, r.returncode, last)
        time.sleep(1.5 * (i + 1))
    return False, last

def _ensure_dashboard():
    """确保 dashboard.html 存在: 缺失时(全新空库/新 clone/被误删)自动生成, 使页面始终可加载、导入 UI 可达。
    空库下 make_dashboard.py 仍会生成含 ECharts 框架与"数据备份/初始化"面板的空看板(不依赖任何数据),
    用户即可在页面上导入备份或添加基金。生成失败不影响服务启动。"""
    if os.path.exists(DASHBOARD):
        return True
    try:
        ok, log = _run_script("make_dashboard.py")
        if ok and os.path.exists(DASHBOARD):
            return True
        print(f"[warn] 自动生成 dashboard.html 失败: {log[-400:]}")
    except Exception as e:
        print(f"[warn] 自动生成 dashboard.html 异常: {e}")
    return os.path.exists(DASHBOARD)

def _log_sync_error(script, rc, log):
    try:
        p = os.path.join(BASE, "data", "sync_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {script} FAILED rc={rc}\n{log}\n{'-'*60}\n")
    except Exception:
        pass

def refresh_engine():
    """重跑引擎 + 重新生成看板; 检查子进程退出码, 任一失败则重试并如实反馈。"""
    ok_ft, log_ft = _run_script("fund_tracker.py")
    ok_md, log_md = _run_script("make_dashboard.py")
    if not (ok_ft and ok_md):
        return f"[ENGINE FAILED] fund_tracker={ok_ft} make_dashboard={ok_md}\n{log_ft}\n{log_md}"
    return f"[ok] fund_tracker + make_dashboard\n{log_ft}\n{log_md}"

def refresh_light():
    """轻量刷新(删除/隐藏基金时用, 秒级): 按 config 过滤 latest 快照后重建看板, 不重跑引擎"""
    v = fund_db.kv_get("latest.json")
    if v is None:
        return refresh_engine()
    data = v
    cfg = fund_db.kv_get("funds.json")
    cfg = cfg.get("funds", {}) if isinstance(cfg, dict) else {}
    visible = [k for k, v2 in cfg.items() if not v2.get("hidden")]
    data["funds"] = {k: v2 for k, v2 in data["funds"].items() if k in visible}
    fund_db.kv_set("latest.json", data)
    r = subprocess.run([PY, os.path.join(BASE, "make_dashboard.py")], capture_output=True, text=True, timeout=60, close_fds=True)
    return r.stdout[-400:] + r.stderr[-300:]

def refresh_positions_and_dashboard():
    """轻量刷新(保存/添加/交易增删后秒级更新看板, 不触发全量联网净值同步):
    用库内最新 trades 重算各基金持仓快照(新增基金仅抓该基金净值), 再重建 dashboard.html。
    相比 refresh_engine 的全量联网建模(~13s), 此路径仅 ~1s 级, 是"保存/添加基金变快"的核心。"""
    _ok_pos, log_pos = _run_script("fund_tracker.py", "refreshpositions")
    _ok, log = _run_script("make_dashboard.py")
    return (log_pos + "\n" + log)[-400:]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默日志

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            if not os.path.exists(DASHBOARD):
                _ensure_dashboard()
            try:
                with open_read_shared(DASHBOARD) as f:
                    body = _inject_backtop(f.read())
            except FileNotFoundError:
                self._json({"ok": False, "message": "dashboard.html 生成失败, 请检查服务日志"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/market", "/market.html"):
            """市场热点独立页(板块资金流): 由 make_dashboard.py 生成, 缺失时提示先跑引擎"""
            if not os.path.exists(MARKET):
                self._json({"ok": False, "message": "market.html 未生成 — 请先运行引擎同步(python fund_tracker.py)"}, 404)
                return
            try:
                with open_read_shared(MARKET) as f:
                    body = _inject_backtop(f.read())
            except FileNotFoundError:
                self._json({"ok": False, "message": "market.html 读取失败"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/sector", "/sector.html"):
            """板块行情看板(行情+主力/散户资金流): 由 make_dashboard.py 生成"""
            if not os.path.exists(SECTOR):
                self._json({"ok": False, "message": "sector.html 未生成 — 请先运行引擎同步(python fund_tracker.py)"}, 404)
                return
            try:
                with open_read_shared(SECTOR) as f:
                    body = _inject_backtop(f.read())
            except FileNotFoundError:
                self._json({"ok": False, "message": "sector.html 读取失败"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/pigcycle", "/pigcycle.html"):
            """猪周期独立模块页面: 单独页面, 物理隔离于基金看板(独立库 data/pigcycle.db)"""
            if not os.path.exists(PIGCYCLE):
                self._json({"ok": False, "message": "pigcycle.html 未生成"}, 404)
                return
            try:
                with open_read_shared(PIGCYCLE) as f:
                    body = _inject_backtop(f.read())
            except FileNotFoundError:
                self._json({"ok": False, "message": "pigcycle.html 读取失败"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/macro", "/macro.html"):
            """宏观 CPI/PPI 独立页面: 独立库 data/macro.db, 图表+解读(与基金/猪周期均隔离)"""
            if not os.path.exists(MACRO):
                self._json({"ok": False, "message": "macro.html 未生成"}, 404)
                return
            try:
                with open_read_shared(MACRO) as f:
                    body = _inject_backtop(f.read())
            except FileNotFoundError:
                self._json({"ok": False, "message": "macro.html 读取失败"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/macro/data":
            """宏观数据: ?type=cpi|ppi[&limit=N] -> 库内升序行(dict)"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = (qs.get("type") or [""])[0].strip()
            if kind not in ("cpi", "ppi"):
                self._json({"ok": False, "message": "type 需为 cpi 或 ppi"}, 400)
                return
            try:
                rows = macro_db.get_series(kind)
                try:
                    limit = int((qs.get("limit") or ["0"])[0])
                    if limit > 0:
                        rows = rows[-limit:]
                except ValueError:
                    pass
                self._json({"ok": True, "type": kind, "count": len(rows), "rows": rows})
            except Exception as e:
                self._json({"ok": False, "message": str(e)})
        elif path == "/api/macro/meta":
            """宏观元信息: 各表覆盖 + 最新一期 + 自动刷新记账 + 发布说明"""
            try:
                cov = macro_db.coverage()
                self._json({
                    "ok": True, "coverage": cov,
                    "auto": macro_db.kv_get("macro_refresh_date") or {},
                    "release_note": "CPI/PPI 为统计局月度数据, 一般于次月 9~12 日发布(如遇春节等顺延); 本页每日打开自动检查更新一次。",
                })
            except Exception as e:
                self._json({"ok": False, "message": str(e)})
        elif path == "/api/macro/analysis":
            """解读: 由库内 CPI/PPI 现算 同比/环比/剪刀差/动能 + 规则化信号与条件式观察"""
            try:
                cpi_rows = macro_db.get_series("cpi")
                ppi_rows = macro_db.get_series("ppi")
                if not cpi_rows or not ppi_rows:
                    self._json({"ok": False, "message": "库内尚无数据, 请先拉取(页面上方按钮)"})
                    return
                a = macro_analysis.compute(cpi_rows, ppi_rows)
                cov = macro_db.coverage()
                a["coverage"] = cov
                self._json(a)
            except Exception as e:
                self._json({"ok": False, "message": "解读计算失败: %s" % e})
        elif path == "/api/sector":
            """板块行情看板数据接口: ?q=板块名称或BK码 -> 行情 + 主力/散户(五档)资金流。
            缓存缺失时后台触发刷新并返回 loading, 前端轮询即可。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json({"ok": False, "message": "缺少查询参数 q(板块名称或 BK 码)"})
                return
            items = (fund_db.kv_get(fund_tracker._EM_BOARDS_KEY) or {}).get("items") or []
            if not items:
                trigger_boards_refresh(force=True)
                self._json({"ok": True, "loading": True,
                            "message": "板块数据缓存中, 请约 1 分钟后再查询(或刷新重试)"})
                return
            detail = fund_tracker.get_sector_detail(q)
            if not detail:
                # 尝试模糊兜底: 返回若干候选板块名, 便于前端提示
                cands = [it.get("name") for it in items if q in (it.get("name") or "")][:8]
                self._json({"ok": False, "message": f"未找到板块: {q}", "candidates": cands})
                return
            self._json({"ok": True, "detail": detail,
                        "boards_updated_at": (fund_db.kv_get(fund_tracker._EM_BOARDS_KEY) or {}).get("updated_at")})
        elif path == "/api/boards":
            """板块行情总览: 返回 em_boards 缓存中全部板块(行业+概念)的行情+主力资金流,
            供板块看板(sector.html)默认展示'全部板块'总览, 前端点击某行可下钻单个板块详情。"""
            data = fund_db.kv_get(fund_tracker._EM_BOARDS_KEY) or {}
            items = data.get("items") or []
            if not items:
                trigger_boards_refresh(force=True)
                self._json({"ok": True, "loading": True,
                            "message": "板块数据缓存中, 请约 1 分钟后再刷新重试"})
                return
            boards = [{
                "code": it.get("code"), "name": it.get("name"), "kind": it.get("kind"),
                "price": it.get("price"), "chg_pct": it.get("chg_pct"),
                "vol": it.get("vol"), "turnover": it.get("turnover"),
                "amplitude": it.get("amplitude"), "turnover_rate": it.get("turnover_rate"),
                "main_net": it.get("main_net"), "main_ratio": it.get("main_ratio"),
                "super_net": it.get("super_net"), "big_net": it.get("big_net"),
                "mid_net": it.get("mid_net"), "small_net": it.get("small_net"),
            } for it in items]
            self._json({"ok": True, "boards": boards,
                        "updated_at": data.get("updated_at"), "count": len(boards)})
        elif path in ("/favicon.ico", "/favicon.svg"):
            """站点图标(避免浏览器请求 /favicon.ico 返回 404)"""
            fp = os.path.join(BASE, "static", "favicon.svg")
            if not os.path.isfile(fp):
                self._json({"ok": False, "message": "not found"}, 404)
                return
            with open_read_shared(fp) as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/navs/"):
            """趋势数据接口: 从数据库读取某基金全量历史净值(页面曲线数据源)
            返回 [[date, nav], ...] 升序"""
            code = path[len("/api/navs/"):].strip()
            if not (code.isdigit() and len(code) == 6):
                self._json({"ok": False, "message": "基金代码无效"}, 400)
                return
            try:
                ser = fund_db.get_nav_series(code)
                data = [[r[0], r[1]] for r in ser if r[0] and r[1] is not None]
                self._json({"ok": True, "code": code, "count": len(data), "data": data})
            except Exception as e:
                self._json({"ok": False, "message": str(e)})
        elif path == "/api/navs":
            """趋势数据调试接口: 返回各基金历史净值概要(曲线数据来源 = 数据库)"""
            try:
                out = {}
                cfg = fund_db.kv_get("funds.json")
                cfg = cfg.get("funds", {}) if isinstance(cfg, dict) else {}
                for code in cfg:
                    if cfg[code].get("hidden"):
                        continue
                    mn, mx, cnt = fund_db.get_nav_range(code)
                    out[code] = {"count": cnt, "start": mn, "end": mx}
                self._json({"ok": True,
                            "note": "趋势数据由引擎(东财pingzhongdata)拉取入库后内嵌到dashboard.html的CFG.navs, 页面打开直接用内嵌数据绘制曲线, 不发任何趋势接口请求",
                            "navs": out})
            except Exception as e:
                self._json({"ok": False, "message": str(e)})
        elif path == "/api/sync/check":
            """轻量检查(只读, <50ms): 页面打开时先问是否需要同步, 避免每次打开都显示覆盖层/等待"""
            today = datetime.date.today().isoformat()
            st = read_sync_state()
            if st.get("date") == today:
                ok = st.get("status") == "ok"
                self._json({"ok": True, "need": False, "state": st.get("status"),
                            "message": "今日已同步成功" if ok else "今日同步未成功, 可点重试"})
                return
            if not need_sync():
                self._json({"ok": True, "need": False, "state": "ok", "message": "今日数据已是最新"})
                return
            self._json({"ok": True, "need": True, "state": st.get("status", "") or "pending"})
        elif path == "/api/pigcycle/fundnav":
            """返回某只基金全量净值(升序), 供猪周期喂价图表叠加净值曲线(右轴)。
            ?code=014414。只读基金看板库 data/fund_history.db, 不写任何数据。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0].strip()
            if not code:
                self._json({"ok": False, "message": "缺少 code 参数"})
                return
            try:
                ser = fund_db.get_nav_series(code)
            except Exception as e:
                self._json({"ok": False, "message": "读取净值失败: %s" % e})
                return
            data = [{"date": r[0], "nav": r[1]} for r in ser if r[1] is not None]
            self._json({"ok": True, "code": code, "count": len(data), "data": data})
        elif path == "/api/pigcycle/data":
            """返回单个指标全量序列(指标隔离: 只读对应表, dict 列表)。
            ?indicator=sow_inventory|piglet_price|frozen_inventory"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ind = (qs.get("indicator") or [""])[0].strip()
            if ind not in pigcycle_db.INDICATORS:
                self._json({"ok": False, "message": "未知指标: %s" % ind})
                return
            rows = pigcycle_db.get_series(ind, ascending=True)
            self._json({"ok": True, "indicator": ind,
                        "unit": pigcycle_db.INDICATORS[ind]["unit"],
                        "count": len(rows), "data": rows})
        elif path == "/api/pigcycle/meta":
            """返回指标元信息(名称/单位/频率/说明/序列), 供页面渲染隔离卡片。"""
            out = {}
            for k, m in pigcycle_db.INDICATORS.items():
                out[k] = {"name": m["name"], "unit": m["unit"], "freq": m["freq"],
                          "desc": m["desc"], "series": m.get("series", [])}
            self._json({"ok": True, "indicators": out})
        elif path == "/api/pigcycle/sources":
            """返回干净的数据源配置: 周度监测地址 + 能繁官方发布列表(供页面选择/展示)。"""
            try:
                cfg = pigcycle_fetch.load_sources()
            except Exception:
                cfg = {}
            self._json({"ok": True, "sources": cfg})
            return
        elif path == "/api/pigcycle/prices":
            """仔猪/生猪周度价格预览: ?history=1&limit=16 返回历史序列; 默认最新一篇。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("history", ["0"])[0] in ("1", "true"):
                try:
                    limit = int(qs.get("limit", ["16"])[0])
                except Exception:
                    limit = 16
                try:
                    res = pigcycle_fetch.fetch_piglet_history(limit=limit)
                except Exception as e:
                    self._json({"ok": False, "message": "抓取失败: %s" % e})
                    return
                self._json(res)
                return
            try:
                res = pigcycle_fetch.fetch_piglet_weekly()
            except Exception as e:
                self._json({"ok": False, "message": "抓取失败: %s" % e})
                return
            self._json(res)
            return
        elif path == "/api/pigcycle/analysis/meta":
            """返回周期分析指标元信息(阈值带/利弊/官方来源), 供页面渲染分析卡片。"""
            self._json({"ok": True, "analysis": pigcycle_db.analysis_meta()})
            return
        elif path == "/api/pigcycle/analysis/data":
            """返回单个分析指标序列。capacity 额外返回季度+月度汇总与保有量分区。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0].strip()
            if key not in pigcycle_db.ANALYSIS:
                self._json({"ok": False, "message": "未知分析指标: %s" % key})
                return
            if key == "capacity":
                out = pigcycle_db.get_capacity_analysis()
                out["quarterly_series"] = pigcycle_db.get_series("sow_inventory", ascending=True)
                self._json({"ok": True, "key": key, "data": out})
                return
            rows = pigcycle_db.get_analysis_series(key, ascending=True)
            self._json({"ok": True, "key": key,
                        "unit": pigcycle_db.ANALYSIS[key]["unit"],
                        "count": len(rows), "data": rows})
            return
        elif path == "/api/pigcycle/feed/meta":
            """返回实时喂价元信息(名称/单位/频率/来源/序列), 供页面渲染隔离卡片。"""
            self._json({"ok": True, "feeds": pigcycle_db.feed_meta()})
            return
        elif path == "/api/pigcycle/feed/data":
            """返回单个实时喂价全量序列。?feed=spot_hog_daily|slaughter_rate_weekly"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            feed = (qs.get("feed") or [""])[0].strip()
            if feed not in pigcycle_db.FEEDS:
                self._json({"ok": False, "message": "未知实时喂价: %s" % feed})
                return
            rows = pigcycle_db.get_feed_series(feed, ascending=True)
            self._json({"ok": True, "feed": feed,
                        "unit": pigcycle_db.FEEDS[feed]["unit"],
                        "count": len(rows), "data": rows})
            return
        elif path.startswith("/static/"):
            """本地静态资源(echarts 等), 避免依赖外部 CDN"""
            rel = path[len("/static/"):]
            fp = os.path.join(BASE, "static", rel)
            if not os.path.isfile(fp) or ".." in rel:
                self._json({"ok": False, "message": "not found"}, 404)
                return
            with open_read_shared(fp) as f:
                body = f.read()
            ctype = "application/javascript" if fp.endswith(".js") else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/sync":
            """页面打开时调用: 每日只自动执行一次昨日/历史检查补充。
            - 当日已处理过(成功或失败) -> need=false, 不再自动触发
            - ?force=1 为手动重试(忽略当日记账, 强制执行)
            - 同步完成 -> done=true; 失败 -> ok=false + message(页面可进入可重试)"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = qs.get("force", ["0"])[0] in ("1", "true")
            today = datetime.date.today().isoformat()
            st = read_sync_state()
            if not force and daily_sync_done():
                ok = st.get("status") == "ok"
                self._json({"ok": True, "need": False, "state": st.get("status"),
                            "message": "今日已同步成功" if ok else "今日同步未成功, 可点击页面提示重试"})
                return
            if not force and not need_sync():
                # 数据已是最新(如今天手动跑过引擎): 记账后跳过, 不再重复执行
                write_sync_state(today, "ok")
                self._json({"ok": True, "need": False, "state": "ok", "message": "今日数据已是最新"})
                return
            # 先记账(running), 防止并发/重入导致当日执行多次
            write_sync_state(today, "running")
            try:
                with SYNC_LOCK:
                    log = refresh_engine()
                # 引擎返回值含 [ENGINE FAILED] 标记才视为失败(此前不检查退出码会误报成功)
                failed = log.startswith("[ENGINE FAILED]")
                write_sync_state(today, "failed" if failed else "ok")
                if failed:
                    self._json({"ok": False, "need": True, "done": False, "state": "failed",
                                "message": "同步失败(引擎执行错误), 可重试", "engine": (log or "")[-600:]})
                else:
                    self._json({"ok": True, "need": True, "done": True, "state": "ok",
                                "engine": (log or "")[-400:]})
            except Exception as e:
                write_sync_state(today, "failed")
                self._json({"ok": False, "need": True, "done": False, "state": "failed",
                            "message": f"同步失败: {e}"})
        elif path == "/api/funds":
            # 返回跟踪中的基金(隐藏/伪删除的不显示) + 已删除(hidden)列表供恢复
            allf = fund_db.kv_get("funds.json")
            allf = allf.get("funds", {}) if isinstance(allf, dict) else {}
            visible = {k: v for k, v in allf.items() if not v.get("hidden")}
            deleted = {k: v for k, v in allf.items() if v.get("hidden")}
            self._json({"ok": True, "funds": visible, "deleted": deleted})
        elif path.startswith("/api/funds/state"):
            # 轻量返回单只基金最新快照(交易/持仓/聚合), 供添加/删除交易后局部刷新交易面板, 避免整页 reload
            # 兼容两种调用: 路径 /api/funds/state/<code> 与查询参数 /api/funds/state?code=<code>
            # 注意: 上方 path 已被 urlparse(...).path 剥离查询串, 查询参数须从 self.path 的 query 部分取
            code = path[len("/api/funds/state"):].lstrip("/").split("?")[0].strip()
            if not code:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (qs.get("code") or [""])[0].strip()
            if not (code.isdigit() and len(code) == 6):
                self._json({"ok": False, "message": "基金代码无效"}, 400); return
            lat = fund_db.kv_get("latest.json") or {}
            fund = (lat.get("funds") or {}).get(code)
            if not fund:
                self._json({"ok": False, "message": "未找到基金快照(可能已隐藏)"}); return
            self._json({"ok": True, "code": code, "fund": {
                "trades": fund.get("trades") or [],
                "position": fund.get("position") or {},
                "trade_summary": fund.get("trade_summary") or {},
            }})
        elif path == "/api/settings":
            st = fund_db.kv_get("settings.json") or {}
            if not isinstance(st, dict):
                st = {}
            st.setdefault("auto_refresh", True)
            st.setdefault("refresh_seconds", 60)
            self._json({"ok": True, "settings": st})
        elif path == "/api/export":
            """导出数据备份: 基金配置 + 交易记录 + 设置, 组合为单个 JSON bundle 供下载/初始化空库"""
            funds = fund_db.kv_get("funds.json") or {"funds": {}}
            trades = fund_db.kv_get("trades.json") or {"trades": []}
            settings = fund_db.kv_get("settings.json") or {}
            if not isinstance(funds, dict):
                funds = {"funds": {}}
            if not isinstance(trades, dict):
                trades = {"trades": []}
            if not isinstance(settings, dict):
                settings = {}
            bundle = {
                "app": "jijing-fx",
                "version": 1,
                "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "funds": funds,
                "trades": trades,
                "settings": settings,
            }
            self._json(bundle)
        else:
            self._json({"ok": False, "message": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            ln = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
        except Exception:
            data = {}
        # ---------------- 宏观 CPI/PPI 独立模块 API ----------------
        if path == "/api/macro/auto":
            """页面打开每日自动触发: 拉取东财 CPI/PPI 全量(幂等 upsert)。
            当日已成功 -> need=false; ?force=1 强制重跑; kv('macro_refresh_date') 记账。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = qs.get("force", ["0"])[0] in ("1", "true")
            today = datetime.date.today().isoformat()
            st = macro_db.kv_get("macro_refresh_date") or {}
            # 滞后补漏: 若库内最新月份落后于"上个自然月"(数据应已发布), 即使当日已刷新也强制重查,
            # 避免发布日当天先开页面(数据未挂网)后当天不再自动拉取的盲区。
            try:
                t = datetime.date.today()
                py, pm = (t.year - 1, 12) if t.month == 1 else (t.year, t.month - 1)
                expected = "%04d-%02d" % (py, pm)
                cov = macro_db.coverage()
                stale = ((cov.get("cpi", {}).get("end") or "") < expected
                         or (cov.get("ppi", {}).get("end") or "") < expected)
            except Exception:
                stale = False
            if not force and not stale and st.get("date") == today and st.get("status") == "ok":
                self._json({"ok": True, "need": False, "state": "ok",
                            "message": "今日已自动刷新(CPI/PPI)"})
                return
            macro_db.kv_set("macro_refresh_date",
                            {"date": today, "status": "running",
                             "ts": datetime.datetime.now().isoformat(timespec="seconds")})
            try:
                r = macro_fetch.fetch_macro()
                res = {}
                ok_all = True
                for kind in ("cpi", "ppi"):
                    v = r.get(kind) or {}
                    if not v.get("ok"):
                        ok_all = False
                        res[kind] = {"ok": False, "message": v.get("message", "抓取失败")}
                        continue
                    n = 0
                    for row in v["rows"]:
                        if kind == "cpi":
                            macro_db.upsert_cpi(row, source=v.get("source", ""),
                                                source_url=v.get("source_url", ""))
                        else:
                            macro_db.upsert_ppi(row, source=v.get("source", ""),
                                                source_url=v.get("source_url", ""))
                        n += 1
                    res[kind] = {"ok": True, "upserted": n,
                                 "latest": v.get("latest"), "count": v.get("count")}
                # 官方 PPI 环比增量(统计局月度发布稿): 东财 PPI 无环比列, 环比以此一手源补充
                mom_note = ""
                try:
                    off = macro_ppi_official.fetch_latest()
                    if off and off.get("period") and off.get("mom") is not None:
                        macro_db.upsert_ppi(
                            {"period": off["period"], "yoy": off.get("yoy"),
                             "mom": off.get("mom"), "base_index": None,
                             "cum_index": None},
                            source=macro_ppi_official.SOURCE_TXT,
                            source_url=off.get("source_url", ""))
                        res.setdefault("ppi", {})["mom_official"] = \
                            "%s mom=%s" % (off["period"], off.get("mom"))
                        mom_note = " · PPI 环比(官方)已更新"
                    else:
                        mom_note = " · 官方 PPI 稿暂未上线"
                except Exception as e:
                    mom_note = " · PPI 官方环比更新失败: %s" % e
                state = "ok" if ok_all else "partial"
                macro_db.kv_set("macro_refresh_date",
                                {"date": today, "status": state,
                                 "ts": datetime.datetime.now().isoformat(timespec="seconds")})
                self._json({"ok": True, "need": True, "done": True, "state": state,
                            "result": res,
                            "message": ("CPI/PPI 自动刷新完成%s" % mom_note)
                                       if ok_all else "CPI/PPI 刷新部分成功"})
            except Exception as e:
                macro_db.kv_set("macro_refresh_date",
                                {"date": today, "status": "failed",
                                 "ts": datetime.datetime.now().isoformat(timespec="seconds")})
                self._json({"ok": False, "need": True, "done": False, "state": "failed",
                            "message": "CPI/PPI 自动刷新异常: %s" % e})
            return
        # ---------------- 猪周期独立模块 API ----------------
        if path == "/api/pigcycle/add":
            """新增/更新单条指标数据(指标隔离)。body 依指标不同含 value / piglet+pork / source_title+source_url。"""
            ind = str(data.get("indicator", "")).strip()
            d = str(data.get("period_date", "")).strip()
            try:
                if ind == "sow_inventory":
                    pigcycle_db.upsert_point(ind, d,
                        value=data.get("value"),
                        source_title=str(data.get("source_title", "") or ""),
                        source_url=str(data.get("source_url", "") or ""),
                        source=str(data.get("source", "") or ""),
                        note=str(data.get("note", "") or ""))
                elif ind == "piglet_price":
                    pigcycle_db.upsert_point(ind, d,
                        piglet=data.get("piglet"), pork=data.get("pork"),
                        source_url=str(data.get("source_url", "") or ""),
                        source=str(data.get("source", "") or ""),
                        note=str(data.get("note", "") or ""))
                else:
                    pigcycle_db.upsert_point(ind, d,
                        value=data.get("value"),
                        source=str(data.get("source", "") or ""),
                        note=str(data.get("note", "") or ""))
            except Exception as e:
                self._json({"ok": False, "message": "添加失败: %s" % e})
                return
            self._json({"ok": True, "message": "已保存 %s · %s" % (ind, d)})
            return
        elif path == "/api/pigcycle/delete":
            """删除单条指标数据(指标隔离)。body: {indicator, period_date}"""
            ind = str(data.get("indicator", "")).strip()
            d = str(data.get("period_date", "")).strip()
            try:
                n = pigcycle_db.delete_point(ind, d)
            except Exception as e:
                self._json({"ok": False, "message": "删除失败: %s" % e})
                return
            self._json({"ok": True, "deleted": n, "message": "已删除 %s · %s" % (ind, d)})
            return
        elif path == "/api/pigcycle/sample":
            """首次体验: 写入示例数据(各表为空时)。"""
            try:
                n = pigcycle_db.load_sample_data()
            except Exception as e:
                self._json({"ok": False, "message": "载入示例失败: %s" % e})
                return
            self._json({"ok": True, "count": n, "message": "已载入示例数据 %d 条(若已有数据则跳过)" % n})
            return
        elif path == "/api/pigcycle/prices":
            """仔猪/生猪周度价格导入: ?history=1 批量导入最近 N 周; 默认导入最新一周。
            body: {history:true, limit:16} 或 {} 。upsert 进 piglet_price(仔猪+生猪同源)。"""
            history = bool(data.get("history")) if isinstance(data, dict) else False
            limit = 16
            try:
                if isinstance(data, dict) and data.get("limit") is not None:
                    limit = int(data.get("limit"))
            except Exception:
                limit = 16
            try:
                if history:
                    res = pigcycle_fetch.fetch_piglet_history(limit=limit)
                else:
                    res = pigcycle_fetch.fetch_piglet_weekly()
            except Exception as e:
                self._json({"ok": False, "message": "抓取失败: %s" % e})
                return
            if not res.get("ok"):
                self._json({"ok": False, "message": res.get("message", "抓取失败"), "detail": res})
                return
            try:
                if history:
                    rows = res.get("rows", [])
                    n = 0
                    for r in rows:
                        if not r.get("date"):
                            continue
                        pigcycle_db.upsert_point("piglet_price", r["date"],
                                                 piglet=r.get("piglet"), pork=r.get("pork"),
                                                 source="农业农村部周度监测", source_url=r.get("url", ""),
                                                 note=r.get("title", ""))
                        n += 1
                    self._json({"ok": True, "imported": n,
                                "message": "已导入 %d 周历史价格（仔猪/生猪）" % n})
                else:
                    r = res
                    pigcycle_db.upsert_point("piglet_price", r["date"],
                                             piglet=r.get("piglet"), pork=r.get("pork"),
                                             source="农业农村部周度监测", source_url=r.get("url", ""),
                                             note=r.get("title", ""))
                    self._json({"ok": True, "imported": 1,
                                "message": "已导入 %s 仔猪 %.2f / 生猪 %.2f 元/公斤" % (
                                    r.get("date"), r.get("piglet") or 0, r.get("pork") or 0)})
            except Exception as e:
                self._json({"ok": False, "message": "导入失败: %s" % e})
                return
            return
        elif path == "/api/pigcycle/import":
            """CSV 导入: body {indicator, rows:[[period_date,value], ...]}。upsert 进对应隔离表。"""
            ind = str(data.get("indicator", "")).strip()
            rows = data.get("rows") or []
            if ind not in pigcycle_db.INDICATORS:
                self._json({"ok": False, "message": "未知指标: %s" % ind})
                return
            if not isinstance(rows, list):
                self._json({"ok": False, "message": "rows 须为数组"})
                return
            n = 0
            for r in rows:
                try:
                    if len(r) < 2:
                        continue
                    pigcycle_db.upsert_point(ind, str(r[0]).strip(), value=r[1],
                                            note=str(r[2]) if len(r) > 2 else "",
                                            source=str(r[3]) if len(r) > 3 else "csv导入")
                    n += 1
                except Exception:
                    pass
            self._json({"ok": True, "upserted": n, "indicator": ind})
            return
        elif path == "/api/pigcycle/analysis/add":
            """分析指标新增/更新: capacity->sow_inventory(季度能繁, 已内置权威历史); grain_pig_ratio->value; index_pe->pe+pb。"""
            key = str(data.get("key", "")).strip()
            d = str(data.get("period_date", "")).strip()
            if key not in pigcycle_db.ANALYSIS:
                self._json({"ok": False, "message": "未知分析指标: %s" % key})
                return
            try:
                if key == "capacity":
                    pigcycle_db.upsert_point("sow_inventory", d, value=data.get("value"),
                        note=str(data.get("note", "") or ""),
                        source=str(data.get("source", "") or "手动录入(季度)"),
                        source_url=str(data.get("source_url", "") or ""))
                elif key == "grain_pig_ratio":
                    pigcycle_db.upsert_analysis(key, d, value=data.get("value"),
                        note=str(data.get("note", "") or ""),
                        source=str(data.get("source", "") or ""),
                        source_url=str(data.get("source_url", "") or ""))
                elif key == "index_pe":
                    pigcycle_db.upsert_analysis(key, d, pe=data.get("pe"), pb=data.get("pb"),
                        note=str(data.get("note", "") or ""),
                        source=str(data.get("source", "") or ""),
                        source_url=str(data.get("source_url", "") or ""))
            except Exception as e:
                self._json({"ok": False, "message": "添加失败: %s" % e})
                return
            self._json({"ok": True, "message": "已保存 %s · %s" % (key, d)})
            return
        elif path == "/api/pigcycle/analysis/delete":
            """分析指标删除: capacity->sow_inventory(季度能繁); 其他->各自表。"""
            key = str(data.get("key", "")).strip()
            d = str(data.get("period_date", "")).strip()
            if key not in pigcycle_db.ANALYSIS:
                self._json({"ok": False, "message": "未知分析指标: %s" % key})
                return
            try:
                if key == "capacity":
                    n = pigcycle_db.delete_point("sow_inventory", d)
                else:
                    n = pigcycle_db.delete_analysis(key, d)
            except Exception as e:
                self._json({"ok": False, "message": "删除失败: %s" % e})
                return
            self._json({"ok": True, "deleted": n, "message": "已删除 %s · %s" % (key, d)})
            return
        elif path == "/api/pigcycle/analysis/fetch":
            """分析指标自动抓取(尽力而为): grain_pig_ratio 无接口->引导手动; index_pe->东财。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0].strip()
            if key not in ("grain_pig_ratio", "index_pe"):
                self._json({"ok": False, "message": "该指标不支持自动抓取: %s" % key})
                return
            try:
                if key == "grain_pig_ratio":
                    res = pigcycle_fetch.fetch_grain_pig_ratio()
                else:
                    res = pigcycle_fetch.fetch_index_pe()
            except Exception as e:
                self._json({"ok": False, "message": "抓取异常: %s" % e})
                return
            if res.get("ok"):
                try:
                    if key == "index_pe":
                        pigcycle_db.upsert_analysis(key, res.get("date"),
                            pe=res.get("pe"), pb=res.get("pb"), source="自动抓取(东财)",
                            source_url="https://quote.eastmoney.com/center/boardlist.html#boards2")
                except Exception:
                    pass
                self._json({"ok": True, "date": res.get("date"), "pe": res.get("pe"),
                            "pb": res.get("pb"), "name": res.get("name"),
                            "message": res.get("message", "已抓取")})
            else:
                self._json({"ok": False, "message": res.get("message", "抓取失败"),
                            "source_url": res.get("source_url", "")})
            return
        elif path == "/api/pigcycle/seed-history":
            """补全能繁权威季度历史(2020Q1–2026Q2)。"""
            try:
                n = pigcycle_db.seed_capacity_history()
            except Exception as e:
                self._json({"ok": False, "message": "载入失败: %s" % e})
                return
            self._json({"ok": True, "upserted": n,
                        "message": "已载入能繁权威历史 %d 个季度(2020Q1–2026Q2)" % n})
            return
        elif path == "/api/pigcycle/history":
            """分析指标3年历史自动抓取(东财, 尽力而为): 当前支持 index_pe。"""
            key = str(data.get("key", "")).strip()
            if key != "index_pe":
                self._json({"ok": False,
                            "message": "该指标暂不支持自动历史抓取: %s（仅 index_pe 支持；能繁已内置权威历史）" % key})
                return
            try:
                res = pigcycle_fetch.fetch_index_pe_history()
            except Exception as e:
                self._json({"ok": False, "message": "抓取异常: %s" % e})
                return
            if res.get("ok"):
                n = 0
                for r in res.get("rows", []):
                    try:
                        pigcycle_db.upsert_analysis("index_pe", r["date"], pe=r.get("pe"), pb=r.get("pb"),
                                                    source="自动抓取(东财估值历史)")
                        n += 1
                    except Exception:
                        pass
                self._json({"ok": True, "upserted": n, "count": res.get("count"),
                            "message": "已写入 %d 条指数 PE 历史(东财)" % n})
            else:
                self._json({"ok": False, "message": res.get("message", "抓取失败")})
            return
        elif path == "/api/pigcycle/feed/auto":
            """页面打开时每日自动触发一次: 日度价全量 upsert + 屠宰最新周 upsert。
            - 当日已自动刷新过(成功) -> need=false, 不再联网;
            - ?force=1 -> 忽略当日记账强制重跑;
            - 记账用 pigcycle.db 的 app_kv('feed_refresh_date'), 与手动按钮互不干扰。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = qs.get("force", ["0"])[0] in ("1", "true")
            today = datetime.date.today().isoformat()
            st = pigcycle_db.kv_get("feed_refresh_date") or {}
            if not force and st.get("date") == today and st.get("status") == "ok":
                self._json({"ok": True, "need": False, "state": "ok",
                            "message": "今日已自动刷新"})
                return
            pigcycle_db.kv_set("feed_refresh_date",
                               {"date": today, "status": "running",
                                "ts": datetime.datetime.now().isoformat(timespec="seconds")})
            try:
                res = pigcycle_refresh.refresh_all(slaughter_history=False)
                ok = (res.get("spot", {}).get("ok", False)
                      and res.get("slaughter", {}).get("ok", False)
                      and res.get("monthly", {}).get("ok", False))
                pigcycle_db.kv_set("feed_refresh_date",
                                   {"date": today, "status": "ok" if ok else "partial",
                                    "ts": datetime.datetime.now().isoformat(timespec="seconds")})
                self._json({"ok": True, "need": True, "done": True, "state": "ok" if ok else "partial",
                            "result": res,
                            "message": "自动刷新完成(日度价+屠宰最新周+月度3年)"})
            except Exception as e:
                pigcycle_db.kv_set("feed_refresh_date",
                                   {"date": today, "status": "failed",
                                    "ts": datetime.datetime.now().isoformat(timespec="seconds")})
                self._json({"ok": False, "need": True, "done": False, "state": "failed",
                            "message": "自动刷新异常: %s" % e})
            return
        elif path == "/api/pigcycle/feed/refresh":
            """实时喂价拉取并刷新: spot_hog_daily 全量 upsert(含历史回Fill);
            slaughter_rate_weekly 默认仅最新周(每日), history=true 扫描历史多周回Fill。
            body: {feed, history?:bool, pages?:int}"""
            feed = str(data.get("feed", "")).strip()
            if feed not in pigcycle_db.FEEDS:
                self._json({"ok": False, "message": "未知实时喂价: %s" % feed})
                return
            history = bool(data.get("history")) if isinstance(data, dict) else False
            try:
                if feed == "spot_hog_daily":
                    res = pigcycle_fetch.fetch_spot_hog_daily()
                    if res.get("ok"):
                        n = 0
                        for r in res.get("rows", []):
                            pigcycle_db.upsert_feed("spot_hog_daily", r["date"], value=r["value"],
                                                   source="中国养猪网", source_url="https://www.zhuwang.com.cn/")
                            n += 1
                        self._json({"ok": True, "upserted": n, "latest": res.get("latest"),
                                    "latest_date": res.get("latest_date"),
                                    "message": "已刷新 %d 个交易日的生猪现货日度价" % n})
                    else:
                        self._json({"ok": False, "message": res.get("message", "抓取失败")})
                elif feed == "slaughter_rate_weekly":
                    if history:
                        pages = 12
                        try:
                            if isinstance(data, dict) and data.get("pages") is not None:
                                pages = int(data.get("pages"))
                        except Exception:
                            pages = 12
                        res = pigcycle_fetch.fetch_slaughter_weekly_history(max_pages=pages)
                        if res.get("ok"):
                            n = 0
                            for r in res.get("rows", []):
                                pigcycle_db.upsert_feed("slaughter_rate_weekly", r["date"],
                                    value=r["rate"], kill_volume=r.get("kill_volume"),
                                    pork_white=r.get("pork_white"),
                                    source="卓创资讯·中国畜牧业协会", source_url=r.get("url"),
                                    note=r.get("title"))
                                n += 1
                            self._json({"ok": True, "upserted": n,
                                        "message": "已刷新 %d 周屠宰开工率历史" % n})
                        else:
                            self._json({"ok": False, "message": res.get("message", "抓取失败")})
                    else:
                        res = pigcycle_fetch.fetch_slaughter_weekly_latest()
                        if res.get("ok"):
                            pigcycle_db.upsert_feed("slaughter_rate_weekly", res["date"],
                                value=res["rate"], kill_volume=res.get("kill_volume"),
                                pork_white=res.get("pork_white"),
                                source="卓创资讯·中国畜牧业协会", source_url=res.get("url"),
                                note=res.get("title"))
                            self._json({"ok": True, "upserted": 1,
                                        "message": "已刷新 %s 周度开工率 %.2f%%" % (res.get("date"), res.get("rate") or 0)})
                        else:
                            self._json({"ok": False, "message": res.get("message", "抓取失败")})
                elif feed == "hog_monthly_moa":
                    # 月度权威数据: 全量回Fill(覆盖近 3 年+, 幂等); history 可指定更早起始月
                    start_ym = "202301"
                    try:
                        if isinstance(data, dict) and data.get("start"):
                            start_ym = str(data.get("start"))
                    except Exception:
                        pass
                    res = pigcycle_fetch.fetch_hog_monthly_moa(start_ym=start_ym)
                    if res.get("ok"):
                        n = 0
                        for r in res.get("rows", []):
                            pigcycle_db.upsert_feed("hog_monthly_moa", r["period"], value=r.get("value"),
                                piglet=r.get("piglet"), pork_white=r.get("pork_white"),
                                kill_volume=r.get("kill_volume"),
                                source="农业农村部·生猪专题", source_url=r.get("url"))
                            n += 1
                        self._json({"ok": True, "upserted": n, "start": res.get("start"),
                                    "end": res.get("end"),
                                    "message": "已刷新 %d 个月度生猪价格(%s~%s)" % (n, res.get("start"), res.get("end"))})
                    else:
                        self._json({"ok": False, "message": res.get("message", "抓取失败")})
            except Exception as e:
                self._json({"ok": False, "message": "刷新异常: %s" % e})
            return
        elif path == "/api/pigcycle/feed/delete":
            """实时喂价删除单条。body: {feed, period_date}"""
            feed = str(data.get("feed", "")).strip()
            d = str(data.get("period_date", "")).strip()
            if feed not in pigcycle_db.FEEDS:
                self._json({"ok": False, "message": "未知实时喂价: %s" % feed})
                return
            try:
                n = pigcycle_db.delete_feed(feed, d)
            except Exception as e:
                self._json({"ok": False, "message": "删除失败: %s" % e})
                return
            self._json({"ok": True, "deleted": n, "message": "已删除 %s · %s" % (feed, d)})
            return
        if path == "/api/funds/save":
            code = str(data.get("code", "")).strip()
            if not code:
                self._json({"ok": False, "message": "缺少基金代码"})
                return
            args = [PY, os.path.join(BASE, "manage_funds.py"), "set", code]
            for flag, key in (("--amount", "buy_amount"), ("--shares", "shares"), ("--buy-nav", "buy_nav")):
                v = data.get(key)
                if v is not None and v != "":
                    args += [flag, str(v)]
            if "tags" in data:
                args += ["--tags", str(data["tags"])]
            if data.get("buy_fee_rate") not in (None, ""):
                try:  # 管理面板填的是百分比(如0.12=0.12%), 转小数存储
                    args += ["--buy-fee-rate", str(round(float(data["buy_fee_rate"]) / 100.0, 6))]
                except (TypeError, ValueError):
                    pass
            if "minute" in data:  # 分时锚: 空串=移除; 6位纯数字由 manage_funds 自动补 sh/sz 前缀
                args += ["--minute", str(data.get("minute") or "").strip()]
            if data.get("minute_name"):
                args += ["--minute-name", str(data["minute_name"])]
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            # 轻量刷新: 现有基金已有快照, 仅重算持仓快照 + 重建看板(秒级, 不联网),
            # 避免 refresh_engine 全量联网建模(~13s)导致保存卡顿
            with SYNC_LOCK:
                log = refresh_positions_and_dashboard()
            self._json({"ok": True, "message": f"已保存并刷新: {msg}", "engine": log[-400:]})
        elif path == "/api/settings":
            cur = fund_db.kv_get("settings.json") or {}
            if not isinstance(cur, dict):
                cur = {}
            if "auto_refresh" in data:
                cur["auto_refresh"] = bool(data["auto_refresh"])
            if "refresh_seconds" in data:
                try:
                    sec = int(data["refresh_seconds"])
                    if 10 <= sec <= 600:
                        cur["refresh_seconds"] = sec
                except (TypeError, ValueError):
                    pass
            fund_db.kv_set("settings.json", cur)
            self._json({"ok": True, "settings": cur})
        elif path == "/api/funds/add":
            code = str(data.get("code", "")).strip()
            if not (code.isdigit() and len(code) == 6):
                self._json({"ok": False, "message": "基金代码须为 6 位数字"})
                return
            args = [PY, os.path.join(BASE, "manage_funds.py"), "add", code]
            if data.get("name"): args += ["--name", str(data["name"])]
            if data.get("buy_amount") not in (None, ""): args += ["--amount", str(data["buy_amount"])]
            if data.get("shares") not in (None, ""): args += ["--shares", str(data["shares"])]
            if data.get("buy_nav") not in (None, ""): args += ["--buy-nav", str(data["buy_nav"])]
            if data.get("anchor"): args += ["--anchor", str(data["anchor"])]
            if data.get("anchor_name"): args += ["--anchor-name", str(data["anchor_name"])]
            if data.get("minute"): args += ["--minute", str(data["minute"])]
            if data.get("minute_name"): args += ["--minute-name", str(data["minute_name"])]
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            # 轻量刷新: refresh_positions_only 会为新增基金抓取其净值(单基金 ~1 请求)并补建快照,
            # 再重建看板; 不再全量联网建模全部基金(~13s) -> 添加基金由 ~13s 降到 ~1s 级
            with SYNC_LOCK:
                log = refresh_positions_and_dashboard()
            self._json({"ok": True, "message": f"已添加并刷新: {msg}", "engine": log[-400:]})
        elif path == "/api/funds/hide":
            code = str(data.get("code", "")).strip()
            r = subprocess.run([PY, os.path.join(BASE, "manage_funds.py"), "hide", code],
                               capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            with SYNC_LOCK:
                log = refresh_light()  # 删除用轻量刷新, 秒级生效
            self._json({"ok": True, "message": f"已伪删除(隐藏): {msg}", "engine": log[-400:]})
        elif path == "/api/funds/unhide":
            code = str(data.get("code", "")).strip()
            r = subprocess.run([PY, os.path.join(BASE, "manage_funds.py"), "unhide", code],
                               capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            with SYNC_LOCK:
                # 智能刷新: 基金数据仍留在 latest 快照时秒级重建; 已被过滤移除时全量补数据
                in_latest = False
                try:
                    lv = fund_db.kv_get("latest.json")
                    if lv is None:
                        lp = os.path.join(BASE, "data", "latest.json")
                        with open(lp, encoding="utf-8") as f:
                            lv = json.load(f)
                    in_latest = code in (lv or {}).get("funds", {})
                except Exception:
                    pass
                log = refresh_light() if in_latest else refresh_engine()
            self._json({"ok": True, "message": f"已恢复显示: {msg}", "engine": log[-400:]})
        elif path == "/api/funds/purge":
            code = str(data.get("code", "")).strip()
            if not code:
                self._json({"ok": False, "message": "缺少基金代码"})
                return
            r = subprocess.run([PY, os.path.join(BASE, "manage_funds.py"), "purge", code],
                               capture_output=True, text=True, timeout=60, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            with SYNC_LOCK:
                log = refresh_engine()
            self._json({"ok": True, "message": f"已彻底删除: {msg}", "engine": log[-400:]})
        elif path == "/api/trades/add":
            code = str(data.get("code", "")).strip()
            ttype = str(data.get("type", "buy")).strip().lower()
            if ttype not in ("buy", "sell", "dividend"):
                self._json({"ok": False, "message": "type 须为 buy/sell/dividend"})
                return
            args = [PY, os.path.join(BASE, "manage_funds.py"), "trade", code,
                    "--type", ttype]
            for flag, key in (("--amount", "amount"), ("--shares", "shares"),
                              ("--nav", "nav"), ("--date", "date"), ("--fee", "fee")):
                v = data.get(key)
                if v not in (None, ""):
                    args += [flag, str(v)]
            if data.get("clear"):
                args += ["--clear"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            # 轻量刷新: 先用库内最新 trades 重算各基金持仓并写回 latest.json 快照(秒级, 不联网),
            # 再重跑 make_dashboard 重新生成页面。这样新增的分红会进入快照, 看板列表/图表才能显示。
            with SYNC_LOCK:
                _ok_pos, log_pos = _run_script("fund_tracker.py", "refreshpositions")
                _ok, log = _run_script("make_dashboard.py")
                log = (log_pos + "\n" + log)[-400:]
            self._json({"ok": True, "message": f"已添加交易并刷新: {msg}", "engine": log[-400:]})
        elif path == "/api/trades/del":
            tid = str(data.get("id", "")).strip()
            r = subprocess.run([PY, os.path.join(BASE, "manage_funds.py"), "tradedel", tid],
                               capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            # 同 /api/trades/add: 轻量刷新快照持仓 + 重跑 make_dashboard(不触发联网同步)
            with SYNC_LOCK:
                _ok_pos, log_pos = _run_script("fund_tracker.py", "refreshpositions")
                _ok, log = _run_script("make_dashboard.py")
                log = (log_pos + "\n" + log)[-400:]
            self._json({"ok": True, "message": f"已删除交易记录并刷新: {msg}", "engine": log[-400:]})
        elif path == "/api/fund/est-correction":
            code = str(data.get("code", "")).strip()
            state = str(data.get("state", "")).strip().lower()
            if not (code.isdigit() and len(code) == 6):
                self._json({"ok": False, "message": "基金代码须为 6 位数字"})
                return
            if state not in ("on", "off"):
                self._json({"ok": False, "message": "state 须为 on 或 off"})
                return
            r = subprocess.run([PY, os.path.join(BASE, "manage_funds.py"), "est-correction", code, state],
                               capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            # 开启: 需要联网同步才能生成修正 -> 全量引擎刷新; 关闭: 已清库, 仅轻量重建看板即可
            with SYNC_LOCK:
                if state == "on":
                    log = refresh_engine()
                else:
                    _ok, log = _run_script("make_dashboard.py")
            self._json({"ok": True, "message": f"已{('开启' if state == 'on' else '关闭')}预估修正: {msg}",
                        "engine": log[-400:] if isinstance(log, str) else log})
        elif path == "/api/import":
            """导入数据备份: 覆盖写入 funds/trades/settings 并重建看板(可用于空库初始化/换机迁移)"""
            if not isinstance(data, dict):
                self._json({"ok": False, "message": "无效的导入数据(应为 JSON 对象)"})
                return
            funds_in = data.get("funds")
            trades_in = data.get("trades")
            settings_in = data.get("settings")
            # 归一化: 兼容 {"funds": {...}} 与纯 {...}; trades 兼容 {"trades": [...]} 与 [...]
            funds_dict = {}
            if isinstance(funds_in, dict):
                funds_dict = funds_in.get("funds", funds_in)
            trades_list = []
            if isinstance(trades_in, list):
                trades_list = trades_in
            elif isinstance(trades_in, dict):
                tl = trades_in.get("trades")
                if isinstance(tl, list):
                    trades_list = tl
            if not isinstance(funds_dict, dict):
                funds_dict = {}
            if not isinstance(trades_list, list):
                trades_list = []
            # 写回数据库(覆盖)
            fund_db.kv_set("funds.json", {"funds": funds_dict})
            fund_db.kv_set("trades.json", {"trades": trades_list})
            if isinstance(settings_in, dict) and settings_in:
                fund_db.kv_set("settings.json", settings_in)
            # 重建快照 + 看板(空库会触发联网拉取净值); 即便引擎失败也先保留已写入数据
            try:
                with SYNC_LOCK:
                    log = refresh_engine()
            except Exception as e:
                self._json({"ok": False, "message": f"数据已写入, 但重建看板失败: {e}",
                            "funds": len(funds_dict), "trades": len(trades_list)})
                return
            self._json({"ok": True, "message": "导入成功并重建看板",
                        "funds": len(funds_dict), "trades": len(trades_list),
                        "engine": log[-400:]})
        elif path == "/api/refresh":
            with SYNC_LOCK:
                log = refresh_engine()
            self._json({"ok": True, "message": "已刷新", "engine": log[-400:]})
        else:
            self._json({"ok": False, "message": "not found"}, 404)

if __name__ == "__main__":
    # 空库/全新环境: 先确保 dashboard.html 存在, 否则页面无法加载、导入 UI 不可达
    _ensure_dashboard()
    # 后台预热板块缓存(行情看板数据源), 不阻塞服务启动
    trigger_boards_refresh()
    # 猪周期喂价常驻每日定时刷新(每天 08:00 自刷, 服务在跑即生效)
    threading.Thread(target=_pigcycle_daily_scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Fund dashboard started: http://{HOST}:{PORT}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
