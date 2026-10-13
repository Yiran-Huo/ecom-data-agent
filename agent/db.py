"""
用只读账号连接 MySQL。Agent 的所有代码都只通过这里的 engine 访问数据库。

测试：在项目根目录运行  python -m agent.db
"""
import os

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine

load_dotenv()

_missing = [k for k in ["DB_HOST", "DB_PORT", "DB_NAME", "DB_RO_USER", "DB_RO_PASSWORD"] if not os.getenv(k)]
if _missing:
    raise RuntimeError(f".env 里缺少这些变量：{', '.join(_missing)}")

# 用 URL.create 分别传参数，密码里有 ! @ # 等特殊字符也不会出错
# （直接拼字符串 "mysql+pymysql://user:pa@ss@host/..." 时，@ 会被误认为分隔符）
DB_URL = URL.create(
    drivername="mysql+pymysql",
    username=os.getenv("DB_RO_USER"),
    password=os.getenv("DB_RO_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT")),
    database=os.getenv("DB_NAME"),
    query={"charset": "utf8mb4"},
)

# pool_pre_ping：每次取连接前先检查连接是否还活着，避免 MySQL 闲置断开后报错
engine = create_engine(DB_URL, pool_pre_ping=True)


if __name__ == "__main__":
    from sqlalchemy import text

    with engine.connect() as conn:
        user = conn.execute(text("SELECT CURRENT_USER()")).scalar()
        n = conn.execute(text("SELECT COUNT(*) FROM events")).scalar()
    print(f"当前账号：{user}")
    print(f"events 表行数：{n:,}")
