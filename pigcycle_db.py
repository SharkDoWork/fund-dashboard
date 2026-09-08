# -*- coding: utf-8 -*-
"""
猪周期数据层 (独立数据库, 与基金看板物理隔离)
============================================
独立 SQLite 库 data/pigcycle.db, 三指标隔离追踪, 各自一张表:
  sow_inventory   能繁母猪存栏量 (万头, 季度绝对量; 已内置 2020Q1–2026Q2 权威历史)
  piglet_price    仔猪价格 / 生猪价格 (元/公斤, 周度, 同源双指标)
  frozen_inventory 冻品库存      (万吨 / 库存指数, 周度或月度)

另有「周期分析指标」两张表(非追踪序列, 而是分析维度的阈值化展示):
  grain_pig_ratio   猪粮比价 (:1, 发改委价格监测中心每周发布, 盈利代理)
  index_pe          中证畜牧指数 PE/PB (倍, 中证指数公司官方发布)

设计要点:
  - 各指标 upsert 键为 (period_date), 互不影响; 删除/导入只影响单指标表。
  - 能繁母猪每行可带 source_title + source_url(官方发布文章), 便于溯源展示。
  - 仔猪/生猪价格来自农业农村部畜牧兽医局周度监测(自动接入), 同源存 piglet + pork。
  - 冻品库存无免费公开源, 仅手动录入 / CSV 导入。
  - 周期分析指标用 ANALYSIS 字典集中管理阈值/利弊/来源, 供前端渲染。
  - 月度能繁(sow_monthly)已移除: 季度绝对量(国新办/统计局)权威且已内置多年历史, 月度口径无干净免费源、需手动/商业源, 价值有限。
"""
import json, os, sqlite3, datetime, stat, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "pigcycle.db")

# 三指标定义(供前端下拉/标签与后端校验复用)
INDICATORS = {
    "sow_inventory": {
        "name": "能繁母猪存栏量",
        "unit": "万头",
        "freq": "quarterly",  # 精确存栏量季度末发布, 中间月份为测算值
        "desc": "农业农村部口径, 决定 10 个月后生猪供给, 是猪周期最根本的产能领先指标。精确存栏量每季度末(3/6/9/12月)经国新办/统计局发布。",
    },
    "piglet_price": {
        "name": "仔猪 / 生猪价格",
        "unit": "元/公斤",
        "freq": "weekly",
        "desc": "农业农村部畜牧兽医局周度监测(全国500个县集贸市场): 仔猪价与生猪价, 反映补栏情绪与即期供需。",
        "series": [
            {"key": "piglet", "name": "仔猪", "unit": "元/公斤"},
            {"key": "pork", "name": "生猪", "unit": "元/公斤"},
        ],
    },
    "frozen_inventory": {
        "name": "冻品库存",
        "unit": "万吨",
        "freq": "weekly",
        "desc": "屠宰-冻品环节库存(亦可用库存指数), 高位代表被动累库/需求偏弱。无免费公开源, 需手动录入或 CSV 导入。",
    },
}

