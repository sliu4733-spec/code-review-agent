/** 包含 N+1 查询和 O(n²) 性能问题的 TypeScript 代码 */

interface User {
    id: number;
    name: string;
}

interface Order {
    id: number;
    userId: number;
    amount: number;
}

async function getUserOrders(users: User[]): Promise<Array<{ user: User; orders: Order[] }>> {
    const result = [];
    // N+1 查询：循环内逐个查询
    for (const user of users) {
        const orders = await db.query(`SELECT * FROM orders WHERE user_id = ${user.id}`);
        result.push({ user, orders });
    }
    return result;
}

function findDuplicates(items: number[]): number[] {
    const duplicates: number[] = [];
    // O(n²)：双重循环
    for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
            if (items[i] === items[j] && !duplicates.includes(items[i])) {
                duplicates.push(items[i]);
            }
        }
    }
    return duplicates;
}

async function fetchUserData(userId: number): Promise<any> {
    // 三个独立请求却串行 await
    const user = await api.get(`/users/${userId}`);
    const orders = await api.get(`/users/${userId}/orders`);
    const profile = await api.get(`/users/${userId}/profile`);
    return { user, orders, profile };
}

function generateToken(): string {
    // 用 Math.random() 生成安全令牌（不安全）
    return Math.random().toString(36).substring(2);
}
