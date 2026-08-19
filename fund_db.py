# -*- coding: utf-8 -*-
"""
基金历史数据持久层 (SQLite)
============================
三张表, 全部历史数据只增不删:
  nav_history : 官方发布的全量净值历史(每次运行从 LSJZ 拉取后 upsert, 持续累积)
  snapshots   : 引擎每次运行的一份完整预估快照(含模型预估/基金实时/基线)
  corrections : 每个交易日的"模型预估 vs 官方实际"修正记录
                —— 官方净值通常当日 20:00 后发布, 因此采用"延迟补生成":
                   任何一次运行都会检查: 某交易日已有快照、但当日官方净值已入库
                   且尚无修正记录 -> 自动补生成修正记录(末次预估/预估区间/偏差)
"""
import json, os, sqlite3, datetime, stat, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "fund_history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_history (
  trade_date   TEXT NOT NULL,
  fund_code    TEXT NOT NULL,
  dwjz         REAL,
  ljjz         REAL,
  official_chg_pct REAL,
  source       TEXT DEFAULT '',
  updated_at   TEXT,
  PRIMARY KEY (trade_date, fund_code)
);
CREATE TABLE IF NOT EXISTS snapshots (
  ts           TEXT NOT NULL,
  fund_code    TEXT NOT NULL,
  trade_date   TEXT,
  model_chg_pct REAL,
  adjusted_chg_pct REAL,
  official_live_chg_pct REAL,
  live_price   REAL,
  baseline_date TEXT,
  baseline_nav REAL,
  market_status TEXT,
  quote_time   TEXT,
  PRIMARY KEY (ts, fund_code)
);
CREATE TABLE IF NOT EXISTS corrections (
  trade_date   TEXT NOT NULL,
  fund_code    TEXT NOT NULL,
  official_dwjz REAL,
  official_chg_pct REAL,
  last_model_chg_pct REAL,
  min_model_chg_pct REAL,
  max_model_chg_pct REAL,
  snap_count   INTEGER DEFAULT 0,
  bias_pct     REAL,
  corrected_at TEXT,
  PRIMARY KEY (trade_date, fund_code)
);
CREATE TABLE IF NOT EXISTS fund_holdings (
  fund_code    TEXT NOT NULL,
  hold_type    TEXT NOT NULL,   -- 'FUND' | 'STOCK'
  target_code  TEXT NOT NULL,
  target_name  TEXT,
  weight_pct   REAL,
  ratio_real   INTEGER DEFAULT 0,  -- 1=真实季报比例, 0=估算(合同下限/配置默认)
  quarter      TEXT,
  report_date  TEXT,
  fetched_ts   REAL,
  PRIMARY KEY (fund_code, hold_type, target_code)
);
CREATE INDEX IF NOT EXISTS idx_nav_code ON nav_history(fund_code);
CREATE INDEX IF NOT EXISTS idx_fh_code ON fund_holdings(fund_code);
CREATE INDEX IF NOT EXISTS idx_snap_code ON snapshots(fund_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_corr_code ON corrections(fund_code);
CREATE TABLE IF NOT EXISTS app_kv (
  k TEXT PRIMARY KEY,
  v TEXT,
  updated_at TEXT
);
"""

def _ensure_writable(path):
    """Clear a Windows read-only attribute if present.

    The real-world cause of sqlite3 'attempt to write a readonly database' here
    was NOT the main .db file, but the WAL auxiliary files (-wal / -shm) or the
    containing directory picking up the FILE_ATTRIBUTE_READONLY bit — OneDrive,
    antivirus, sync-backup and indexers do this routinely to files they touch.
    When -wal is read-only, any WAL write silently falls back to a read-only
    open and every INSERT fails with that error. os.access(..., W_OK) reflects
    the bit and os.chmod() clears it, so we call this on the db, both WAL aux
    files and the directory (see _conn). With journal_mode=DELETE there are no
    -wal/-shm files at all, but the clearing is kept as a safety net.
    """
    if not os.path.exists(path):
        return
    try:
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    except OSError:
        pass  # genuine permission/FS issue — let the later write surface it

def _conn():
    d = os.path.dirname(DB_PATH)
    os.makedirs(d, exist_ok=True)
    # 清除只读位: 主库文件 + WAL 辅助文件 + 目录
    # (WAL 的 -wal/-shm 被外部工具加只读位是 readonly 报错的根因, 必须一并清除)
    _ensure_writable(DB_PATH)
    _ensure_writable(DB_PATH + "-wal")
    _ensure_writable(DB_PATH + "-shm")
    _ensure_writable(d)
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # 放弃 WAL, 改用默认回滚日志(DELETE): 不再生成 -wal/-shm 共享内存文件,
        # 彻底消除 Windows 上辅助文件被加只读位导致的写入失败(本应用写入少且串行性, 无需 WAL 并发)。
        # 已在 WAL 模式的库会在首次连接时自动 checkpoint 并切换; 切回 DELETE 前已清过只读位, 可正常写入。
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        c.close()
        raise
    return c

def _retry(fn):
    """写操作自动重试装饰器: 瞬态 readonly/locked 时清只读位+等待后重试(自愈外部工具/反病毒瞬时触碰)"""
    def wrapper(*args, **kw):
        for attempt in range(4):
            try:
                return fn(*args, **kw)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("readonly" in msg or "locked" in msg) and attempt < 3:
                    _ensure_writable(DB_PATH)
                    _ensure_writable(os.path.dirname(DB_PATH))
                    time.sleep(1.0 + attempt * 0.8)
                    continue
                raise
    return wrapper

@_retry
def init_db():
    c = _conn()
    try:
        c.executescript(SCHEMA)
        c.commit()
    finally:
        c.close()

@_retry
def upsert_nav(rows):
    """rows: [{trade_date, fund_code, dwjz, ljjz, official_chg_pct, source}]"""
    if not rows:
        return 0
    c = _conn()
    n = 0
    try:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            c.execute(
                """INSERT INTO nav_history(trade_date,fund_code,dwjz,ljjz,official_chg_pct,source,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date,fund_code)
                   DO UPDATE SET dwjz=excluded.dwjz, ljjz=excluded.ljjz,
                                 official_chg_pct=excluded.official_chg_pct,
                                 source=excluded.source, updated_at=excluded.updated_at""",
                (r["trade_date"], r["fund_code"], r.get("dwjz"), r.get("ljjz"),
                 r.get("official_chg_pct"), r.get("source", ""), now))
            n += 1
        c.commit()
    finally:
        c.close()
    return n

@_retry
def add_snapshot(s):
    """s: dict with keys ts, fund_code, trade_date, model_chg_pct, adjusted_chg_pct,
       official_live_chg_pct, live_price, baseline_date, baseline_nav, market_status, quote_time"""
    c = _conn()
    try:
        c.execute(
            """INSERT OR REPLACE INTO snapshots(ts,fund_code,trade_date,model_chg_pct,adjusted_chg_pct,
               official_live_chg_pct,live_price,baseline_date,baseline_nav,market_status,quote_time)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (s["ts"], s["fund_code"], s.get("trade_date"), s.get("model_chg_pct"),
             s.get("adjusted_chg_pct"), s.get("official_live_chg_pct"), s.get("live_price"),
             s.get("baseline_date"), s.get("baseline_nav"), s.get("market_status"), s.get("quote_time")))
        c.commit()
    finally:
        c.close()

