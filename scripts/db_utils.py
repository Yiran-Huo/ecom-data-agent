"""
数据库连接的公共函数，load_data.py 和 verify_db.py 都会用到。

两种身份：
  admin  —— 你自己的管理员账号，用来建表、导入数据（DB_ADMIN_USER / DB_ADMIN_PASSWORD）
  ro     —— 给 Agent 用的只读账号（DB_RO_USER / DB_RO_PASSWORD）
"""
import os
import re
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

# 不管从哪个目录运行脚本，都以项目根目录（scripts/ 的上一级）为准
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

REQUIRED = {
    "admin": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD"],
    "ro": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_RO_USER", "DB_RO_PASSWORD"],
}


def get_conn(role: str = "admin"):
    """按身份连接 MySQL，返回 pymysql 连接。缺少配置或连不上时，打印能看懂的提示后退出。"""
    missing = [k for k in REQUIRED[role] if not os.getenv(k)]
    if missing:
        sys.exit(f"❌ .env 里缺少这些变量：{', '.join(missing)}（参考文档第 2 步 2.5 的模板）")

    user_key = "DB_ADMIN_USER" if role == "admin" else "DB_RO_USER"
    pass_key = "DB_ADMIN_PASSWORD" if role == "admin" else "DB_RO_PASSWORD"
    try:
        return pymysql.connect(
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT")),
            user=os.getenv(user_key),
            password=os.getenv(pass_key),
            database=os.getenv("DB_NAME"),
            charset="utf8mb4",
            autocommit=False,
        )
    except RuntimeError as e:
        # MySQL 8 默认的密码认证方式需要 cryptography 这个包
        if "cryptography" in str(e):
            sys.exit("❌ 缺少 cryptography 包，请先运行：pip install cryptography")
        raise
    except pymysql.err.OperationalError as e:
        code = e.args[0]
        hints = {
            1045: f"用户名或密码不对：检查 .env 里的 {user_key} / {pass_key}",
            1044: f"账号 {os.getenv(user_key)} 没有访问库 {os.getenv('DB_NAME')} 的权限",
            1049: f"数据库 {os.getenv('DB_NAME')} 不存在：先在 MySQL 里 CREATE DATABASE",
            2003: "连不上 MySQL：服务启动了吗？试试 brew services start mysql@8.4",
        }
        sys.exit(f"❌ 连接失败（{code}）：{hints.get(code, e.args[1])}")


def run_sql_file(conn, path: Path):
    """执行一个 .sql 文件里的所有语句（去掉 -- 注释后按分号切分）。"""
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"^\s*--.*$", "", text, flags=re.MULTILINE)  # 去掉整行注释
    statements = [s.strip() for s in text.split(";") if s.strip()]
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    conn.commit()
    return len(statements)
