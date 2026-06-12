// 包含安全问题的 Go 示例
package main

import (
    "crypto/md5"
    "database/sql"
    "fmt"
    "math/rand"
    "net/http"
    "os"
)

var apiKey = "sk-abc123def456ghi789"

func getUser(db *sql.DB, username string) (*User, error) {
    // SQL 注入：fmt.Sprintf 拼接
    query := fmt.Sprintf("SELECT * FROM users WHERE name = '%s'", username)
    return db.Query(query)  // 未使用参数化查询
}

func downloadFile(w http.ResponseWriter, r *http.Request) {
    filename := r.URL.Query().Get("file")
    // 路径遍历
    data, _ := os.ReadFile("/var/www/uploads/" + filename)
    w.Write(data)
}

func hashPassword(password string) string {
    // MD5 用于密码哈希
    hash := md5.Sum([]byte(password))
    return fmt.Sprintf("%x", hash)
}

func generateToken() string {
    // math/rand 不安全
    return fmt.Sprintf("%d", rand.Int63())
}
