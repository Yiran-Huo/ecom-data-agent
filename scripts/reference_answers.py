"""
第 6 步测试题的标准答案：用手写的 SQL 算出来，拿去和 Agent 的回答对比。
（第 8 步做评测集时，这些题和 SQL 可以直接搬进 eval/questions.jsonl）

运行方式（在项目根目录）：python scripts/reference_answers.py
"""
import pandas as pd

from db_utils import get_conn

QUESTIONS = [
    (
        "Q1. 10 月一共有多少次购买？涉及多少个购买用户？",
        """
        SELECT COUNT(*) AS purchases, COUNT(DISTINCT user_id) AS buyers
        FROM events
        WHERE event_type = 'purchase' AND price > 0
        """,
    ),
    (
        "Q2. GMV 最高的 10 个品牌是哪些？（排除品牌为空的商品）",
        """
        SELECT p.brand, ROUND(SUM(e.price), 2) AS gmv, COUNT(*) AS purchases
        FROM events e JOIN products p ON e.product_id = p.product_id
        WHERE e.event_type = 'purchase' AND e.price > 0 AND p.brand IS NOT NULL
        GROUP BY p.brand
        ORDER BY gmv DESC
        LIMIT 10
        """,
    ),
    (
        "Q3. 10 月每天的 GMV 趋势（Agent 应该画折线图；这里只列数字）",
        """
        SELECT DATE(event_time) AS day, ROUND(SUM(price), 2) AS gmv
        FROM events
        WHERE event_type = 'purchase' AND price > 0
        GROUP BY day
        ORDER BY day
        """,
    ),
    (
        "Q4. 用户级「浏览 → 加购 → 购买」转化率（购买 = 有效购买，price > 0）",
        """
        SELECT
            COUNT(DISTINCT CASE WHEN event_type = 'view' THEN user_id END)     AS viewers,
            COUNT(DISTINCT CASE WHEN event_type = 'cart' THEN user_id END)     AS carters,
            COUNT(DISTINCT CASE WHEN event_type = 'purchase' AND price > 0 THEN user_id END) AS buyers,
            ROUND(COUNT(DISTINCT CASE WHEN event_type = 'cart' THEN user_id END)
                  / COUNT(DISTINCT CASE WHEN event_type = 'view' THEN user_id END), 4) AS view_to_cart,
            ROUND(COUNT(DISTINCT CASE WHEN event_type = 'purchase' AND price > 0 THEN user_id END)
                  / COUNT(DISTINCT CASE WHEN event_type = 'view' THEN user_id END), 4) AS view_to_purchase
        FROM events
        """,
    ),
    (
        "Q5. 哪个一级品类的「浏览 → 购买」用户转化率最高？（排除没有品类的商品）",
        """
        SELECT c.level1,
               COUNT(DISTINCT CASE WHEN e.event_type = 'view' THEN e.user_id END) AS viewers,
               COUNT(DISTINCT CASE WHEN e.event_type = 'purchase' AND e.price > 0 THEN e.user_id END) AS buyers,
               ROUND(COUNT(DISTINCT CASE WHEN e.event_type = 'purchase' AND e.price > 0 THEN e.user_id END)
                     / NULLIF(COUNT(DISTINCT CASE WHEN e.event_type = 'view' THEN e.user_id END), 0), 4) AS conversion
        FROM events e
        JOIN products p ON e.product_id = p.product_id
        JOIN categories c ON p.category_id = c.category_id
        WHERE c.level1 IS NOT NULL
        GROUP BY c.level1
        ORDER BY conversion DESC
        """,
    ),
]


def fmt(x: float) -> str:
    """数字显示成 1,234.56 / 0.1049 这样，去掉多余的 0。"""
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def main():
    conn = get_conn("ro")
    try:
        for title, sql in QUESTIONS:
            print(f"\n{title}")
            with conn.cursor() as cur:
                cur.execute(sql)
                df = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
            # MySQL 返回的小数是 Decimal 类型，先转成 float
            for col in df.columns:
                if df[col].map(lambda v: hasattr(v, "is_finite")).any():
                    df[col] = df[col].astype(float)
            if len(df) > 12:   # 每日数据太长，只显示首尾和汇总
                print(pd.concat([df.head(5), df.tail(5)]).to_string(index=False, float_format=fmt))
                print(f"（共 {len(df)} 天，GMV 最高：{df.loc[df['gmv'].idxmax(), 'day']}，"
                      f"最低：{df.loc[df['gmv'].idxmin(), 'day']}）")
            else:
                print(df.to_string(index=False, float_format=fmt))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
