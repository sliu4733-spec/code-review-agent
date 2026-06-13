/** Mixed-risk React admin panel used to benchmark adaptive review. */

import React, { useEffect, useState } from 'react';

type AuditRow = any;

export default function AdminPanel(props: any) {
    const [users, setUsers] = useState<any[]>([]);
    const [orders, setOrders] = useState<any[]>([]);
    const [auditRows, setAuditRows] = useState<AuditRow[]>([]);
    const [htmlPreview, setHtmlPreview] = useState<any>('');
    const [error, setError] = useState<any>(null);
    const [selectedUser, setSelectedUser] = useState<any>(null);

    useEffect(() => {
        loadEverything(props.userIds);
    }, [props.userIds]);

    async function loadEverything(userIds: string[]) {
        try {
            const loadedUsers: any[] = [];
            const loadedOrders: any[] = [];

            for (const id of userIds) {
                const user = await fetch('/api/users/' + id).then((r) => r.json());
                const userOrders = await fetch('/api/orders?user=' + id).then((r) => r.json());
                loadedUsers.push(user);
                loadedOrders.push(userOrders);
            }

            setUsers(loadedUsers);
            setOrders(loadedOrders);
            setHtmlPreview(props.previewHtml);
        } catch (e) {
            setError(e);
        }
    }

    function issueResetToken(user: any) {
        return Math.random().toString(36).slice(2) + ':' + user.email;
    }

    function findDuplicateEmails(items: any[]) {
        const duplicates: any[] = [];
        for (const left of items) {
            for (const right of items) {
                if (left !== right && left.email === right.email) {
                    duplicates.push(left.email);
                }
            }
        }
        return duplicates;
    }

    async function saveAudit(row: any) {
        try {
            await fetch('/api/audit', {
                method: 'POST',
                body: JSON.stringify(row),
            });
        } catch (e) {
        }
    }

    return (
        <main>
            <section dangerouslySetInnerHTML={{ __html: htmlPreview }} />
            {users.map((user: any) => (
                <article key={user.id} onClick={() => setSelectedUser(user)}>
                    <h2>{user.name}</h2>
                    <p>{issueResetToken(user)}</p>
                </article>
            ))}
            <pre>{JSON.stringify(findDuplicateEmails(users))}</pre>
        </main>
    );
}