# 实时喂价追踪(自动每日拉取): 与三指标/分析卡物理隔离的独立序列
#  - spot_hog_daily          生猪现货日度价(外三元, 元/公斤), 中国养猪网每日发布
#  - slaughter_rate_weekly   周度屠宰开工率(%), 卓创资讯(中国畜牧业协会官网免费转载)
FEEDS = {
    "spot_hog_daily": {
        "name": "生猪现货日度价（外三元）",
        "unit": "元/公斤",
        "freq": "daily",
        "desc": "全国生猪(外三元)现货日度均价, 中国养猪网每日发布。反映即期供需与猪价底部修复, "
                "是'旺季兑现'最灵敏的日度信号; 历史序列可由其走势图接口一次回Fill。",
        "source": "中国养猪网(ajax_zhujia 历史接口, 免费)",
        "source_url": "https://www.zhuwang.com.cn/",
    },
    "slaughter_rate_weekly": {
        "name": "屠宰开工率（周度）",
        "unit": "%",
        "freq": "weekly",
        "desc": "全国重点屠宰企业周内平均开工率(卓创资讯, 中国畜牧业协会官网免费转载)。"
                "开工率上行=需求/走货改善, 是'金九旺季成色'的核心验证指标; 另含日均屠宰量与白条猪肉均价。",
        "source": "卓创资讯 · 中国畜牧业协会(caaa.cn, 免费周评)",
        "source_url": "https://www.caaa.cn/html/fw/market/zhu/",
        "series": [
            {"key": "value", "name": "开工率", "unit": "%"},
            {"key": "kill_volume", "name": "日均屠宰量", "unit": "万头"},
            {"key": "pork_white", "name": "白条猪肉均价", "unit": "元/公斤"},
        ],
    },
    "hog_monthly_moa": {
        "name": "生猪价格（月度·农业农村部）",
        "unit": "元/公斤",
        "freq": "monthly",
        "desc": "农业农村部《生猪专题》月度权威数据: 全国生猪出场价、仔猪价、白条猪价及定点屠宰量。"
                "覆盖近 3 年以上, 是与日度/周度信号互补的'长周期'基准(免费、权威、结构化)。",
        "source": "农业农村部·生猪专题(moa.gov.cn, 免费)",
        "source_url": "https://www.moa.gov.cn/ztzl/szcpxx/jdsj/",
        "series": [
            {"key": "value", "name": "生猪出场价", "unit": "元/公斤"},
            {"key": "piglet", "name": "仔猪价", "unit": "元/公斤"},
            {"key": "pork_white", "name": "白条猪价", "unit": "元/公斤"},
            {"key": "kill_volume", "name": "定点屠宰量", "unit": "万头"},
        ],
    },
}

