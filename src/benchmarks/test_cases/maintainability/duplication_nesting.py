"""包含重复代码、魔法数字和过深嵌套的示例代码"""


def calculate_shipping_fee(weight, distance, is_express):
    """魔法数字 + 过深嵌套"""
    fee = 0
    if weight < 5:
        if distance < 10:
            fee = 10
        else:
            if distance < 50:
                fee = 20
            else:
                if distance < 100:
                    fee = 30
                else:
                    fee = 50
    else:
        if weight < 20:
            if distance < 10:
                fee = 25
            else:
                if distance < 50:
                    fee = 40
                else:
                    fee = 60
        else:
            fee = 100

    if is_express:
        fee = fee * 1.5

    return fee


def calculate_tax(income, has_house, has_car):
    """重复逻辑块"""
    tax = 0
    base = income - 5000

    # 重复块 1
    if base > 0 and base <= 3000:
        tax += base * 0.03
    elif base > 3000 and base <= 12000:
        tax += base * 0.1 - 210
    elif base > 12000 and base <= 25000:
        tax += base * 0.2 - 1410

    # 重复块 2（几乎相同的结构）
    if has_house:
        house_tax_base = income * 0.7
        if house_tax_base > 0 and house_tax_base <= 3000:
            tax += house_tax_base * 0.03
        elif house_tax_base > 3000 and house_tax_base <= 12000:
            tax += house_tax_base * 0.1 - 210

    if has_car:
        car_tax_base = income * 0.5
        # 重复块 3（再次相同结构）
        if car_tax_base > 0 and car_tax_base <= 3000:
            tax += car_tax_base * 0.03
        elif car_tax_base > 3000 and car_tax_base <= 12000:
            tax += car_tax_base * 0.1 - 210

    return tax


# 重复常量定义
MAX_RETRY_COUNT = 3
RETRY_LIMIT = 3
RETRY_TIMES = 3


def connect_db(passwd):
    # 空 except
    try:
        return Database.connect(password=passwd)
    except:
        pass
