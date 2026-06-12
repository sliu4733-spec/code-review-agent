/** 包含 SQL 注入和 XSS 漏洞的 Node.js 代码 */
const express = require('express');
const mysql = require('mysql');
const app = express();

// 硬编码数据库密码
const DB_PASSWORD = 'admin123456';
const API_KEY = 'sk-abc123def456ghi789jkl012mno345pqr678stu';

app.get('/user', (req, res) => {
    const name = req.query.name;
    // SQL 注入：直接拼接用户输入
    const query = `SELECT * FROM users WHERE name = '${name}'`;
    db.query(query, (err, result) => {
        res.json(result);
    });
});

app.get('/hello', (req, res) => {
    const name = req.query.name || 'Guest';
    // XSS：未转义的用户输入直接渲染
    res.send(`<h1>Hello, ${name}!</h1>`);
});

app.get('/search', (req, res) => {
    const keyword = req.query.q;
    // XSS：innerHTML 直接写入
    res.send(`
        <html><body>
            <div id="result"></div>
            <script>
                document.getElementById('result').innerHTML = '${keyword}';
            </script>
        </body></html>
    `);
});