# 周期分析指标定义(阈值 / 利弊 / 官方来源集中管理, 供前端渲染与后端校验)
# zone 配色遵循中国习惯: 涨/利好偏绿(此处去产能=利好未来供给, 故下降偏绿), 风险偏红。
ANALYSIS = {
    "capacity": {
        "name": "产能环比（能繁母猪）",
        "unit": "万头 / %",
        "freq": "quarterly",
        "desc": "最根本的产能领先指标, 决定 10 个月后生猪供给。季度末绝对量由国新办/统计局发布(滞后约 1 个月), "
                "已内置 2020 年以来权威季度历史(统计局口径)用于观察完整猪周期。下方给出「季度环比」与相对正常保有量的分区。",
        "保有量": 3900,  # 2024 修订《生猪产能调控实施方案》正常保有量(万头)
        # 存栏量相对正常保有量(3900万头)的分区(绿/黄/红), 据 2024 修订方案: 92%~105% 绿, 85%~92%/105%~110% 黄, <85%/>110% 红
        "zones": [
            {"min": 0,     "max": 3315, "label": "过低(供应风险, <85%)", "color": "#d8504a"},
            {"min": 3315,  "max": 3588, "label": "黄色·偏低(85%~92%)", "color": "#e3a008"},
            {"min": 3588,  "max": 4095, "label": "绿色·合理(92%~105%)", "color": "#2faa6a"},
            {"min": 4095,  "max": 4290, "label": "黄色·偏高(105%~110%)", "color": "#e3a008"},
            {"min": 4290,  "max": 99999, "label": "红色·严重过剩(>110%)", "color": "#d8504a"},
        ],
        # 环比变化的方向含义: 下降=去产能(利好未来供给, 偏绿); 上升=累积(偏红)
        "利弊": {
            "利": [
                "最根本的产能领先指标, 决定 10 个月后生猪供给, 逻辑最硬;",
                "季度绝对量由国新办/统计局发布, 权威、口径统一;",
                "已内置 2020 年以来 26 个季度权威历史, 可直接观察上一轮猪周期(2021 峰值→2024 谷底→2025-26 恢复)。",
            ],
            "弊": [
                "季度绝对量滞后严重: 季末 + 约 1 个月才公布(如 2026Q2 于 7-24 才出);",
                "行业 PSY/繁育效率持续提升, 同样头数产出更多猪, '头数'对真实供给的代表性下降;",
                "正常保有量曾由 4100(2021)调至 3900(2024 修订), 历史数据跨口径比较需注意。",
            ],
        },
        "来源": "国新办/统计局(季度末绝对量) · 历史序列: 建信期货研究中心整理(统计局口径)2020Q1–2026Q2",
    },
    "grain_pig_ratio": {
        "name": "猪粮比价",
        "unit": ":1",
        "freq": "weekly",
        "desc": "全国生猪出场价 ÷ 全国主要批发市场二等玉米均价(发改委价格监测中心每周发布)。"
                "是判断养殖盈亏最直接的官方实时代理, 也是收储/放储预警的核心指标。",
        # 阈值带(发改委《完善政府猪肉储备调节机制预案》2021)
        "zones": [
            {"min": 0,   "max": 5,    "label": "一级预警·深度亏损(<5:1)", "color": "#d8504a"},
            {"min": 5,   "max": 6,    "label": "二级预警(5:1~6:1)", "color": "#e0822a"},
            {"min": 6,   "max": 7,    "label": "临界/微利(6:1~7:1)", "color": "#e3a008"},
            {"min": 7,   "max": 9,    "label": "盈利线以上(≥7:1)", "color": "#2faa6a"},
            {"min": 9,   "max": 12,   "label": "上涨三级预警(9:1~12:1)", "color": "#e3a008"},
            {"min": 12,  "max": 999,  "label": "上涨一级预警(>12:1)", "color": "#d8504a"},
        ],
        "利弊": {
            "利": [
                "国家发改委价格监测中心每周发布, 高频、实时、官方;",
                "直接反映养殖盈亏对比(猪价 vs 饲料成本), 比'头均盈利'更易得;",
                "收储/放储预警的官方核心指标, 信号意义强、市场认可度高。",
            ],
            "弊": [
                "是'比价'非绝对头均盈利, 受玉米价扰动(玉米涨会压低比值但不一定真亏);",
                "不区分企业成本差异(龙头成本远低于行业均值);",
                "全国平均口径较粗, 区域/规模分化被抹平。",
            ],
        },
        "来源": "国家发改委价格监测中心(每周) · 提示: 无免费编程接口, 建议每周读取官方数值后手动录入",
    },
    "index_pe": {
        "name": "中证畜牧指数 PE",
        "unit": "倍",
        "freq": "daily/weekly",
        "desc": "中证畜牧养殖指数(930707)市盈率(TTM)与市净率(PB)。官方由中证指数公司发布, "
                "一眼看板块估值位置。注意: 强周期行业 PE 严重失真, 须结合 PB 与产能/猪粮比价反向解读。",
        # 周期股 PE 不能机械看高低: 底部利润薄/亏损→PE虚高; 顶部利润暴增→PE虚低
        "zones": [
            {"min": 0,   "max": 0,    "label": "亏损(PE为负/无意义)", "color": "#d8504a"},
            {"min": 0,   "max": 20,   "label": "偏低(多在周期顶部, 警惕追高)", "color": "#e0822a"},
            {"min": 20,  "max": 40,   "label": "中等", "color": "#e3a008"},
            {"min": 40,  "max": 999,  "label": "偏高(多在周期底部, 非卖点)", "color": "#2faa6a"},
        ],
        "利弊": {
            "利": [
                "中证指数公司官方稳定发布指数 PE/PB, 口径统一、可追溯;",
                "一眼看板块估值相对位置, 配合历史分位判断贵贱;",
                "PE+PB 双指标比单看 PE 更稳(周期底部 PB 更能反映资产低估)。",
            ],
            "弊": [
                "强周期行业 PE 严重失真: 周期底部利润薄/亏损→PE虚高甚至为负, 易被误判'贵';",
                "周期顶部利润暴增→PE虚低, 易被误判'便宜'而追高;",
                "单一 PE 会反向误导买卖, 须配合产能(去化)、盈利(猪粮比价)、PB 共同判断。",
            ],
        },
        "来源": "中证指数公司官网(csindex.com.cn) · 东财/同花顺免费可得 · 指数代码 930707",
    },
}

