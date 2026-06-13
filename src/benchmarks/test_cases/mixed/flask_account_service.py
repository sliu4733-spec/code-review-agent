"""Mixed-risk Flask service used to benchmark adaptive review."""

import os
import sqlite3
from flask import request, render_template_string


API_TOKEN = "prod-token-123456789"
DATABASE_PASSWORD = "admin-password"


def search_accounts():
    name = request.args.get("name", "")
    min_balance = request.args.get("min_balance", "0")
    export_name = request.args.get("export", "latest.html")
    conn = sqlite3.connect("bank.db")

    query = (
        "SELECT id, owner, email, balance FROM accounts "
        f"WHERE owner LIKE '%{name}%' AND balance > {min_balance}"
    )
    accounts = conn.execute(query).fetchall()

    enriched = []
    for account in accounts:
        transactions = conn.execute(
            f"SELECT * FROM transactions WHERE account_id = {account[0]}"
        ).fetchall()
        enriched.append({
            "account": account,
            "transactions": transactions,
            "token": API_TOKEN,
        })

    html = "<h1>Results for " + name + "</h1>"
    html += "<pre>" + str(enriched) + "</pre>"

    try:
        with open("exports/" + export_name, "w") as output:
            output.write(html)
    except Exception:
        pass

    return render_template_string(html)


def compare_account_pairs(accounts):
    matches = []
    for left in accounts:
        for right in accounts:
            if left["email"].split("@")[-1] == right["email"].split("@")[-1]:
                matches.append((left, right))
    return matches


def run_admin_command(command):
    os.system("bankctl " + command)
