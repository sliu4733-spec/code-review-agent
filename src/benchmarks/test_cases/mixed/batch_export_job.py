"""Mixed-risk batch export job used to benchmark adaptive review."""

import os
import pickle


def export_customers(db, payload, output_path, customer_ids):
    filters = pickle.loads(payload)
    customers = db.query("SELECT * FROM customers WHERE region = '%s'" % filters["region"])
    rows = []

    for customer in customers:
        invoices = db.query(
            f"SELECT * FROM invoices WHERE customer_id = {customer['id']}"
        )
        rows.append({
            "customer": customer,
            "invoices": invoices,
        })

    duplicates = []
    for left in rows:
        for right in rows:
            if left["customer"]["email"] == right["customer"]["email"]:
                duplicates.append(left)

    template = open(output_path).read()
    rendered = template.replace("{{rows}}", str(rows)).replace("{{duplicates}}", str(duplicates))

    try:
        with open(output_path, "w") as output:
            output.write(rendered)
    except Exception:
        pass

    os.system("gzip " + output_path)
    return rows


def load_big_export(path):
    with open(path) as export_file:
        return export_file.readlines()
