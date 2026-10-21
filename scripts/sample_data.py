"""
按用户抽样：只保留 user_id % SAMPLE_MOD == 0 的用户，保证每个被选中用户的行为序列是完整的。
同时处理 10 月和 11 月两个文件，用的是同一条抽样规则，所以两个月是同一批用户，行为历史前后连续。

运行方式（在项目根目录 ecom-agent/ 下）：python scripts/sample_data.py
"""
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW_FILES = [ROOT / "data" / "raw" / "2019-Oct.csv", ROOT / "data" / "raw" / "2019-Nov.csv"]
OUT_PATH = ROOT / "data" / "sample.csv"
SAMPLE_MOD = 10          # 保留约 1/10 的用户。改这个数之前想清楚：两个月必须用同一个值
CHUNK_SIZE = 1_000_000   # 每次读 100 万行


def main():
    missing = [p.name for p in RAW_FILES if not p.exists()]
    if missing:
        raise FileNotFoundError(f"找不到 {', '.join(missing)}，请确认已下载并解压到 data/raw/ 下")
    if OUT_PATH.exists():
        OUT_PATH.unlink()  # 删除上一次的结果，否则重复运行时数据会被追加两遍

    total_rows, kept_rows = 0, 0
    all_users, kept_users = set(), set()
    start = time.time()
    first_chunk = True

    for raw in RAW_FILES:
        print(f"—— 处理 {raw.name} ——")
        file_rows, file_kept = 0, 0
        for chunk in pd.read_csv(raw, chunksize=CHUNK_SIZE):
            file_rows += len(chunk)
            all_users.update(chunk["user_id"].unique())

            # 核心一行：按用户筛选，而不是按行随机抽
            sampled = chunk[chunk["user_id"] % SAMPLE_MOD == 0]
            file_kept += len(sampled)
            kept_users.update(sampled["user_id"].unique())

            # 整个输出文件只在第一块写表头，之后都只追加数据
            sampled.to_csv(OUT_PATH, mode="a", header=first_chunk, index=False)
            first_chunk = False
            print(f"  已处理 {file_rows:,} 行，保留 {file_kept:,} 行，累计用时 {time.time() - start:.0f} 秒")
        total_rows += file_rows
        kept_rows += file_kept

    print("—— 完成 ——")
    print(f"原始：{total_rows:,} 行，{len(all_users):,} 个用户")
    print(f"抽样：{kept_rows:,} 行，{len(kept_users):,} 个用户")
    print(f"行数占比 {kept_rows / total_rows:.1%}，用户占比 {len(kept_users) / len(all_users):.1%}")


if __name__ == "__main__":
    main()
