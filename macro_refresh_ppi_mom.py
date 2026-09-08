"""macro_refresh_ppi_mom.py — PPI 官方环比(统计局发布稿)入库脚本。

用法:
  python macro_refresh_ppi_mom.py            # 增量: 只抓最新一期官方稿入库
  python macro_refresh_ppi_mom.py --backfill # 历史回填: 扫栏目分页(默认近~20年)全部入库
  python macro_refresh_ppi_mom.py --backfill --until 201001  # 回填到指定年(含)

入库: ppi_monthly.mom / yoy / cum(官方一手口径), 幂等 upsert, 不覆盖已存在的东财同比。
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macro_db          # noqa: E402
import macro_ppi_official as m  # noqa: E402


def upsert_rows(rows):
    """官方稿行 → ppi_monthly。只写 yoy/mom(与东财同比同口径、环比为官方新增);
    base_index/cum_index 不写(官方累计是涨幅%口径, 与东财"指数(上年同期=100)"不同, 保留东财列)。"""
    n = 0
    for r in rows:
        macro_db.upsert_ppi(
            {"period": r["period"], "yoy": r.get("yoy"), "mom": r.get("mom"),
             "base_index": None, "cum_index": None},
            source=r.get("source", m.SOURCE_TXT),
            source_url=r.get("source_url", ""))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--until", default="200501", help="回填截止期 YYYYMM, 默认 200501")
    ap.add_argument("--max-pages", type=int, default=260)
    args = ap.parse_args()

    macro_db.init_db()

    if args.backfill:
        print("backfill scanning zxfb pages (max %d) until %s ..." % (args.max_pages, args.until))

        def prog(pg, got):
            if pg % 20 == 0 or pg == 1:
                print("  page %d scanned, rows %d (latest period %s)" % (pg, got, ""))
        res = m.fetch_history(max_pages=args.max_pages, until=args.until, progress=prog)
        print("scan done: pages=%d rows=%d range=%s~%s" % (
            res["scanned_pages"], res["count"], res["earliest"], res["latest"]))
        n = upsert_rows(res["rows"])
        print("upserted %d rows" % n)
    else:
        latest = m.fetch_latest(scan_pages=8)
        if not latest:
            print("no latest official PPI release found")
            return
        n = upsert_rows([latest])
        print("incremental: %s mom=%s yoy=%s upserted=%d src=%s" % (
            latest["period"], latest.get("mom"), latest.get("yoy"), n, latest["source_url"]))

    cov = macro_db.coverage()["ppi"]
    print("ppi coverage now: %d rows %s ~ %s" % (cov["count"], cov["start"], cov["end"]))
    if cov["latest"]:
        print("latest row:", dict(cov["latest"]))


if __name__ == "__main__":
    main()
