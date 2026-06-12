"""包含 N+1 查询和 O(n²) 性能问题的示例代码"""


def get_users_with_orders():
    """N+1 查询问题：循环内查询数据库"""
    users = db.query("SELECT * FROM users")
    result = []
    for user in users:
        # 每个 user 一次查询 = N+1 问题
        orders = db.query(f"SELECT * FROM orders WHERE user_id = {user.id}")
        result.append({"user": user, "orders": orders})
    return result


def find_duplicates(items):
    """O(n²)：用列表遍历代替哈希表"""
    duplicates = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i] == items[j] and items[i] not in duplicates:
                duplicates.append(items[i])
    return duplicates


def check_intersection(list_a, list_b):
    """O(n*m)：嵌套循环检查交集"""
    common = []
    for a in list_a:
        for b in list_b:
            if a == b:
                common.append(a)
    return common
