/** 包含可维护性问题的 React TypeScript 组件 */

import React, { useState, useEffect } from 'react';

// 上帝组件：承担太多职责
const UserDashboard: React.FC = () => {
    const [users, setUsers] = useState<any[]>([]);
    const [orders, setOrders] = useState<any>([]);
    const [notifications, setNotifications] = useState<any>([]);
    const [profile, setProfile] = useState<any>(null);
    const [theme, setTheme] = useState('light');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<any>(null);

    // 过长函数：100+ 行的渲染
    return (
        <div>
            <div>
                <div>
                    <div>
                        {/* 过深嵌套：4 层 div 嵌套 */}
                        <h1>Dashboard</h1>
                    </div>
                </div>
            </div>
            {users.map((u) => (
                <div key={u.id}>
                    {/* 重复的内联样式 */}
                    <span style={{ color: '#333', fontSize: '14px', fontWeight: 'bold' }}>
                        {u.name}
                    </span>
                    <span style={{ color: '#333', fontSize: '14px', fontWeight: 'bold' }}>
                        {u.email}
                    </span>
                </div>
            ))}
        </div>
    );
};

// 魔法数字
function calculateDiscount(price: number): number {
    if (price > 1000) {
        return price * 0.85;
    } else if (price > 500) {
        return price * 0.9;
    } else if (price > 100) {
        return price * 0.95;
    }
    return price;
}

// 空 catch + any 类型滥用
async function fetchData(url: string): Promise<any> {
    try {
        const response = await fetch(url);
        const data: any = await response.json();
        return data;
    } catch (e) {
        // 空 catch：静默吞掉错误
    }
}

export default UserDashboard;
