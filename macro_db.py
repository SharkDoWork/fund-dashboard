"""macro_db.py — 宏观月度指标(CPI/PPI)数据层。

物理隔离: 独立数据库 data/macro.db, 独立表:
  - cpi_monthly  全国 CPI 月度(同比/环比/指数/累计, 含城市/农村同比)
  - ppi_monthly  PPI 月度(同比/指数/累计)
  - app_kv       页面自动刷新记账(与 pigcycle 模式一致, key='macro_refresh_date')
period_date 统一 'YYYY-MM'。
"""
import datetime
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "macro.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cpi_monthly (
  period_date TEXT NOT NULL PRIMARY KEY,   -- YYYY-MM
  yoy         REAL,                        -- 全国当月同比 %
  mom         REAL,                        -- 全国环比 %
  base_index  REAL,                        -- 全国指数(上年同月=100)
  cum_index   REAL,                        -- 全国累计指数
  city_yoy    REAL,                        -- 城市同比 %
  rural_yoy   REAL,                        -- 农村同比 %
  note        TEXT DEFAULT '',
  source      TEXT DEFAULT '',
  source_url  TEXT DEFAULT '',
  updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS ppi_monthly (
  period_date TEXT NOT NULL PRIMARY KEY,   -- YYYY-MM
  yoy         REAL,                        -- 当月同比 %
  mom         REAL,                        -- 全国环比 %(统计局官方月度发布稿口径)
  base_index  REAL,                        -- 出厂价格指数(上年同月=100)
  cum_index   REAL,                        -- 累计指数
  note        TEXT DEFAULT '',
  source      TEXT DEFAULT '',
  source_url  TEXT DEFAULT '',
  updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS app_kv (
  k TEXT PRIMARY KEY,
  v TEXT,
  updated_at TEXT
);
"""


def _ensure_writable(path):
    if not os.path.exists(path):
        return
    try:
        if not os.access(path, os.W_OK):
            os.chmod(path, 0o600)
    except OSError:
        pass


def _conn():
    d = os.path.dirname(DB_PATH)
    os.makedirs(d, exist_ok=True)
    _ensure_writable(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _migrate(con):
    """旧库迁移: 为 ppi_monthly 补 mom 列(官方环比, 2026-09 起加入)。"""
    cols = [r[1] for r in con.execute("PRAGMA table_info(ppi_monthly)").fetchall()]
    if "mom" not in cols:
        con.execute("ALTER TABLE ppi_monthly ADD COLUMN mom REAL")


def init_db():
    con = _conn()
    try:
        con.executescript(_SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def upsert_cpi(row, source="", source_url=""):
    con = _conn()
    try:
        con.execute(
            """INSERT INTO cpi_monthly (period_date, yoy, mom, base_index, cum_index,
                 city_yoy, rural_yoy, note, source, source_url, updated_at)
               VALUES (?,?,?,?,?,?,?,'',?,?,?)
               ON CONFLICT(period_date) DO UPDATE SET
                 yoy=excluded.yoy, mom=excluded.mom, base_index=excluded.base_index,
                 cum_index=excluded.cum_index, city_yoy=excluded.city_yoy,
                 rural_yoy=excluded.rural_yoy,
                 source=excluded.source, source_url=excluded.source_url,
                 updated_at=excluded.updated_at""",
            (row["period"], row.get("yoy"), row.get("mom"), row.get("base_index"),
             row.get("cum_index"), row.get("city_yoy"), row.get("rural_yoy"),
             source, source_url, _now()))
        con.commit()
    finally:
        con.close()


def upsert_ppi(row, source="", source_url=""):
    con = _conn()
    try:
        con.execute(
            """INSERT INTO ppi_monthly (period_date, yoy, mom, base_index, cum_index,
                 note, source, source_url, updated_at)
               VALUES (?,?,?,?,?,'',?,?,?)
               ON CONFLICT(period_date) DO UPDATE SET
                 yoy=excluded.yoy, mom=COALESCE(excluded.mom, ppi_monthly.mom),
                 base_index=COALESCE(excluded.base_index, ppi_monthly.base_index),
                 cum_index=COALESCE(excluded.cum_index, ppi_monthly.cum_index),
                 source=excluded.source, source_url=excluded.source_url,
                 updated_at=excluded.updated_at""",
            (row["period"], row.get("yoy"), row.get("mom"), row.get("base_index"),
             row.get("cum_index"), source, source_url, _now()))
        con.commit()
    finally:
        con.close()


def get_series(kind):
    """返回升序行列表(dict)。kind in ('cpi','ppi')。"""
    con = _conn()
    try:
        table = "cpi_monthly" if kind == "cpi" else "ppi_monthly"
        rows = con.execute("SELECT * FROM %s ORDER BY period_date" % table).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def coverage():
    """各表覆盖统计: {cpi: {count,start,end,latest:{...}}, ppi: {...}}"""
    out = {}
    for kind, table in (("cpi", "cpi_monthly"), ("ppi", "ppi_monthly")):
        con = _conn()
        try:
            r = con.execute(
                "SELECT count(*) c, min(period_date) s, max(period_date) e "
                "FROM %s" % table).fetchone()
            latest = con.execute(
                "SELECT * FROM %s WHERE period_date=(SELECT max(period_date) FROM %s)"
                % (table, table)).fetchone()
        finally:
            con.close()
        out[kind] = {"count": r["c"], "start": r["s"], "end": r["e"],
                     "latest": dict(latest) if latest else None}
    return out


def kv_get(key, default=None):
    con = _conn()
    try:
        row = con.execute("SELECT v FROM app_kv WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, ValueError):
            return row["v"]
    finally:
        con.close()


def kv_set(key, value):
    con = _conn()
    try:
        con.execute(
            "INSERT INTO app_kv (k, v, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), _now()))
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    init_db()
    print(coverage())
