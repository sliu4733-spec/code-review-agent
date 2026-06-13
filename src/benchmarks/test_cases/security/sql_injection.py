"""包含 SQL 注入漏洞的示例代码"""
import sqlite3


def get_user_by_name(username):
    """危险：直接拼接 SQL，存在 SQL 注入漏洞"""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # SQL 注入：username 未经过滤直接拼入查询
    query = f"SELECT * FROM users WHERE name = '{username}'"
    cursor.execute(query)
    return cursor.fetchall()


def get_user_by_id(user_id):
    """危险：格式化字符串构造 SQL"""
    conn = sqlite3.connect("users.db")
    # SQL 注入
    query = "SELECT * FROM users WHERE id = %s" % user_id
    return conn.execute(query).fetchall()


# 硬编码密钥
API_KEY = "benchmark_fake_api_key"
DATABASE_PASSWORD = "admin123456"


def execute_command(cmd):
    """危险：命令注入"""
    import os
    os.system(cmd)