# 分析指标 -> 数据表 映射
_ANALYSIS_TABLE = {
    "grain_pig_ratio": "grain_pig_ratio",
    "index_pe": "index_pe",
    # capacity 特殊: 由 sow_inventory(季度) 直接计算, 无独立表
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sow_inventory (
  period_date   TEXT NOT NULL PRIMARY KEY,   -- YYYY-MM-DD
  value         REAL NOT NULL,
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_title  TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS piglet_price (
  period_date   TEXT NOT NULL PRIMARY KEY,
  piglet        REAL,                         -- 仔猪 元/公斤
  pork          REAL,                         -- 生猪 元/公斤
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS frozen_inventory (
  period_date   TEXT NOT NULL PRIMARY KEY,
  value         REAL NOT NULL,
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS grain_pig_ratio (
  period_date   TEXT NOT NULL PRIMARY KEY,    -- YYYY-MM-DD (周)
  value         REAL NOT NULL,                -- 猪粮比价 :1
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS index_pe (
  period_date   TEXT NOT NULL PRIMARY KEY,    -- YYYY-MM-DD
  pe            REAL,                         -- 市盈率 TTM 倍
  pb            REAL,                         -- 市净率 倍
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS app_kv (
  k TEXT PRIMARY KEY,
  v TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS spot_hog_daily (
  period_date   TEXT NOT NULL PRIMARY KEY,
  value         REAL NOT NULL,                -- 生猪(外三元)现货日度价 元/公斤
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS slaughter_rate_weekly (
  period_date   TEXT NOT NULL PRIMARY KEY,    -- 周度(取卓创周评覆盖的周截止日 YYYY-MM-DD)
  value         REAL NOT NULL,                -- 周内平均开工率 %
  kill_volume   REAL,                         -- 周内日均屠宰量 万头
  pork_white    REAL,                         -- 白条猪肉均价 元/公斤
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS hog_monthly_moa (
  period_date   TEXT NOT NULL PRIMARY KEY,    -- 月度 YYYY-MM(如 2026-07)
  value         REAL NOT NULL,                -- 全国生猪出场价格 元/公斤
  piglet        REAL,                         -- 全国仔猪价格 元/公斤
  pork_white    REAL,                         -- 全国批发市场白条猪价格 元/公斤
  kill_volume   REAL,                         -- 定点屠宰企业屠宰量 万头
  note          TEXT DEFAULT '',
  source        TEXT DEFAULT '',
  source_url    TEXT DEFAULT '',
  updated_at    TEXT
);
"""

_TABLE_MAP = {
    "sow_inventory": "sow_inventory",
    "piglet_price": "piglet_price",
    "frozen_inventory": "frozen_inventory",
}


def _ensure_writable(path):
    if not os.path.exists(path):
        return
    try:
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    except OSError:
        pass


def _conn():
    d = os.path.dirname(DB_PATH)
    os.makedirs(d, exist_ok=True)
    _ensure_writable(DB_PATH)
    _ensure_writable(DB_PATH + "-wal")
    _ensure_writable(DB_PATH + "-shm")
    _ensure_writable(d)
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        c.close()
        raise
    return c


def _retry(fn):
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


def _has_col(c, table, col):
    try:
        rows = c.execute("PRAGMA table_info(%s)" % table).fetchall()
        return any(r[1] == col for r in rows)
    except Exception:
        return False


def _migrate(c):
    """兼容旧库: 加列 / 重建 piglet_price / 丢弃已移除的信号表 / 清理 kv。"""
    for col in ("source_title", "source_url"):
        if not _has_col(c, "sow_inventory", col):
            try:
                c.execute("ALTER TABLE sow_inventory ADD COLUMN %s TEXT DEFAULT ''" % col)
            except Exception:
                pass
    if _has_col(c, "piglet_price", "value"):
        try:
            c.execute(
                """CREATE TABLE IF NOT EXISTS _piglet_new (
                    period_date TEXT PRIMARY KEY, piglet REAL, pork REAL,
                    note TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '', updated_at TEXT)"""
            )
            c.execute(
                """INSERT OR IGNORE INTO _piglet_new(period_date, piglet, note, source, updated_at)
                   SELECT period_date, value, note, source, updated_at FROM piglet_price"""
            )
            c.execute("DROP TABLE IF EXISTS piglet_price")
            c.execute("ALTER TABLE _piglet_new RENAME TO piglet_price")
        except Exception:
            pass
    for t in ("per_head_profit", "stock_pe"):
        try:
            c.execute("DROP TABLE IF EXISTS %s" % t)
        except Exception:
            pass
    try:
        c.execute("DELETE FROM app_kv WHERE k='takprofit_config'")
    except Exception:
        pass


@_retry
def init_db():
    c = _conn()
    try:
        c.executescript(SCHEMA)
        _migrate(c)
        c.commit()
    finally:
        c.close()


def _f(v):
    if v is None or v == "":
        raise ValueError("数值不能为空")
    return float(v)


@_retry
def upsert_point(indicator, period_date, value=None, piglet=None, pork=None,
                 note="", source="", source_title="", source_url=""):
    """新增/更新单条指标数据; 指标隔离: 只写对应表。按指标不同字段生效。"""
    table = _TABLE_MAP.get(indicator)
    if not table:
        raise ValueError("未知指标: %s" % indicator)
    period_date = str(period_date or "").strip()
    if not period_date:
        raise ValueError("period_date 不能为空")
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        if indicator == "sow_inventory":
            v = _f(value)
            c.execute(
                """INSERT INTO sow_inventory(period_date, value, note, source, source_title, source_url, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, note=excluded.note, source=excluded.source,
                     source_title=excluded.source_title, source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, v, note or "", source or "", source_title or "", source_url or "", now))
        elif indicator == "piglet_price":
            pl = _f(piglet)
            pk = _f(pork)
            c.execute(
                """INSERT INTO piglet_price(period_date, piglet, pork, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     piglet=excluded.piglet, pork=excluded.pork,
                     note=excluded.note, source=excluded.source,
                     source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, pl, pk, note or "", source or "", source_url or "", now))
        else:  # frozen_inventory
            v = _f(value)
            c.execute(
                """INSERT INTO frozen_inventory(period_date, value, note, source, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, note=excluded.note, source=excluded.source, updated_at=excluded.updated_at""",
                (period_date, v, note or "", source or "", now))
        c.commit()
        return True
    finally:
        c.close()


@_retry
def delete_point(indicator, period_date):
    table = _TABLE_MAP.get(indicator)
    if not table:
        raise ValueError("未知指标: %s" % indicator)
    init_db()
    c = _conn()
    try:
        n = c.execute("DELETE FROM %s WHERE period_date=?" % table, (str(period_date),)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def get_series(indicator, ascending=True):
    table = _TABLE_MAP.get(indicator)
    if not table:
        return []
    init_db()
    c = _conn()
    try:
        order = "ASC" if ascending else "DESC"
        if indicator == "sow_inventory":
            rows = c.execute(
                "SELECT period_date, value, note, source, source_title, source_url, updated_at "
                "FROM sow_inventory ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "note": r[2], "source": r[3],
                     "source_title": r[4], "source_url": r[5], "updated_at": r[6]} for r in rows]
        elif indicator == "piglet_price":
            rows = c.execute(
                "SELECT period_date, piglet, pork, note, source, source_url, updated_at "
                "FROM piglet_price ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "piglet": r[1], "pork": r[2], "note": r[3],
                     "source": r[4], "source_url": r[5], "updated_at": r[6]} for r in rows]
        else:
            rows = c.execute(
                "SELECT period_date, value, note, source, updated_at "
                "FROM frozen_inventory ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "note": r[2], "source": r[3],
                     "updated_at": r[4]} for r in rows]
    finally:
        c.close()


def latest_value(indicator):
    rows = get_series(indicator, ascending=False)
    return rows[0] if rows else None


# ---------------------------------------------------------- 实时喂价 CRUD(独立表, 与三指标/分析卡隔离)
_FEED_TABLE = {
    "spot_hog_daily": "spot_hog_daily",
    "slaughter_rate_weekly": "slaughter_rate_weekly",
    "hog_monthly_moa": "hog_monthly_moa",
}


def feed_meta():
    """返回实时喂价元信息(名称/单位/频率/说明/来源/序列), 供前端渲染。"""
    out = {}
    for k, m in FEEDS.items():
        out[k] = {"name": m["name"], "unit": m["unit"], "freq": m["freq"],
                  "desc": m["desc"], "source": m.get("source", ""),
                  "source_url": m.get("source_url", ""),
                  "series": m.get("series", [])}
    return out


@_retry
def upsert_feed(feed, period_date, value=None, kill_volume=None, pork_white=None,
                piglet=None, note="", source="", source_url=""):
    """新增/更新实时喂价单条: spot_hog_daily(value) / slaughter_rate_weekly(value,kv,pw)
       / hog_monthly_moa(value,piglet,pw,kv)。"""
    table = _FEED_TABLE.get(feed)
    if not table:
        raise ValueError("未知实时喂价: %s" % feed)
    period_date = str(period_date or "").strip()
    if not period_date:
        raise ValueError("period_date 不能为空")
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        if feed == "spot_hog_daily":
            v = _f(value)
            c.execute(
                """INSERT INTO spot_hog_daily(period_date, value, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, note=excluded.note, source=excluded.source,
                     source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, v, note or "", source or "", source_url or "", now))
        elif feed == "slaughter_rate_weekly":
            v = _f(value)
            kv = _f(kill_volume) if kill_volume not in (None, "") else None
            pw = _f(pork_white) if pork_white not in (None, "") else None
            c.execute(
                """INSERT INTO slaughter_rate_weekly(period_date, value, kill_volume, pork_white, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, kill_volume=excluded.kill_volume, pork_white=excluded.pork_white,
                     note=excluded.note, source=excluded.source, source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, v, kv, pw, note or "", source or "", source_url or "", now))
        else:  # hog_monthly_moa
            v = _f(value)
            pl = _f(piglet) if piglet not in (None, "") else None
            pw = _f(pork_white) if pork_white not in (None, "") else None
            kv = _f(kill_volume) if kill_volume not in (None, "") else None
            c.execute(
                """INSERT INTO hog_monthly_moa(period_date, value, piglet, pork_white, kill_volume, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, piglet=excluded.piglet, pork_white=excluded.pork_white,
                     kill_volume=excluded.kill_volume, note=excluded.note, source=excluded.source,
                     source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, v, pl, pw, kv, note or "", source or "", source_url or "", now))
        c.commit()
        return True
    finally:
        c.close()


@_retry
def delete_feed(feed, period_date):
    table = _FEED_TABLE.get(feed)
    if not table:
        raise ValueError("未知实时喂价: %s" % feed)
    init_db()
    c = _conn()
    try:
        n = c.execute("DELETE FROM %s WHERE period_date=?" % table, (str(period_date),)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def get_feed_series(feed, ascending=True):
    """读取实时喂价序列( dict 列表)。"""
    table = _FEED_TABLE.get(feed)
    if not table:
        return []
    init_db()
    c = _conn()
    try:
        order = "ASC" if ascending else "DESC"
        if feed == "spot_hog_daily":
            rows = c.execute(
                "SELECT period_date, value, note, source, source_url, updated_at "
                "FROM spot_hog_daily ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "note": r[2], "source": r[3],
                     "source_url": r[4], "updated_at": r[5]} for r in rows]
        elif feed == "slaughter_rate_weekly":
            rows = c.execute(
                "SELECT period_date, value, kill_volume, pork_white, note, source, source_url, updated_at "
                "FROM slaughter_rate_weekly ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "kill_volume": r[2], "pork_white": r[3],
                     "note": r[4], "source": r[5], "source_url": r[6], "updated_at": r[7]} for r in rows]
        else:  # hog_monthly_moa
            rows = c.execute(
                "SELECT period_date, value, piglet, pork_white, kill_volume, note, source, source_url, updated_at "
                "FROM hog_monthly_moa ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "piglet": r[2], "pork_white": r[3],
                     "kill_volume": r[4], "note": r[5], "source": r[6], "source_url": r[7],
                     "updated_at": r[8]} for r in rows]
    finally:
        c.close()


# ---------------------------------------------------------- 周期分析指标 CRUD
def _analysis_table(key):
    t = _ANALYSIS_TABLE.get(key)
    if not t:
        raise ValueError("未知分析指标: %s" % key)
    return t


@_retry
def upsert_analysis(key, period_date, value=None, pe=None, pb=None,
                    note="", source="", source_url=""):
    """新增/更新分析指标单条: grain_pig_ratio(value) / index_pe(pe,pb)。"""
    table = _analysis_table(key)
    period_date = str(period_date or "").strip()
    if not period_date:
        raise ValueError("period_date 不能为空")
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        if key == "grain_pig_ratio":
            v = _f(value)
            c.execute(
                """INSERT INTO grain_pig_ratio(period_date, value, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     value=excluded.value, note=excluded.note, source=excluded.source,
                     source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, v, note or "", source or "", source_url or "", now))
        elif key == "index_pe":
            pev = _f(pe) if pe not in (None, "") else None
            pbv = _f(pb) if pb not in (None, "") else None
            c.execute(
                """INSERT INTO index_pe(period_date, pe, pb, note, source, source_url, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(period_date) DO UPDATE SET
                     pe=excluded.pe, pb=excluded.pb, note=excluded.note,
                     source=excluded.source, source_url=excluded.source_url, updated_at=excluded.updated_at""",
                (period_date, pev, pbv, note or "", source or "", source_url or "", now))
        c.commit()
        return True
    finally:
        c.close()


@_retry
def delete_analysis(key, period_date):
    if key == "capacity":
        raise ValueError("产能指标由 sow_inventory 管理, 请用对应删除接口")
    table = _analysis_table(key)
    init_db()
    c = _conn()
    try:
        n = c.execute("DELETE FROM %s WHERE period_date=?" % table, (str(period_date),)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def get_analysis_series(key, ascending=True):
    """读取分析指标序列(非 capacity)。返回 dict 列表。"""
    if key == "capacity":
        return []
    table = _analysis_table(key)
    init_db()
    c = _conn()
    try:
        order = "ASC" if ascending else "DESC"
        if key == "grain_pig_ratio":
            rows = c.execute(
                "SELECT period_date, value, note, source, source_url, updated_at "
                "FROM grain_pig_ratio ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "value": r[1], "note": r[2], "source": r[3],
                     "source_url": r[4], "updated_at": r[5]} for r in rows]
        else:  # index_pe
            rows = c.execute(
                "SELECT period_date, pe, pb, note, source, source_url, updated_at "
                "FROM index_pe ORDER BY period_date %s" % order).fetchall()
            return [{"period_date": r[0], "pe": r[1], "pb": r[2], "note": r[3],
                     "source": r[4], "source_url": r[5], "updated_at": r[6]} for r in rows]
    finally:
        c.close()


def _mom(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / prev * 100


def get_capacity_analysis():
    """汇总产能: 季度(来自 sow_inventory), 含环比与保有量分区。"""
    q = get_series("sow_inventory", ascending=True)
    target = ANALYSIS["capacity"].get("保有量", 3900)
    out = {"保有量": target, "quarterly": None}
    if q:
        last = q[-1]
        prev = q[-2] if len(q) >= 2 else None
        val = last["value"]
        out["quarterly"] = {
            "period_date": last["period_date"], "value": val,
            "prev_value": prev["value"] if prev else None,
            "mom": _mom(val, prev["value"]) if prev else None,
            "zone": _zone_of(ANALYSIS["capacity"]["zones"], val),
            "source_title": last.get("source_title", ""),
            "source_url": last.get("source_url", ""),
        }
    return out


def _zone_of(zones, value):
    if value is None:
        return None
    for z in zones:
        if z["min"] <= value < z["max"]:
            return {"label": z["label"], "color": z["color"]}
    return None


def analysis_meta():
    """返回分析指标元信息(名称/单位/阈值/利弊/来源), 供前端渲染。"""
    out = {}
    for k, m in ANALYSIS.items():
        out[k] = {
            "name": m["name"], "unit": m["unit"], "freq": m["freq"], "desc": m["desc"],
            "zones": m.get("zones", []),
            "利弊": m.get("利弊", {}),
            "来源": m.get("来源", ""),
        }
        if "保有量" in m:
            out[k]["保有量"] = m["保有量"]
    return out


# ---------------------------------------------------------------- KV 设置
def kv_get(key, default=None):
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
    init_db()
    c = _conn()
    try:
        v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        c.execute("INSERT OR REPLACE INTO app_kv(k, v, updated_at) VALUES(?,?,?)",
                  (key, v, datetime.datetime.now().isoformat(timespec="seconds")))
        c.commit()
    finally:
        c.close()


# 能繁母猪存栏量权威季度历史(万头), 统计局口径, 建信期货研究中心整理(2020Q1–2026Q2)
# 用于观察完整猪周期(2021 峰值→2024 谷底→2025-26 恢复)。period_date 取季度末。
CAPACITY_HISTORY = [
    ("2020-03-31", 3380), ("2020-06-30", 3620), ("2020-09-30", 3810), ("2020-12-31", 4150),
    ("2021-03-31", 4320), ("2021-06-30", 4570), ("2021-09-30", 4480), ("2021-12-31", 4310),
    ("2022-03-31", 4170), ("2022-06-30", 4270), ("2022-09-30", 4360), ("2022-12-31", 4390),
    ("2023-03-31", 4310), ("2023-06-30", 4310), ("2023-09-30", 4250), ("2023-12-31", 4130),
    ("2024-03-31", 3970), ("2024-06-30", 4030), ("2024-09-30", 4060), ("2024-12-31", 4080),
    ("2025-03-31", 4030), ("2025-06-30", 4010), ("2025-09-30", 4030), ("2025-12-31", 3960),
    ("2026-03-31", 3910), ("2026-06-30", 3780),
]


def seed_capacity_history():
    """补全能繁权威季度历史(幂等 upsert, 不删除既有数据)。返回写入条数。"""
    n = 0
    for d, v in CAPACITY_HISTORY:
        upsert_point("sow_inventory", d, value=v,
                     source="统计局季度发布(建信期货整理)",
                     source_title="能繁母猪存栏量(万头)",
                     source_url="https://www.stats.gov.cn/")
        n += 1
    return n


def load_sample_data():
    """首次体验: 写入示例数据(各表为空时)。能繁补全权威季度历史; 其余带来源便于演示。"""
    n = 0
    # 能繁: 内置 2020Q1–2026Q2 权威季度历史(无论是否已存在都补全, 属权威序列)
    n += seed_capacity_history()
    # 仔猪/生猪: 近期周度监测(同源双指标)
    if not get_series("piglet_price"):
        pig = [
            ("2026-07-30", 23.00, 11.20),
            ("2026-08-06", 22.80, 11.00),
            ("2026-08-13", 22.42, 11.13),
        ]
        for d, pl, pk in pig:
            upsert_point("piglet_price", d, piglet=pl, pork=pk,
                         source="农业农村部周度监测",
                         source_url="https://www.agri.cn/sj/jcyj/")
            n += 1
    # 冻品库存: 手动示例
    if not get_series("frozen_inventory"):
        fro = [
            ("2026-07-30", 142.0),
            ("2026-08-06", 145.0),
            ("2026-08-13", 148.0),
        ]
        for d, v in fro:
            upsert_point("frozen_inventory", d, value=v, source="示例")
            n += 1
    # ---- 周期分析示例 ----
    if not get_analysis_series("grain_pig_ratio"):
        for d, v in [("2026-07-30", 5.4), ("2026-08-06", 5.3), ("2026-08-13", 5.2)]:
            upsert_analysis("grain_pig_ratio", d, value=v, source="示例(发改委每周)", note="示例")
            n += 1
    if not get_analysis_series("index_pe"):
        for d, pe, pb in [("2026-07-30", 45.0, 2.2), ("2026-08-06", 43.0, 2.15), ("2026-08-13", 42.0, 2.1)]:
            upsert_analysis("index_pe", d, pe=pe, pb=pb, source="示例(中证指数公司)", note="示例")
            n += 1
    return n


def stats():
    out = {}
    for ind in INDICATORS:
        out[ind] = len(get_series(ind))
    out["grain_pig_ratio"] = len(get_analysis_series("grain_pig_ratio"))
    out["index_pe"] = len(get_analysis_series("index_pe"))
    for f in FEEDS:
        out[f] = len(get_feed_series(f))
    return out


if __name__ == "__main__":
    init_db()
    print("示例数据条数:", load_sample_data())
    print("各指标条数:", json.dumps(stats(), ensure_ascii=False))
