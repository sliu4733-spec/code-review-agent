"""包含 XSS 和路径遍历漏洞的示例代码"""
from flask import Flask, request, render_template_string

app = Flask(__name__)


@app.route("/hello")
def hello():
    """危险：反射型 XSS — 用户输入直接渲染"""
    name = request.args.get("name", "")
    return f"<h1>Hello, {name}!</h1>"


@app.route("/profile")
def profile():
    """危险：未转义的用户数据"""
    username = request.args.get("user", "")
    template = "<div>Welcome, " + username + "</div>"
    return render_template_string(template)


@app.route("/download")
def download():
    """危险：路径遍历 — 用户输入直接拼入文件路径"""
    filename = request.args.get("file", "")
    return open("/var/www/uploads/" + filename).read()


@app.route("/view")
def view():
    """危险：路径遍历"""
    import os
    path = request.args.get("path", "")
    # ../ 可以访问任意文件
    with open(os.path.join("/opt/data/", path)) as f:
        return f.read()
