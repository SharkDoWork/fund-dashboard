# -*- coding: utf-8 -*-
"""
历史数据导入工具
=================
把导出的 CSV / JSON 重新导入 SQLite 历史库(按主键去重: 已存在则更新, 不重复插入):
  --file 指定文件:
    - fund_history_*.json  -> 一次导入全部三表
    - nav_history_*.csv    -> 仅导入净值表
    - corrections_*.csv    -> 仅导入修正表
    - snapshots_*.csv      -> 仅导入快照表
  --table 可选指定 CSV 对应表(文件名为标准导出名时可不填)
  --dry-run 仅预览不写入

用途: 换机器迁移数据 / 备份恢复 / 历史数据回放分析
示例:
  python import_data.py --file data/export/fund_history_20260813_103000.json
  python import_data.py --file data/export/nav_history_20260813_103000.csv
  python import_data.py --file data/export/nav_history_20260813_103000.csv --dry-run
"""
import argparse, csv, json, os, re, sys, datetime
import fund_db

BASE = os.path.dirname(os.path.abspath(__file__))
FIELD_MAP = {
    "nav_history": ["trade_date", "fund_code", "dwjz", "ljjz", "official_chg_pct", "source", "updated_at"],
    "corrections": ["trade_date", "fund_code", "official_dwjz", "official_chg_pct", "last_model_chg_pct",
                    "min_model_chg_pct", "max_model_chg_pct", "snap_count", "bias_pct", "corrected_at"],
    "snapshots": ["ts", "fund_code", "trade_date", "model_chg_pct", "adjusted_chg_pct",
                  "official_live_chg_pct", "live_price", "baseline_date", "baseline_nav",
                  "market_status", "quote_time"],
}

def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None

def _row_to_record(table, row, headers):
    """按导出表头(中文)或英文原表头映射回字段"""
    rec = {}
    for i, h in enumerate(headers):
        key = None
        if h in FIELD_MAP[table]:
            key = h
        else:
            # 中文表头还原
            cn_map = {"基金(代码)": "fund_code", "交易日": "trade_date", "单位净值": "dwjz",
                      "累计净值": "ljjz", "官方涨跌幅%": "official_chg_pct", "快照时间": "ts",
                      "模型预估%": "model_chg_pct", "修正预估%": "adjusted_chg_pct",
                      "基金实时/估算%": "official_live_chg_pct", "现价/估算净值": "live_price",
                      "基线日期": "baseline_date", "基线净值": "baseline_nav", "市场状态": "market_status",
                      "行情时间": "quote_time", "官方净值": "official_dwjz", "末次模型预估%": "last_model_chg_pct",
                      "当日预估最低%": "min_model_chg_pct", "当日预估最高%": "max_model_chg_pct",
                      "当日快照数": "snap_count", "偏差%(末次预估-官方)": "bias_pct", "修正生成时间": "corrected_at",
                      "数据来源": "source", "入库时间": "updated_at"}
            key = cn_map.get(h)
        if key is None:
            continue
        v = row[i] if i < len(row) else ""
        if key == "fund_code" and v:
            # 兼容导出显示格式 "名称(516670)" -> 还原原始代码
            m = re.search(r"\((\d{6})\)", str(v))
            if m:
                v = m.group(1)
        if key in ("dwjz", "ljjz", "official_chg_pct", "official_dwjz", "last_model_chg_pct",
                   "min_model_chg_pct", "max_model_chg_pct", "bias_pct", "model_chg_pct",
                   "adjusted_chg_pct", "official_live_chg_pct", "live_price", "baseline_nav"):
            v = _num(v)
        if key == "snap_count":
            try:
                v = int(v) if v not in ("", None) else 0
            except (TypeError, ValueError):
                v = 0
        rec[key] = v if v not in ("", None) else None
    return rec

def import_json(path, dry_run):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    plan = {}
    for table, tbl in payload.get("tables", {}).items():
        if table not in FIELD_MAP:
            continue
        rows = []
        for row in tbl.get("rows", []):
            rec = _row_to_record(table, row, tbl.get("headers", []))
            if rec.get("trade_date") or rec.get("ts"):
                rows.append(rec)
        plan[table] = rows
    _apply(plan, dry_run)
    return plan

def import_csv(path, table, dry_run):
    if table not in FIELD_MAP:
        print(f"未知表: {table} (可选: {list(FIELD_MAP)})")
        sys.exit(1)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        headers = next(rd)
        rows = []
        for row in rd:
            rec = _row_to_record(table, row, headers)
            if rec.get("trade_date") or rec.get("ts"):
                rows.append(rec)
    plan = {table: rows}
    _apply(plan, dry_run)
    return plan

def _apply(plan, dry_run):
    total = sum(len(v) for v in plan.values())
    print(f"{'【预览】将导入' if dry_run else '导入'} {total} 条记录:")
    for t, rows in plan.items():
        print(f"  - {t}: {len(rows)} 条 (示例: {rows[0] if rows else '无'})")
    if dry_run:
        print("dry-run 模式: 未写入任何数据")
        return
    fund_db.init_db()
    done = 0
    for t, rows in plan.items():
        if t == "nav_history":
            done += fund_db.upsert_nav(rows)
        elif t == "snapshots":
            for r in rows:
                fund_db.add_snapshot(r)
                done += 1
        elif t == "corrections":
            c = fund_db._conn()
            try:
                now = datetime.datetime.now().isoformat(timespec="seconds")
                for r in rows:
                    r.setdefault("corrected_at", now)
                    c.execute(
                        """INSERT OR REPLACE INTO corrections(trade_date,fund_code,official_dwjz,official_chg_pct,
                           last_model_chg_pct,min_model_chg_pct,max_model_chg_pct,snap_count,bias_pct,corrected_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (r.get("trade_date"), r.get("fund_code"), r.get("official_dwjz"),
                         r.get("official_chg_pct"), r.get("last_model_chg_pct"), r.get("min_model_chg_pct"),
                         r.get("max_model_chg_pct"), r.get("snap_count") or 0, r.get("bias_pct"),
                         r.get("corrected_at")))
                    done += 1
                c.commit()
            finally:
                c.close()
    print(f"写入完成: {done} 条 (按主键去重, 已存在则更新)")
    print("当前库统计:", fund_db.stats())

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="导入基金历史数据(CSV/JSON -> SQLite)")
    ap.add_argument("--file", required=True, help="导入文件路径")
    ap.add_argument("--table", choices=list(FIELD_MAP), help="CSV 对应的表名(JSON 无需指定)")
    ap.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    args = ap.parse_args()
    if not os.path.exists(args.file):
        print(f"文件不存在: {args.file}")
        sys.exit(1)
    base = os.path.basename(args.file)
    if base.endswith(".json"):
        import_json(args.file, args.dry_run)
    elif base.endswith(".csv"):
        table = args.table
        if not table:
            for t in FIELD_MAP:
                if base.startswith(t + "_"):
                    table = t
                    break
        if not table:
            print("CSV 文件名无法识别表名, 请用 --table 指定 (可选:", list(FIELD_MAP), ")")
            sys.exit(1)
        import_csv(args.file, table, args.dry_run)
    else:
        print("仅支持 .json 或 .csv 文件")
        sys.exit(1)
