"""Seeds a demo workspace: a messy spreadsheet source, a SQLite ERP connection, a target, a mapping
and a published endpoint, plus an admin API key printed once.

    python -m databridge.demo
"""

import sqlite3
import sys
from pathlib import Path

from databridge.config import settings
from databridge.core.db import init_db

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _erp_db() -> Path:
    path = settings.data_dir / "demo_erp.db"
    if path.exists():
        return path
    con = sqlite3.connect(path)
    con.executescript("""
        create table customers(id integer primary key, name text not null, region text, created_on date);
        create table orders(order_id integer primary key, customer_id integer references customers(id),
                            order_date date, amount numeric(12,2), status varchar(20));
        insert into customers values (1,'Jane Doe','East','2025-01-10'),(2,'Ravi Kumar','West','2025-03-02'),
                                     (3,'Maria Lopez','South','2025-06-21');
        insert into orders values (10,1,'2026-08-01',120.50,'Shipped'),(11,2,'2026-08-03',99.99,'Pending'),
                                  (12,1,'2026-08-09',15,'Shipped'),(13,3,'2026-08-12',480,'Cancelled');
        create view v_order_summary as
            select c.name, c.region, count(*) orders, sum(o.amount) total
            from orders o join customers c on c.id = o.customer_id group by c.name, c.region;
    """)
    con.commit()
    con.close()
    return path


def main() -> None:
    init_db()
    from databridge.engine.mapper import MappingSpec
    from databridge.ingest.sheet_profile import suggest_profile
    from databridge.services import connections, endpoints, mappings, sources, targets

    if not (SAMPLES / "vendor_sales.xlsx").exists():
        import runpy

        runpy.run_path(str(SAMPLES / "make_samples.py"), run_name="__main__")
    if sources.list_sources():
        print("Demo data already present in", settings.data_dir.resolve())
        return

    sales = (SAMPLES / "vendor_sales.xlsx").read_bytes()
    src = sources.create_upload_source("Vendor sales", "vendor_sales.xlsx", sales,
                                       suggest_profile(sales, "vendor_sales.xlsx"))
    tmpl = (SAMPLES / "target_customer_orders.xlsx").read_bytes()
    tgt = targets.target_from_template("Customer orders", "target_customer_orders.xlsx", tmpl)
    targets.save_target(tgt.name, [
        {"name": "customer_id", "type": "string", "required": True, "description": "Vendor customer number"},
        {"name": "full_name", "type": "string", "required": True},
        {"name": "region", "type": "string"},
        {"name": "order_date", "type": "date", "required": True},
        {"name": "amount_usd", "type": "decimal(12,2)", "required": True},
        {"name": "status", "type": "string"},
    ], target_id=tgt.id)
    m = mappings.create_mapping("Vendor sales to customer orders", src.id, tgt.id)
    rules = [r for r in m.rules if r["target"] != "full_name"]
    rules.append({"target": "full_name", "formula": 'PROPER(TRIM([First Name])) & " " & [Last Name]'})
    rules = [{**r, "formula": "PROPER([Status])"} if r["target"] == "status" else r for r in rules]
    mappings.save_spec(m.id, MappingSpec(rules=rules, validations=[
        {"field": "status", "kind": "allowed", "value": "Shipped,Pending,Cancelled"}]))
    mappings.publish(m.id)
    endpoints.save_endpoint({"slug": "customer-orders", "name": "Customer orders", "mapping_id": m.id,
                             "description": "Monthly vendor sales, cleaned",
                             "params": [{"name": "region", "column": "region", "op": "eq"},
                                        {"name": "status", "column": "status", "op": "eq"},
                                        {"name": "q", "column": "full_name", "op": "contains"}]})
    connections.save_connection("Demo ERP (SQLite)", "database", {"dialect": "sqlite", "database": str(_erp_db())})
    connections.save_connection("Samples folder", "filesystem", {"protocol": "file", "root": str(SAMPLES),
                                                                 "file_pattern": "*.xlsx"})
    from databridge.services import users

    admin_pw = None
    if users.count_users() == 0:
        _, admin_pw = users.create_user("admin", "admin", created_by="demo", full_name="Demo Admin")
    _, raw = endpoints.create_api_key("Demo admin key", ["*"])
    print("Demo workspace created in", settings.data_dir.resolve())
    print("Admin API key (shown once):", raw)
    if admin_pw:
        print(f"Studio sign-in: admin / {admin_pw}  (temporary; you'll choose a new password at first sign-in)")
    print(f'Try: curl -H "X-API-Key: {raw}" "{settings.public_base_url}/api/v1/data/customer-orders?region=East"')


if __name__ == "__main__":
    sys.exit(main())
