"""包含不安全反序列化和加密缺陷的示例代码"""
import pickle
import yaml
import hashlib
import random


def load_user_data(data):
    """危险：不安全的 pickle 反序列化"""
    return pickle.loads(data)


def load_config(path):
    """危险：yaml.load 可执行任意代码"""
    with open(path) as f:
        return yaml.load(f)


def hash_password(password):
    """危险：MD5 用于密码哈希"""
    return hashlib.md5(password.encode()).hexdigest()


def generate_token():
    """危险：random 用于安全令牌生成"""
    return ''.join(random.choice('abcdef0123456789') for _ in range(32))