def _trade_days_with_snapshots():
    c = _conn()
    try:
        cur = c.execute("SELECT DISTINCT trade_date, fund_code FROM snapshots WHERE trade_date IS NOT NULL")
        return cur.fetchall()
    finally:
        c.close()

def _has_official_nav(trade_date, fund_code):
    c = _conn()
    try:
        cur = c.execute("SELECT 1 FROM nav_history WHERE trade_date=? AND fund_code=?", (trade_date, fund_code))
        return cur.fetchone() is not None
    finally:
        c.close()

def _exists_correction(trade_date, fund_code):
    c = _conn()
    try:
        cur = c.execute("SELECT 1 FROM corrections WHERE trade_date=? AND fund_code=?", (trade_date, fund_code))
        return cur.fetchone() is not None
    finally:
        c.close()

def _snapshot_stats(trade_date, fund_code):
    """统计该日快照: 预估值回退链 model_chg_pct -> adjusted_chg_pct -> official_live_chg_pct
    (联接基金等无披露持仓模型的基金, 用当日实时通道涨跌作为'预估'参与修正)。"""
    c = _conn()
    try:
        cur = c.execute(
            """SELECT COUNT(*),
                      MIN(COALESCE(model_chg_pct, adjusted_chg_pct, official_live_chg_pct)),
                      MAX(COALESCE(model_chg_pct, adjusted_chg_pct, official_live_chg_pct))
               FROM snapshots WHERE trade_date=? AND fund_code=?
               AND COALESCE(model_chg_pct, adjusted_chg_pct, official_live_chg_pct) IS NOT NULL""",
            (trade_date, fund_code))
        cnt, mn, mx = cur.fetchone()
        cur2 = c.execute(
            """SELECT COALESCE(model_chg_pct, adjusted_chg_pct, official_live_chg_pct), quote_time
               FROM snapshots WHERE trade_date=? AND fund_code=?
               AND COALESCE(model_chg_pct, adjusted_chg_pct, official_live_chg_pct) IS NOT NULL
               ORDER BY ts DESC LIMIT 1""", (trade_date, fund_code))
        last = cur2.fetchone()
        return cnt, mn, mx, last
    finally:
        c.close()

