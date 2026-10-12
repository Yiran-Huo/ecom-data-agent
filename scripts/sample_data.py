"""
按用户抽样：只保留 user_id % SAMPLE_MOD == 0 的用户，保证每个被选中用户的行为序列是完整的。
运行方式（在项目根目录 ecom-agent/ 下）：python scripts/sample_data.py
"""
from pathlib import Path
import time
import pandas as pd

RAW_PATH = Path("data/raw/2019-Oct.csv")
OUT_PATH = Path("data/sample.csv")
SAMPLE_MOD = 10          # 保留约 1/10 的用户；如果后面导入 MySQL 太慢，可以改成 20
CHUNK_SIZE = 1_000_000   # 每次读 100 万行


def main():
    if not RAW_PATH.exists():
        raise FileNotFoundError(f"找不到 {RAW_PATH}，请确认已下载并解压到 data/raw/ 下")
    if OUT_PATH.exists():
        OUT_PATH.unlink()  # 删除上一次的结果，否则重复运行时数据会被追加两遍

    total_rows, kept_rows = 0, 0
    all_users, kept_users = set(), set()
    start = time.time()

    reader = pd.read_csv(RAW_PATH, chunksize=CHUNK_SIZE)
    for i, chunk in enumerate(reader):
        total_rows += len(chunk)
        all_users.update(chunk["user_id"].unique())

        # 核心一行：按用户筛选，而不是按行随机抽
        sampled = chunk[chunk["user_id"] % SAMPLE_MOD == 0]
        kept_rows += len(sampled)
        kept_users.update(sampled["user_id"].unique())

        # 第一块写表头并新建文件，之后只追加数据
        sampled.to_csv(OUT_PATH, mode="a", header=(i == 0), index=False)
        print(f"已处理 {total_rows:,} 行，保留 {kept_rows:,} 行，用时 {time.time() - start:.0f} 秒")

    print("—— 完成 ——")
    print(f"原始：{total_rows:,} 行，{len(all_users):,} 个用户")
    print(f"抽样：{kept_rows:,} 行，{len(kept_users):,} 个用户")
    print(f"行数占比 {kept_rows / total_rows:.1%}，用户占比 {len(kept_users) / len(all_users):.1%}")


if __name__ == "__main__":
    main()