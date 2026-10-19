"""
用只读账号连接 MySQL。Agent 的所有代码都只通过这里拿数据库连接。

和之前相比多了一件事：可以切换「当前数据库」。
    平时：连 .env 里的 DB_NAME（ecom，真实数据）
    评测：连场景库 ecom_s_d01 等（在真实数据上注入了已知原因的异动，见 scripts/build_scenarios.py）
Agent 和分析工具都只调用 get_engine()，不需要知道自己连的是哪个库。

    from agent.db import get_engine, use_database
    engine = get_engine()                      # 当前数据库
    with use_database("ecom_s_d01"):           # 在这个 with 里面，get_engine() 都指向场景库
        ...

测试：在项目根目录运行  python -m agent.db
"""
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine

load_dotenv()

REQUIRED = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_RO_USER", "DB_RO_PASSWORD"]

# 当前数据库的名字。用环境变量 ECOM_DB 保存，这样 run_python 启动的子进程也会连到同一个库
_ENV_KEY = "ECOM_DB"
_engines = {}   # 每个库一个 engine，建一次反复用


def base_database() -> str:
    """真实数据所在的库（.env 里的 DB_NAME）。"""
    return os.getenv("DB_NAME", "ecom")


def current_database() -> str:
    return os.getenv(_ENV_KEY) or base_database()


def get_engine(database: str | None = None):
    """返回某个库的只读 engine；不传参数就是当前数据库。第一次调用时才真正创建连接。"""
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        raise RuntimeError(f".env 里缺少这些变量：{', '.join(missing)}")
    name = database or current_database()
    if name not in _engines:
        # 用 URL.create 分别传参数，密码里有 ! @ # 等特殊字符也不会出错
        url = URL.create(
            drivername="mysql+pymysql",
            username=os.getenv("DB_RO_USER"),
            password=os.getenv("DB_RO_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT")),
            database=name,
            query={"charset": "utf8mb4"},
        )
        # pool_pre_ping：每次取连接前先检查连接是否还活着，避免 MySQL 闲置断开后报错
        _engines[name] = create_engine(url, pool_pre_ping=True)
    return _engines[name]


@contextmanager
def use_database(name: str):
    """临时切换当前数据库（评测时用）。退出 with 之后自动切回原来的库。"""
    old = os.environ.get(_ENV_KEY)
    os.environ[_ENV_KEY] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = old


def __getattr__(name):
    # 兼容旧写法 from agent.db import engine（lesson 文件和旧脚本在用）
    if name == "engine":
        return get_engine()
    raise AttributeError(name)


if __name__ == "__main__":
    from sqlalchemy import text

    with get_engine().connect() as conn:
        user = conn.execute(text("SELECT CURRENT_USER()")).scalar()
        db = conn.execute(text("SELECT DATABASE()")).scalar()
        n = conn.execute(text("SELECT COUNT(*) FROM events")).scalar()
    print(f"当前账号：{user}，当前库：{db}")
    print(f"events 表行数：{n:,}")
