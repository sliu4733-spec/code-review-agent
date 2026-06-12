"""包含上帝类和过长方法的示例代码"""


class UserManager:
    """上帝类：承担用户管理、邮件发送、数据库、缓存等多重职责"""

    def create_user(self, name, email, password):
        # 数据库操作
        user = {"id": 1, "name": name, "email": email, "password": password}
        self.db.save("users", user)
        # 缓存更新
        self.cache.set(f"user:{user['id']}", user)
        # 发送邮件
        self.email.send(email, "欢迎注册", f"你好 {name}！")
        # 日志
        self.logger.info(f"用户 {name} 注册成功")
        # 统计分析
        self.analytics.track("user_signup", {"name": name})
        return user

    def delete_user(self, user_id):
        user = self.db.get("users", user_id)
        if user:
            self.db.delete("users", user_id)
            self.cache.delete(f"user:{user_id}")
            self.email.send(user["email"], "账户已删除", "再见！")
            self.logger.info(f"用户 {user_id} 已删除")

    def update_profile(self, user_id, data):
        user = self.db.get("users", user_id)
        user.update(data)
        self.db.save("users", user)
        self.cache.set(f"user:{user_id}", user)

    def send_notification(self, user_id, message):
        user = self.db.get("users", user_id)
        self.email.send(user["email"], "通知", message)

    def generate_report(self, user_id):
        user = self.db.get("users", user_id)
        orders = self.db.query(f"orders:{user_id}")
        total = sum(o["amount"] for o in orders)
        return {"user": user, "total_spent": total, "order_count": len(orders)}

    def login(self, email, password):
        user = self.db.query_one("users", email=email)
        if user and user["password"] == password:
            self.cache.set(f"session:{user['id']}", True)
            return True
        return False

    def logout(self, user_id):
        self.cache.delete(f"session:{user_id}")


def process_order(order_data):
    """过长方法：做了太多事情"""
    # 验证数据
    if not order_data.get("items"):
        raise ValueError("空订单")
    if not order_data.get("user_id"):
        raise ValueError("缺少用户")

    # 查询库存
    for item in order_data["items"]:
        stock = check_stock(item["id"])
        if stock < item["quantity"]:
            raise Exception("库存不足")

    # 计算价格
    total = 0
    for item in order_data["items"]:
        product = get_product(item["id"])
        total += product["price"] * item["quantity"]

    # 折扣计算
    if total > 500:
        total *= 0.9
    elif total > 200:
        total *= 0.95

    # 保存订单
    order = {
        "user_id": order_data["user_id"],
        "items": order_data["items"],
        "total": total,
        "status": "pending",
        "created_at": "now",
    }
    save_order(order)

    # 更新库存
    for item in order_data["items"]:
        deduct_stock(item["id"], item["quantity"])

    # 发送通知
    send_notification(order_data["user_id"], f"订单已创建，金额：{total}")

    # 写日志
    log_order(order)

    return order