def _official_nav(trade_date, fund_code):
    c = _conn()
    try:
        cur = c.execute("SELECT dwjz, official_chg_pct FROM nav_history WHERE trade_date=? AND fund_code=?",
                        (trade_date, fund_code))
        return cur.fetchone()
    finally:
        c.close()

@_retry
def generate_corrections(enabled_codes=None):
    """
    延迟补生成修正记录:
    对每个"有快照的交易日+基金", 若当日官方净值已入库且尚无修正记录 -> 生成。
    enabled_codes: 仅这些基金参与修正(预估修正开关开启的); 为 None 时全部参与(向后兼容)。
    返回 (本次新生成记录数, 清理的快照行数)。
    """
    init_db()
    made = 0
    for trade_date, fund_code in _trade_days_with_snapshots():
        if enabled_codes is not None and fund_code not in enabled_codes:
            continue  # 预估修正未开启: 跳过(也不显示)
        if _exists_correction(trade_date, fund_code):
            continue
        if not _has_official_nav(trade_date, fund_code):
            continue  # 当日官方净值未发布, 等待下次运行补生成
        nav = _official_nav(trade_date, fund_code)
        cnt, mn, mx, last = _snapshot_stats(trade_date, fund_code)
        if cnt is None or cnt == 0:
            continue
        last_model = last[0] if last else None
        bias = None
        if last_model is not None and nav[1] is not None:
            bias = round(last_model - nav[1], 3)
        c = _conn()
        try:
            c.execute(
                """INSERT OR REPLACE INTO corrections(trade_date,fund_code,official_dwjz,official_chg_pct,
                   last_model_chg_pct,min_model_chg_pct,max_model_chg_pct,snap_count,bias_pct,corrected_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (trade_date, fund_code, nav[0], nav[1], last_model, mn, mx, cnt, bias,
                 datetime.datetime.now().isoformat(timespec="seconds")))
            c.commit()
            made += 1
        finally:
            c.close()
    # 修正生成后清理快照(已被 corrections 汇总); 删除后回收空间
    purged = purge_old_snapshots(enabled_codes=enabled_codes)
    return made, purged


@_retry
def delete_corrections_for_fund(code):
    """删除某基金的全部修正记录(关闭预估修正时调用)"""
    init_db()
    c = _conn()
    try:
        n = c.execute("DELETE FROM corrections WHERE fund_code=?", (code,)).rowcount
        c.commit()
        return n
    finally:
        c.close()


@_retry
def delete_snapshots_for_fund(code):
    """删除某基金的全部原始快照(关闭预估修正时调用, 该基金不再需要快照)"""
    init_db()
    c = _conn()
    try:
        n = c.execute("DELETE FROM snapshots WHERE fund_code=?", (code,)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def purge_old_snapshots(enabled_codes=None, days=7):
    """
    清理 snapshots 表, 防止一天多次快照无限膨胀:
      - 规则1: 已生成修正记录(被汇总)的 (交易日,基金) 快照全部删除(无论是否今日)
      - 规则2: 超过 days 天的快照一律删除(兜底, 处理 hidden/未开启修正基金的堆积)
    仅在发生过删除时执行 VACUUM 回收 SQLite 文件空间。
    返回删除的快照行数。
    """
    init_db()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    deleted = 0
    c = _conn()
    try:
        # 规则1: 有修正记录的 (交易日,基金) -> 快照已冗余
        cur = c.execute("SELECT DISTINCT trade_date, fund_code FROM corrections")
        for td, fc in cur.fetchall():
            if enabled_codes is not None and fc not in enabled_codes:
                continue  # 未开启修正的基金, 其快照由规则2(7天)清理, 这里不动
            deleted += c.execute("DELETE FROM snapshots WHERE trade_date=? AND fund_code=?", (td, fc)).rowcount
        # 规则2: 7 天兜底
        cur2 = c.execute("SELECT DISTINCT trade_date, fund_code FROM snapshots WHERE trade_date < ?", (cutoff,))
        for td, fc in cur2.fetchall():
            deleted += c.execute("DELETE FROM snapshots WHERE trade_date=? AND fund_code=?", (td, fc)).rowcount
        c.commit()
    finally:
        c.close()
    if deleted > 0:
        vacuum_db()
    return deleted


@_retry
def vacuum_db():
    """回收 SQLite 删除后的空闲空间(DELETE 不会自动缩小文件, 必须 VACUUM)。
    使用 autocommit 隔离级别, 避免 VACUUM 处于事务内报错。"""
    c = _conn()
    try:
        c.isolation_level = None
        c.execute("VACUUM")
    finally:
        c.close()

def query(table, limit=None, order="ASC"):
    init_db()
    c = _conn()
    try:
        cols = {"nav_history": ("trade_date",), "snapshots": ("ts",), "corrections": ("trade_date",)}[table]
        sql = f"SELECT * FROM {table} ORDER BY {cols[0]} {order}"
        if limit:
            sql += f" LIMIT {limit}"
        cur = c.execute(sql)
        names = [d[0] for d in cur.description]
        return names, cur.fetchall()
    finally:
        c.close()

def stats():
    init_db()
    c = _conn()
    try:
        out = {}
        for t in ("nav_history", "snapshots", "corrections"):
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        out["db"] = DB_PATH
        return out
    finally:
        c.close()

def get_nav_range(code):
    """该基金净值覆盖情况 -> (min_date, max_date, count); 无记录 (None, None, 0)"""
    init_db()
    c = _conn()
    try:
        return c.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM nav_history WHERE fund_code=?",
                         (code,)).fetchone()
    finally:
        c.close()

def get_nav_series(code):
    """该基金全量净值序列(升序): [(trade_date, dwjz, official_chg_pct), ...]"""
    init_db()
    c = _conn()
    try:
        return c.execute("SELECT trade_date, dwjz, official_chg_pct FROM nav_history WHERE fund_code=? ORDER BY trade_date ASC",
                         (code,)).fetchall()
    finally:
        c.close()

# ---------------------------------------------------------------- 基金持仓关系表(fund_holdings)
@_retry
def replace_fund_holdings(code, rows):
    """覆盖某基金的直接持仓(基金/股票)。rows: [(hold_type, target_code, target_name, weight_pct, ratio_real, quarter, report_date)]"""
    init_db()
    c = _conn()
    try:
        c.execute("DELETE FROM fund_holdings WHERE fund_code=?", (code,))
        ts = datetime.datetime.now().timestamp()
        for r in rows:
            c.execute("INSERT OR REPLACE INTO fund_holdings(fund_code, hold_type, target_code, target_name, weight_pct, ratio_real, quarter, report_date, fetched_ts) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (code, r[0], r[1], r[2], r[3], r[4], r[5], r[6], ts))
        c.commit()
    finally:
        c.close()

def get_fund_holdings(code):
    """返回该基金直接持仓: [(hold_type, target_code, target_name, weight_pct, ratio_real, quarter, report_date), ...]"""
    init_db()
    c = _conn()
    try:
        return c.execute("SELECT hold_type, target_code, target_name, weight_pct, ratio_real, quarter, report_date "
                          "FROM fund_holdings WHERE fund_code=?", (code,)).fetchall()
    finally:
        c.close()

def get_all_fund_holdings():
    """全量直接持仓, 返回 {fund_code: [rowdict, ...]}"""
    init_db()
    c = _conn()
    try:
        rows = c.execute("SELECT fund_code, hold_type, target_code, target_name, weight_pct, ratio_real, quarter, report_date FROM fund_holdings").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r[0], []).append({
                "hold_type": r[1], "target_code": r[2], "target_name": r[3],
                "weight_pct": r[4], "ratio_real": r[5], "quarter": r[6], "report_date": r[7]})
        return out
    finally:
        c.close()

# ---------------------------------------------------------------- 通用 KV 存储(app_kv)
# 统一管理"原 JSON 文件"型数据: key = 文件名(如 funds.json / trades.json / latest.json /
# baseline_2026-08-14.json / holdings_516670.json / sync_state.json / tag_defs.json),
# value = JSON 文本。读写语义与原文件完全一致, 迁移后 JSON 文件不再作为数据源(仅保留备份)。
KV_FILE_KEYS = ("funds.json", "trades.json", "tag_defs.json", "sync_state.json", "latest.json")

def kv_get(key, default=None):
    """读取 KV: 返回解析后的对象; 不存在返回 default"""
    init_db()
    c = _conn()
    try:
        r = c.execute("SELECT v FROM app_kv WHERE k=?", (key,)).fetchone()
        if r is None or r[0] is None:
            return default
        try:
            return json.loads(r[0])
        except Exception:
            return r[0]
    finally:
        c.close()

@_retry
def kv_set(key, value):
    """写入 KV: value 为 dict/list/str 等, 自动 json 序列化"""
    init_db()
    c = _conn()
    try:
        v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        c.execute("INSERT OR REPLACE INTO app_kv(k, v, updated_at) VALUES(?,?,?)",
                  (key, v, datetime.datetime.now().isoformat(timespec="seconds")))
        c.commit()
    finally:
        c.close()

@_retry
def kv_delete(key):
    init_db()
    c = _conn()
    try:
        c.execute("DELETE FROM app_kv WHERE k=?", (key,))
        c.commit()
    finally:
        c.close()

def kv_has(key):
    init_db()
    c = _conn()
    try:
        return c.execute("SELECT 1 FROM app_kv WHERE k=?", (key,)).fetchone() is not None
    finally:
        c.close()

def purge_fund(code):
    """彻底删除某基金的全部数据(不可恢复):
       基础配置(funds.json) + 买卖记录(trades.json) + 历史净值(nav_history)
       + 持仓关系(fund_holdings) + 快照(snapshots) + 修正(corrections)。
       后期重新 add 该基金时, 引擎会走全新加载流程(重新抓取净值/持仓关系)。"""
    code = str(code or "").strip()
    if not code:
        return 0
    # 1) 配置(funds.json): 移除该基金条目(含 hidden 标记)
    cfg = kv_get("funds.json") or {}
    if isinstance(cfg, dict) and isinstance(cfg.get("funds"), dict):
        if cfg["funds"].pop(code, None) is not None:
            kv_set("funds.json", cfg)
    # 2) 买卖记录(trades.json): 删除该基金所有记录
    t = kv_get("trades.json") or {"trades": []}
    if isinstance(t, dict) and isinstance(t.get("trades"), list):
        before = len(t["trades"])
        t["trades"] = [x for x in t["trades"] if str((x or {}).get("code", "")) != code]
        if len(t["trades"]) != before:
            kv_set("trades.json", t)
    # 3) 数据库表: 按 fund_code 删除(历史净值/持仓/快照/修正)
    init_db()
    c = _conn()
    deleted = 0
    try:
        for tbl in ("nav_history", "fund_holdings", "snapshots", "corrections"):
            try:
                cur = c.execute("DELETE FROM %s WHERE fund_code=?" % tbl, (code,))
                deleted += cur.rowcount or 0
            except Exception:
                pass
        c.commit()
    finally:
        c.close()
    return deleted

# ---------------------------------------------------------------- JSON 文件迁移入库
# 首次运行(或 KV 中无该文件数据)时, 若磁盘上存在同名 JSON 文件则自动导入, 实现平滑迁移。
# 迁移只读不删: 原 JSON 文件保留作为备份, 之后所有读写都走数据库。
def migrate_json_files(force=False):
    """把磁盘 JSON 数据导入 app_kv。返回导入的文件名列表。
    - 数据文件(funds/trades/tag_defs/sync_state): 自动迁移
    - 引擎产物(baseline_*/holdings_*/latest): 亦迁移, 保证"全量入库"目标
    """
    imported = []
    data_dir = os.path.join(BASE, "data")
    cfg_dir = os.path.join(BASE, "config")
    candidates = [
        ("funds.json", cfg_dir), ("trades.json", cfg_dir), ("tag_defs.json", cfg_dir),
        ("sync_state.json", data_dir),
    ]
    # 动态产物文件
    if os.path.isdir(data_dir):
        for fn in sorted(os.listdir(data_dir)):
            if fn.startswith("baseline_") and fn.endswith(".json"):
                candidates.append((fn, data_dir))
            elif fn.startswith("holdings_") and fn.endswith(".json"):
                candidates.append((fn, data_dir))
            elif fn == "latest.json":
                candidates.append((fn, data_dir))
    for fn, d in candidates:
        if not force and kv_has(fn):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            kv_set(fn, data)
            imported.append(fn)
        except Exception as e:
            print(f"  [warn] 迁移 {fn} 失败: {e}")
    return imported

if __name__ == "__main__":
    init_db()
    print("数据库:", DB_PATH)
    print("统计:", stats())
