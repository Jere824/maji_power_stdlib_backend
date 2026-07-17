import csv
import io
from app.auth import require_roles
from app.database import rows_to_dicts
from app.validators import validate_billing_period, validate_service_type

OFFICER_ROLES = {"billing_officer", "admin"}


def overdue_accounts(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    rows = conn.execute(
        """
        SELECT b.id AS bill_id, b.billing_period, b.due_date, b.total_due, b.status,
               ua.account_number, ua.meter_number, ua.service_type, ua.address,
               u.name AS customer_name, u.phone AS customer_phone,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.bill_id=b.id AND p.status='confirmed'), 0) AS paid_amount
        FROM bills b
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE b.status = 'overdue'
           OR (b.status IN ('issued','partially_paid') AND date(b.due_date) < date('now'))
        ORDER BY b.due_date ASC
        """
    ).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["balance"] = round(float(item["total_due"]) - float(item["paid_amount"] or 0), 2)
    return {"items": items, "count": len(items)}


def daily_collections(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    date_value = query.get("date", [None])[0]
    params = []
    date_filter = "date('now')"
    if date_value:
        date_filter = "date(?)"
        params.append(date_value)

    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
        FROM payments
        WHERE status = 'confirmed' AND date(paid_at) = {date_filter}
        """,
        params,
    ).fetchone()
    by_method = rows_to_dicts(conn.execute(
        f"""
        SELECT payment_method, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
        FROM payments
        WHERE status = 'confirmed' AND date(paid_at) = {date_filter}
        GROUP BY payment_method
        ORDER BY payment_method
        """,
        params,
    ).fetchall())
    return {
        "date": date_value or "today",
        "confirmed_payment_count": row["count"],
        "collections_total": round(float(row["total"] or 0), 2),
        "by_payment_method": by_method,
    }


def payment_status_summary(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    rows = rows_to_dicts(conn.execute(
        """
        SELECT status, COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total_amount
        FROM payments
        GROUP BY status
        ORDER BY status
        """
    ).fetchall())
    return {"items": rows}


def revenue_by_service_type(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    billing_period = query.get("billing_period", [None])[0]
    service_type = query.get("service_type", [None])[0]

    sql = """
        SELECT ua.service_type,
               COUNT(DISTINCT b.id) AS bill_count,
               COALESCE(SUM(b.total_due), 0) AS billed_amount,
               COALESCE(SUM((SELECT SUM(p.amount) FROM payments p WHERE p.bill_id=b.id AND p.status='confirmed')), 0) AS collected_amount
        FROM bills b
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        WHERE 1 = 1
    """
    params = []
    if billing_period:
        validate_billing_period(billing_period)
        sql += " AND b.billing_period = ?"
        params.append(billing_period)
    if service_type:
        validate_service_type(service_type)
        sql += " AND ua.service_type = ?"
        params.append(service_type)
    sql += " GROUP BY ua.service_type ORDER BY ua.service_type"

    return {"items": rows_to_dicts(conn.execute(sql, params).fetchall())}


def collections_csv(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    rows = rows_to_dicts(conn.execute(
        """
        SELECT p.id AS payment_id, p.amount, p.payment_method, p.status, p.provider_reference,
               p.receipt_id, p.paid_at, b.id AS bill_id, b.billing_period,
               ua.account_number, ua.service_type, u.name AS customer_name
        FROM payments p
        JOIN bills b ON b.id = p.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        ORDER BY p.created_at DESC, p.id DESC
        """
    ).fetchall())

    output = io.StringIO()
    fieldnames = [
        "payment_id", "amount", "payment_method", "status", "provider_reference",
        "receipt_id", "paid_at", "bill_id", "billing_period", "account_number",
        "service_type", "customer_name",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()
