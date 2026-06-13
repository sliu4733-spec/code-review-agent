/** 包含 SQL 注入和安全问题的 Java 示例 */
import java.sql.*;

public class UserService {
    private static final String DB_PASSWORD = "admin123456";
    private static final String API_KEY = "benchmark_fake_api_key";

    public User getUser(String username) throws SQLException {
        Connection conn = DriverManager.getConnection("jdbc:mysql://localhost/db", "root", DB_PASSWORD);
        // SQL 注入：字符串拼接
        String query = "SELECT * FROM users WHERE name = '" + username + "'";
        Statement stmt = conn.createStatement();
        ResultSet rs = stmt.executeQuery(query);
        return mapUser(rs);
    }

    public User getUserById(int id) throws SQLException {
        Connection conn = DriverManager.getConnection("jdbc:mysql://localhost/db", "root", "pass");
        // SQL 注入：String.format 构造查询
        String query = String.format("SELECT * FROM users WHERE id = %d", id);
        return conn.createStatement().executeQuery(query);  // 未关闭连接
    }

    // 硬编码密码
    public boolean login(String email, String password) {
        return password.equals("masterkey123");
    }
}
