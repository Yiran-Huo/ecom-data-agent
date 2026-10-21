"""
建评测场景：读 eval/scenarios/specs.json，为每个场景建一个视图库 ecom_s_<id>，并算出标准答案。

运行方式（在项目根目录，先跑完 load_data.py 和 verify_db.py）：
    python scripts/build_scenarios.py                 # 建全部场景
    python scripts/build_scenarios.py --ids d01 d02   # 只建这几个
    python scripts/build_scenarios.py --drop          # 删除全部场景库

需要管理员账号（.env 里的 DB_ADMIN_USER）有建库、建视图、GRANT 的权限（root 就可以）。

每个场景做的事：
    1. 用真实数据算出目标（例如「基线 GMV 第 1 的品牌」是哪个）
    2. 建视图库，在排查日当天注入异动（eval/scenario_sql.py）
    3. 用分析工具分别在真实库和场景库上算一遍：
       - 排查日在真实数据里本身是否就异常（是的话这个日期不适合，换一个）
       - 注入后变化够不够大（不够大就加大强度，否则这个场景本身就不成立）
       - 注入的原因真实解释了多少 ΔGMV（true_share，用来判贡献度估计的误差）
       - 纯算法基线的结论和得分（只对 dev 和 real；测试集不跑、不显示，避免看了测试集结果再去调算法）
    4. 结果写入 eval/scenarios/resolved.json（评测脚本读这个）和 eval/scenarios/report.md（给人看）
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from db_utils import get_conn  # noqa: E402

from agent.analysis import config as C  # noqa: E402
from agent.analysis.anomaly import baseline_skip_days, detect  # noqa: E402
from agent.analysis.data import clear_cache, load_window  # noqa: E402
from agent.analysis.metrics import apply_filters  # noqa: E402
from agent.analysis.pipeline import auto_diagnose  # noqa: E402
from eval.scenario_sql import (META_DB, first_seen_statements, meta_statements, needs_first_seen,  # noqa: E402
                               view_statements)
from eval.scenario_types import resolve, zeroed_segments  # noqa: E402
from eval.scoring import score  # noqa: E402

SPECS_PATH = ROOT / "eval" / "scenarios" / "specs.json"
RESOLVED_PATH = ROOT / "eval" / "scenarios" / "resolved.json"
REPORT_PATH = ROOT / "eval" / "scenarios" / "report.md"
PREFIX = "ecom_s_"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def grant_read(conn):
    """让 Agent 的只读账号能读所有场景库（ecom_s_ 开头的库）。"""
    user = os.getenv("DB_RO_USER")
    with conn.cursor() as cur:
        cur.execute("SELECT host FROM mysql.user WHERE user = %s", (user,))
        hosts = [r[0] for r in cur.fetchall()]
        if not hosts:
            sys.exit(f"❌ 找不到只读账号 {user}")
        for host in hosts:
            # 库名里的 _ 是通配符，要转义；不传参数执行，避免 % 被当成格式化符号
            cur.execute(f"GRANT SELECT ON `ecom\\_s\\_%`.* TO {conn.escape(user)}@{conn.escape(host)}")
    conn.commit()


def drop_all(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW DATABASES LIKE 'ecom\\\\_s\\\\_%'")
        names = [r[0] for r in cur.fetchall()]
        for name in names:
            cur.execute(f"DROP DATABASE `{name}`")
            clear_cache(name)
    conn.commit()
    log(f"已删除 {len(names)} 个场景库")


def gmv_today(frame: pd.DataFrame, day: date, filters: dict) -> float:
    today = frame[(frame["d"] == pd.Timestamp(day)) & (frame["et"] == 2) & (frame["price"] > 0)]
    return float(apply_filters(today, filters)["price"].sum())


def build_one(conn, spec: dict, base_db: str) -> dict:
    day = date.fromisoformat(spec["date"])
    db = PREFIX + spec["id"]
    base_frame = load_window(day, base_db)
    r = resolve(spec, base_frame)

    stmts = view_statements(db, base_db, r["ops"], day)
    if needs_first_seen(r["ops"]):   # 删掉了用户整天的行为：按注入后的数据重算首次出现日期
        stmts += first_seen_statements(db, base_db, day)
    with conn.cursor() as cur:
        for stmt in stmts:
            cur.execute(stmt)
    conn.commit()
    clear_cache(db)

    t0 = time.time()
    scn_frame = load_window(day, db)
    fetch_seconds = round(time.time() - t0, 1)

    skip = baseline_skip_days(base_frame, day)   # 基线里的极端日（例如 11-15）不参与
    det_base, det_scn = detect(base_frame, day, skip_days=skip), detect(scn_frame, day, skip_days=skip)
    natural = spec["type"] == "natural"
    expected = r["expected"]

    # 注入的原因真实解释了多少 ΔGMV
    for cause in expected["causes"]:
        if cause["dimension"] == "hour":
            cause["true_share"] = None
            continue
        f = {cause["dimension"]: cause["value"]}
        injected = gmv_today(scn_frame, day, f) - gmv_today(base_frame, day, f)
        cause["true_share"] = round(injected / det_scn["change"], 4) if det_scn["change"] else None

    # 场景是否成立
    problems = []
    if not natural and (det_base["is_anomaly"] or det_base["event_gaps"]
                        or det_base["hour_gap_share"] >= C.HOUR_GAP_MIN_SHARE):
        problems.append(f"排查日在真实数据里本身就异常（{det_base['rel']:+.1%}，z={det_base['z']:.1f}），换一个日期")
    for seg in zeroed_segments(r["ops"], expected["pattern"]):
        problems.append(f"分段 {seg} 被删到 0，和埋点丢失分不开，strength 要 < 1")
    if expected["pattern"] == "normal":
        pass   # 不注入，真实数据正常即可
    elif expected["pattern"] == "data_issue":
        if not (det_scn["event_gaps"] or (det_scn["hour_gaps"] and det_scn["hour_gap_share"] >= C.HOUR_GAP_MIN_SHARE)):
            problems.append("没有检测到数据缺失（按小时或按事件类型）")
    elif not det_scn["is_anomaly"]:
        problems.append(f"注入后变化不够大（{det_scn['rel']:+.1%}，z={det_scn['z']:.1f}），"
                        f"阈值是 {C.REL_THRESHOLD:.0%} 和 z≥{C.Z_THRESHOLD}，请加大 strength")

    # 测试集只在最后评测时跑一次：这里不跑纯算法，避免开发时看到测试集的对错
    algo = None if spec["split"] == "test" else auto_diagnose(scn_frame, day)
    return {
        "id": spec["id"], "split": spec["split"], "type": spec["type"], "date": spec["date"], "db": db,
        "question": r["question"], "spec": spec, "targets": r["targets"], "ops": r["ops"],
        "expected": expected,
        "valid": not problems, "problems": problems,
        "check": {"skipped_days": skip, "base_rel": det_base["rel"], "base_z": det_base["z"],
                  "scn_rel": det_scn["rel"], "scn_z": det_scn["z"], "fetch_seconds": fetch_seconds},
        "algo": algo, "algo_score": score(algo, expected) if algo else None,
    }


def write_report(results: list[dict]):
    rows = []
    for r in results:
        c = r["check"]
        rows.append({
            "id": r["id"], "split": r["split"], "类型": r["type"], "日期": r["date"],
            "目标": json.dumps(r["targets"], ensure_ascii=False),
            "强度": r["spec"].get("strength", ""),
            "真实数据当天": f"{c['base_rel']:+.1%} / z {c['base_z']:.1f}",
            "注入后": f"{c['scn_rel']:+.1%} / z {c['scn_z']:.1f}",
            "真实贡献": ", ".join(f"{x['value']} {x['true_share']:.0%}" for x in r["expected"]["causes"]
                              if x.get("true_share") is not None) or "—",
            "成立": "✅" if r["valid"] else "❌ " + "；".join(r["problems"]),
            "纯算法": "（测试集不显示）" if not r.get("algo_score") else "✅" if r["algo_score"]["top1"]
                      else f"❌ {r['algo']['pattern']} "
                      + ", ".join(f"{x['dimension']}={x['value']}" for x in r["algo"]["root_causes"]),
        })
    n_valid = sum(r["valid"] for r in results)
    seen = [r for r in results if r["valid"] and r.get("algo_score")]
    n_algo = sum(r["algo_score"]["top1"] for r in seen)
    text = "\n".join([
        "# 评测场景报告（自动生成）",
        "",
        f"> 由 scripts/build_scenarios.py 于 {time.strftime('%Y-%m-%d %H:%M')} 生成。"
        f"共 {len(results)} 个场景，成立 {n_valid} 个；纯算法基线在成立的 dev / real 场景里答对 {n_algo} / {len(seen)} 个"
        "（测试集只在最后评测时跑一次，这里不显示）。",
        "> 「成立」为 ❌ 的场景评测时会跳过：按提示换日期或调整 specs.json 里的 strength 后重新运行。",
        "",
        pd.DataFrame(rows).to_markdown(index=False),
        "",
    ])
    REPORT_PATH.write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="*", help="只建这些场景")
    parser.add_argument("--drop", action="store_true", help="删除全部场景库")
    args = parser.parse_args()

    conn = get_conn("admin")
    try:
        if args.drop:
            drop_all(conn)
            return
        specs = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
        if args.ids:
            specs = [s for s in specs if s["id"] in args.ids]
        base_db = os.getenv("DB_NAME")

        log("准备：建 scn_meta 库，给只读账号授权 ...")
        with conn.cursor() as cur:
            for stmt in meta_statements():
                cur.execute(stmt)
        conn.commit()
        grant_read(conn)

        old = {r["id"]: r for r in json.loads(RESOLVED_PATH.read_text(encoding="utf-8"))} \
            if RESOLVED_PATH.exists() else {}
        for i, spec in enumerate(specs, 1):
            log(f"[{i}/{len(specs)}] {spec['id']} {spec['type']} {spec['date']} ...")
            result = build_one(conn, spec, base_db)
            old[spec["id"]] = result
            mark = "✅" if result["valid"] else "❌ " + "；".join(result["problems"])
            algo = "" if not result["algo_score"] else f" · 纯算法 {'✅' if result['algo_score']['top1'] else '❌'}"
            log(f"    注入后 {result['check']['scn_rel']:+.1%}（取数 {result['check']['fetch_seconds']} 秒）{mark}{algo}")
    finally:
        conn.close()

    results = sorted(old.values(), key=lambda r: r["id"])
    RESOLVED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(results)
    log(f"✅ 完成。标准答案：{RESOLVED_PATH.relative_to(ROOT)}；报告：{REPORT_PATH.relative_to(ROOT)}")
    log(f"（场景库都依赖 {META_DB} 库，不要删它）")


if __name__ == "__main__":
    main()
