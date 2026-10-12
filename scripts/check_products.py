import pandas as pd

df = pd.read_csv("data/raw/2019-Oct.csv")
print(f"共 {len(df):,} 行")

# 先加一列：这一行的 brand 是不是空的（True/False）
df["brand_missing"] = df["brand"].isna()

# 按商品分组，每个商品统计这几样
g = df.groupby("product_id").agg(
    n_brand=("brand", "nunique"),            # 有几个不同的品牌（不算空值）
    n_cat=("category_id", "nunique"),        # 有几个不同的品类 ID
    n_price=("price", "nunique"),            # 出现过几个不同的价格
    brand_any_missing=("brand_missing", "max"),  # 有没有哪一行品牌是空的（有一个 True 就是 True）
)

print(f"商品总数：{len(g):,}")
print(f"品牌不一致（≥2 个品牌）：{(g['n_brand'] > 1).sum():,}")
print(f"品牌时有时无（有 1 个品牌，但部分行为空）：{((g['n_brand'] == 1) & g['brand_any_missing']).sum():,}")
print(f"品牌完全缺失：{(g['n_brand'] == 0).sum():,}")
print(f"品类 ID 不一致：{(g['n_cat'] > 1).sum():,}")
print(f"价格变过：{(g['n_price'] > 1).sum():,}（占商品总数 {(g['n_price'] > 1).mean():.1%}）")

# 挑一个价格变过的商品，看看它的价格是怎么变的
example = g[g["n_price"] > 1].index[0]
print(f"\n示例商品 {example} 的价格变化：")
print(df[df["product_id"] == example][["event_time", "price"]].drop_duplicates("price").head(10))