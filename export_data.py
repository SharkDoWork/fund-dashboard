# -*- coding: utf-8 -*-
"""
历史数据导出工具
=================
从 data/fund_history.db 导出全部/指定范围的历史数据, 供留存、分析、迁移:
  --format csv  (默认) 三个 CSV(UTF-8 BOM, Excel 直接打开不乱码): nav_history/corrections/snapshots
  --format json        一个完整 JSON 文件(含全部三表 + 元信息)
  --format all         同时导出 CSV + JSON
  --start/--end        可选日期范围(YYYY-MM-DD)
示例:
  python export_data.py
  python export_data.py --format json
  python export_data.py --format all --start 2026-08-01 --end 2026-08-13
"""
import argparse, csv, json, os, datetime
import fund_db

BASE = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BASE, "data", "export")

FUND_NAMES = {"516670": "招商中证畜牧养殖ETF", "003095": "中欧医疗健康混合A"}
TABLE_CN = {"nav_history": "官方净值历史", "snapshots": "预估快照", "corrections": "修正记录(预估vs实际)"}

def export_csv(rows, headers, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    return path

def do_export(fmt, start, end):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    files = []

    # 拉取数据
    data = {}
    for t in ("nav_history", "snapshots", "corrections"):
        names, rows = fund_db.query(t)
        # 日期过滤
        if start or end:
            date_col = 0 if t != "snapshots" else 2  # snapshots 第3列是 trade_date
            filt = []
            for r in rows:
                d = str(r[date_col] or "")
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                filt.append(r)
            rows = filt
        # 表头中文化
        cn_headers = []
        for i, n in enumerate(names):
            if n == "fund_code":
                cn_headers.append("基金(代码)")
            elif n == "trade_date":
                cn_headers.append("交易日")
            elif n == "dwjz":
                cn_headers.append("单位净值")
            elif n == "ljjz":
                cn_headers.append("累计净值")
            elif n == "official_chg_pct":
                cn_headers.append("官方涨跌幅%")
            elif n == "ts":
                cn_headers.append("快照时间")
            elif n == "model_chg_pct":
                cn_headers.append("模型预估%")
            elif n == "adjusted_chg_pct":
                cn_headers.append("修正预估%")
            elif n == "official_live_chg_pct":
                cn_headers.append("基金实时/估算%")
            elif n == "live_price":
                cn_headers.append("现价/估算净值")
            elif n == "baseline_date":
                cn_headers.append("基线日期")
            elif n == "baseline_nav":
                cn_headers.append("基线净值")
            elif n == "market_status":
                cn_headers.append("市场状态")
            elif n == "quote_time":
                cn_headers.append("行情时间")
            elif n == "official_dwjz":
                cn_headers.append("官方净值")
            elif n == "last_model_chg_pct":
                cn_headers.append("末次模型预估%")
            elif n == "min_model_chg_pct":
                cn_headers.append("当日预估最低%")
            elif n == "max_model_chg_pct":
                cn_headers.append("当日预估最高%")
            elif n == "snap_count":
                cn_headers.append("当日快照数")
            elif n == "bias_pct":
                cn_headers.append("偏差%(末次预估-官方)")
            elif n == "corrected_at":
                cn_headers.append("修正生成时间")
            elif n == "source":
                cn_headers.append("数据来源")
            elif n == "updated_at":
                cn_headers.append("入库时间")
            else:
                cn_headers.append(n)
        # 基金代码 -> 名称+代码
        rows2 = []
        for r in rows:
            rr = list(r)
            for i, n in enumerate(names):
                if n == "fund_code" and rr[i] in FUND_NAMES:
                    rr[i] = f"{FUND_NAMES[rr[i]]}({rr[i]})"
            rows2.append(rr)
        data[t] = {"headers": cn_headers, "rows": rows2}

    if fmt in ("csv", "all"):
        for t in ("nav_history", "corrections", "snapshots"):
            p = os.path.join(EXPORT_DIR, f"{t}_{stamp}.csv")
            export_csv(data[t]["rows"], data[t]["headers"], p)
            files.append(p)
    if fmt in ("json", "all"):
        payload = {
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "db": fund_db.DB_PATH,
            "tables": {t: {"headers": data[t]["headers"], "rows": data[t]["rows"]} for t in data},
        }
        p = os.path.join(EXPORT_DIR, f"fund_history_{stamp}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        files.append(p)

    print(f"已导出 {len(files)} 个文件到 {EXPORT_DIR}:")
    for p in files:
        print("  -", os.path.basename(p), f"({os.path.getsize(p)} 字节)")
    print("\n表格行数:", {k: len(v['rows']) for k, v in data.items()})

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="导出基金历史数据(SQLite -> CSV/JSON)")
    ap.add_argument("--format", choices=["csv", "json", "all"], default="csv")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD")
    args = ap.parse_args()
    do_export(args.format, args.start, args.end)
