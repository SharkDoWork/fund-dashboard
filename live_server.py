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

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DASHBOARD = os.path.join(BASE, "dashboard.html")
CONFIG = os.path.join(BASE, "config", "funds.json")   # 兼容读取源(迁移用), 数据真相源为数据库
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
SYNC_LOCK = threading.Lock()
SYNC_STATE = os.path.join(BASE, "data", "sync_state.json")  # 兼容备份路径


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
                    body = f.read()
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
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            with SYNC_LOCK:
                log = refresh_engine()
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
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, close_fds=True)
            msg = (r.stdout or r.stderr).strip()
            if r.returncode != 0:
                self._json({"ok": False, "message": msg})
                return
            with SYNC_LOCK:
                log = refresh_engine()
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"基金看板服务已启动: http://127.0.0.1:{PORT}   (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
