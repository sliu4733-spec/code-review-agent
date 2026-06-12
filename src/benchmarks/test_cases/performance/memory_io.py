"""包含内存和 I/O 性能问题的示例代码"""


def load_all_logs():
    """内存问题：全量加载大文件"""
    with open("server.log") as f:
        lines = f.readlines()  # 一次性加载全部
    return [line for line in lines if "ERROR" in line]


def process_data(items):
    """不必要的对象创建"""
    result = []
    for item in items:
        temp = {}
        temp["id"] = item.id
        temp["name"] = item.name.upper()
        temp["score"] = item.score * 2 + 100 - 50
        result.append(temp)
    return result


def fetch_all_users():
    """未分页查询"""
    return User.objects.all()  # 全量加载


def compute_stats(data):
    """重复计算：循环内可提取的不变表达式"""
    results = []
    for item in data:
        # len(data) 每次循环都重新计算
        normalized = item / len(data)
        results.append(normalized)
    return results
