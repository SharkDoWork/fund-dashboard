"""pigcycle_refresh.py — 猪周期「实时喂价」每日自动拉取与刷新。

三条自动喂价:
  - spot_hog_daily       生猪(外三元)现货日度价(元/公斤), 中国养猪网, 全量 upsert(含历史回Fill)
  - slaughter_rate_weekly 周度屠宰开工率(%), 卓创资讯·中国畜牧业协会, 默认仅最新周 upsert
  - hog_monthly_moa      月度生猪价格(出场/仔猪/白条/屠宰量), 农业农村部《生猪专题》, 覆盖近 3 年+

用法(独立运行 / 计划任务):
  python pigcycle_refresh.py                  # 日度价全量 + 屠宰最新周 + 月度(3年)
  python pigcycle_refresh.py --slaughter-history --pages 12   # 额外回Fill屠宰多周历史
  python pigcycle_refresh.py --monthly-history --start 202301 # 覆盖回Fill月度(默认已含3年)
  python pigcycle_refresh.py --spot-only
  python pigcycle_refresh.py --slaughter-only --slaughter-history
  python pigcycle_refresh.py --monthly-only

被 live_server.py 的 /api/pigcycle/feed/auto 路由调用, 实现「页面打开时每日自动刷新一次」。
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pigcycle_db
import pigcycle_fetch


def refresh_spot(force=False):
    """抓中国养猪网外三元日度价, 全量 upsert(=历史回Fill + 当日更新)。"""
    r = pigcycle_fetch.fetch_spot_hog_daily()
    if not r.get("ok"):
        return {"ok": False, "feed": "spot_hog_daily", "message": r.get("message", "抓取失败")}
    cnt = 0
    src = r.get("source_url") or "https://www.zhuwang.com.cn/"
    for row in r["rows"]:
        pigcycle_db.upsert_feed(
            "spot_hog_daily", row["date"], value=row["value"],
            note="auto", source="中国养猪网(ajax_zhujia)", source_url=src)
        cnt += 1
    return {"ok": True, "feed": "spot_hog_daily", "upserted": cnt,
            "latest": r.get("latest"), "latest_date": r.get("latest_date"),
            "message": "日度价已写入 %d 条(含历史)" % cnt}


def refresh_slaughter(latest_only=True, pages=12):
    """屠宰开工率: latest_only=仅最新周; 否则多周历史回Fill。"""
    if latest_only:
        r = pigcycle_fetch.fetch_slaughter_weekly_latest()
        rows = [r] if r.get("ok") else []
    else:
        r = pigcycle_fetch.fetch_slaughter_weekly_history(max_pages=pages)
        rows = r.get("rows", []) if r.get("ok") else []
    if not rows:
        return {"ok": False, "feed": "slaughter_rate_weekly",
                "message": r.get("message", "未解析到卓创周评")}
    cnt = 0
    for row in rows:
        if not row.get("date"):
            continue
        pigcycle_db.upsert_feed(
            "slaughter_rate_weekly", row["date"], value=row.get("rate"),
            kill_volume=row.get("kill_volume"), pork_white=row.get("pork_white"),
            note="auto", source="卓创资讯·中国畜牧业协会", source_url=row.get("url", ""))
        cnt += 1
    last = rows[-1]
    return {"ok": True, "feed": "slaughter_rate_weekly", "upserted": cnt,
            "latest_date": last.get("date"),
            "message": "屠宰开工率已写入 %d 周" % cnt}


def refresh_monthly(start_ym="202301"):
    """抓农业农村部月度生猪价格, 全量 upsert(覆盖近 3 年+, 幂等)。"""
    r = pigcycle_fetch.fetch_hog_monthly_moa(start_ym=start_ym)
    if not r.get("ok"):
        return {"ok": False, "feed": "hog_monthly_moa", "message": r.get("message", "抓取失败")}
    cnt = 0
    for row in r["rows"]:
        pigcycle_db.upsert_feed(
            "hog_monthly_moa", row["period"], value=row.get("value"),
            piglet=row.get("piglet"), pork_white=row.get("pork_white"),
            kill_volume=row.get("kill_volume"),
            note="auto", source="农业农村部·生猪专题", source_url=row.get("url", ""))
        cnt += 1
    return {"ok": True, "feed": "hog_monthly_moa", "upserted": cnt,
            "start": r.get("start"), "end": r.get("end"),
            "message": "月度生猪价格已写入 %d 个月(%s~%s)" % (cnt, r.get("start"), r.get("end"))}


def refresh_all(slaughter_history=False, pages=12, monthly_start="202301"):
    """组合刷新。返回 {spot:..., slaughter:..., monthly:...}。"""
    out = {
        "spot": refresh_spot(),
        "slaughter": refresh_slaughter(latest_only=not slaughter_history, pages=pages),
        "monthly": refresh_monthly(start_ym=monthly_start),
    }
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="猪周期实时喂价每日刷新")
    ap.add_argument("--slaughter-history", action="store_true",
                    help="额外回Fill卓创周评屠宰开工率多周历史")
    ap.add_argument("--pages", type=int, default=12, help="屠宰历史扫描页数")
    ap.add_argument("--monthly-history", action="store_true",
                    help="回Fill月度历史(默认 refresh_all 已含近3年)")
    ap.add_argument("--start", type=str, default="202301", help="月度历史起始月 YYYYMM")
    ap.add_argument("--spot-only", action="store_true")
    ap.add_argument("--slaughter-only", action="store_true")
    ap.add_argument("--monthly-only", action="store_true")
    a = ap.parse_args()

    pigcycle_db.init_db()
    if a.spot_only:
        print(json.dumps(refresh_spot(), ensure_ascii=False, indent=2))
    elif a.slaughter_only:
        print(json.dumps(refresh_slaughter(latest_only=not a.slaughter_history, pages=a.pages),
                         ensure_ascii=False, indent=2))
    elif a.monthly_only:
        print(json.dumps(refresh_monthly(start_ym=a.start), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(refresh_all(slaughter_history=a.slaughter_history,
                                     pages=a.pages, monthly_start=a.start),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
