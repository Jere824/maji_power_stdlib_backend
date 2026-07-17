from datetime import datetime, timedelta
from app.auth import require_roles, log_action
from app.database import rows_to_dicts, row_to_dict
from app.errors import APIError
from app.validators import (
    as_float,
    as_int,
    validate_billing_period,
    validate_date,
    validate_positive_amount,
    validate_service_type,
    validate_status,
    BILL_STATUSES,
)

OFFICER_ROLES = {"billing_officer", "admin"}


def get_latest_reading(conn, account_id):
    return conn.execute(
        """
        SELECT * FROM meter_readings
        WHERE utility_account_id = ?
        ORDER BY reading_date DESC, id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def get_active_tariff(conn, service_type):
    tariff = conn.execute(
        """
        SELECT * FROM tariffs
        WHERE service_type = ?
          AND (active_to IS NULL OR active_to >= date('now'))
        ORDER BY active_from DESC, id DESC
        LIMIT 1
        """,
        (service_type,),
    ).fetchone()
    if not tariff:
        raise APIError(f"No active tariff configured for {service_type}.", 400)
    return tariff


def get_confirmed_paid_amount(conn, bill_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid
        FROM payments
        WHERE bill_id = ? AND status = 'confirmed'
        """,
        (bill_id,),
    ).fetchone()
    return float(row["paid"] or 0)


def get_bill_balance(conn, bill):
    paid = get_confirmed_paid_amount(conn, bill["id"])
    return round(float(bill["total_due"]) - paid, 2)


def previous_balance_for_account(conn, account_id, billing_period):
    rows = conn.execute(
        """
        SELECT * FROM bills
        WHERE utility_account_id = ?
          AND billing_period < ?
          AND status IN ('issued', 'partially_paid', 'overdue')
        """,
        (account_id, billing_period),
    ).fetchall()

    balance = 0.0
    for bill in rows:
        balance += get_bill_balance(conn, bill)
    return round(max(balance, 0.0), 2)


def average_consumption(conn, account_id, exclude_reading_id=None):
    if exclude_reading_id:
        rows = conn.execute(
            """
            SELECT consumption FROM meter_readings
            WHERE utility_account_id = ? AND id != ?
            ORDER BY reading_date DESC, id DESC
            LIMIT 3
            """,
            (account_id, exclude_reading_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT consumption FROM meter_readings
            WHERE utility_account_id = ?
            ORDER BY reading_date DESC, id DESC
            LIMIT 3
            """,
            (account_id,),
        ).fetchall()
    if not rows:
        return None
    return sum(float(row["consumption"] or 0) for row in rows) / len(rows)


def compute_bill_preview(conn, account, billing_period, due_date=None):
    existing = conn.execute(
        """
        SELECT id, status FROM bills
        WHERE utility_account_id = ? AND billing_period = ?
        LIMIT 1
        """,
        (account["id"], billing_period),
    ).fetchone()
    if existing:
        return {
            "account_id": account["id"],
            "account_number": account["account_number"],
            "service_type": account["service_type"],
            "can_generate": False,
            "exception": "bill_already_exists",
            "existing_bill_id": existing["id"],
            "existing_status": existing["status"],
        }

    reading = get_latest_reading(conn, account["id"])
    if not reading:
        return {
            "account_id": account["id"],
            "account_number": account["account_number"],
            "service_type": account["service_type"],
            "can_generate": False,
            "exception": "missing_reading",
        }

    consumption = float(reading["consumption"] or 0)
    if consumption < 0:
        return {
            "account_id": account["id"],
            "account_number": account["account_number"],
            "service_type": account["service_type"],
            "can_generate": False,
            "exception": "negative_consumption",
            "meter_reading_id": reading["id"],
            "consumption": consumption,
        }

    tariff = get_active_tariff(conn, account["service_type"])
    previous_balance = previous_balance_for_account(conn, account["id"], billing_period)
    consumption_charge = round(consumption * float(tariff["price_per_unit"]), 2)
    fixed_charge = round(float(tariff["fixed_charge"] or 0), 2)
    tax_amount = round((consumption_charge + fixed_charge) * float(tariff["tax_rate"] or 0), 2)
    penalty_amount = 0.0
    total_due = round(previous_balance + consumption_charge + fixed_charge + tax_amount + penalty_amount, 2)

    avg = average_consumption(conn, account["id"], reading["id"])
    warning = None
    if avg and avg > 0 and consumption > avg * 3:
        warning = "outlier_consumption"

    return {
        "account_id": account["id"],
        "account_number": account["account_number"],
        "meter_number": account["meter_number"],
        "customer_id": account["customer_id"],
        "customer_name": account.get("customer_name"),
        "service_type": account["service_type"],
        "billing_period": billing_period,
        "meter_reading_id": reading["id"],
        "previous_reading": float(reading["previous_reading"]),
        "current_reading": float(reading["current_reading"]),
        "consumption": consumption,
        "previous_balance": previous_balance,
        "consumption_charge": consumption_charge,
        "fixed_charge": fixed_charge,
        "tax_amount": tax_amount,
        "penalty_amount": penalty_amount,
        "total_due": total_due,
        "due_date": due_date,
        "warning": warning,
        "can_generate": True,
    }


def dashboard(conn, user_id):
    require_roles(conn, user_id, OFFICER_ROLES)

    readings_received = conn.execute(
        "SELECT COUNT(*) AS count FROM meter_readings WHERE date(created_at) = date('now')"
    ).fetchone()["count"]

    overdue_accounts = conn.execute(
        """
        SELECT COUNT(DISTINCT utility_account_id) AS count
        FROM bills
        WHERE status = 'overdue'
           OR (status IN ('issued','partially_paid') AND date(due_date) < date('now'))
        """
    ).fetchone()["count"]

    daily_collections = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status = 'confirmed' AND date(paid_at) = date('now')
        """
    ).fetchone()["total"]

    payment_statuses = rows_to_dicts(conn.execute(
        "SELECT status, COUNT(*) AS count FROM payments GROUP BY status ORDER BY status"
    ).fetchall())

    bill_statuses = rows_to_dicts(conn.execute(
        "SELECT status, COUNT(*) AS count FROM bills GROUP BY status ORDER BY status"
    ).fetchall())

    return {
        "readings_received_today": readings_received,
        "bills_to_generate": count_accounts_ready_for_billing(conn),
        "overdue_accounts": overdue_accounts,
        "daily_collections_total": round(float(daily_collections or 0), 2),
        "payment_statuses": payment_statuses,
        "bill_statuses": bill_statuses,
    }


def count_accounts_ready_for_billing(conn):
    current_period = datetime.now().strftime("%Y-%m")
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM utility_accounts ua
        WHERE EXISTS (
            SELECT 1 FROM meter_readings mr WHERE mr.utility_account_id = ua.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM bills b
            WHERE b.utility_account_id = ua.id AND b.billing_period = ?
        )
        """,
        (current_period,),
    ).fetchone()
    return row["count"]


def preview_billing_run(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    billing_period = validate_billing_period(query.get("billing_period", [datetime.now().strftime("%Y-%m")])[0])
    service_type = validate_service_type(query.get("service_type", [None])[0])
    due_date = query.get("due_date", [None])[0]
    if due_date:
        validate_date(due_date, "due_date")
    else:
        due_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

    sql = """
        SELECT ua.*, u.name AS customer_name
        FROM utility_accounts ua
        JOIN users u ON u.id = ua.customer_id
    """
    params = []
    if service_type:
        sql += " WHERE ua.service_type = ?"
        params.append(service_type)
    sql += " ORDER BY ua.account_number"

    accounts = conn.execute(sql, params).fetchall()
    previews = [compute_bill_preview(conn, dict(account), billing_period, due_date) for account in accounts]
    return {
        "billing_period": billing_period,
        "due_date": due_date,
        "total_accounts_checked": len(previews),
        "ready_to_generate": len([p for p in previews if p.get("can_generate")]),
        "exceptions": [p for p in previews if not p.get("can_generate") or p.get("warning")],
        "items": previews,
    }


def create_bill_from_preview(conn, preview, created_by_user_id):
    cur = conn.execute(
        """
        INSERT INTO bills
        (utility_account_id, meter_reading_id, billing_period, previous_balance,
         consumption_charge, fixed_charge, tax_amount, penalty_amount, total_due,
         status, due_date, created_by_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)
        """,
        (
            preview["account_id"],
            preview["meter_reading_id"],
            preview["billing_period"],
            preview["previous_balance"],
            preview["consumption_charge"],
            preview["fixed_charge"],
            preview["tax_amount"],
            preview["penalty_amount"],
            preview["total_due"],
            preview["due_date"],
            created_by_user_id,
        ),
    )
    bill_id = cur.lastrowid
    log_action(conn, created_by_user_id, "bill_generated", f"bill:{bill_id}", f"Generated bill for account {preview['account_number']} period {preview['billing_period']}")
    return bill_id


def generate_one_bill(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    account_id = as_int(data.get("account_id"), "account_id")
    billing_period = validate_billing_period(data.get("billing_period"))
    due_date = data.get("due_date") or (datetime.now() + timedelta(days=int(data.get("due_days", 14)))).strftime("%Y-%m-%d")
    validate_date(due_date, "due_date")

    account = conn.execute(
        """
        SELECT ua.*, u.name AS customer_name
        FROM utility_accounts ua
        JOIN users u ON u.id = ua.customer_id
        WHERE ua.id = ?
        """,
        (account_id,),
    ).fetchone()
    if not account:
        raise APIError("Utility account not found.", 404)

    preview = compute_bill_preview(conn, dict(account), billing_period, due_date)
    if not preview.get("can_generate"):
        raise APIError("Bill cannot be generated because of an exception.", 400, preview)

    bill_id = create_bill_from_preview(conn, preview, user["id"])
    conn.commit()
    return {"message": "Bill generated successfully.", "bill_id": bill_id, "bill": get_bill_by_id(conn, user_id, bill_id)}


def generate_bulk_bills(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    billing_period = validate_billing_period(data.get("billing_period"))
    service_type = validate_service_type(data.get("service_type"))
    due_days = int(data.get("due_days", 14))
    due_date = data.get("due_date") or (datetime.now() + timedelta(days=due_days)).strftime("%Y-%m-%d")
    validate_date(due_date, "due_date")

    sql = """
        SELECT ua.*, u.name AS customer_name
        FROM utility_accounts ua
        JOIN users u ON u.id = ua.customer_id
    """
    params = []
    if service_type:
        sql += " WHERE ua.service_type = ?"
        params.append(service_type)
    sql += " ORDER BY ua.account_number"

    accounts = conn.execute(sql, params).fetchall()
    generated = []
    exceptions = []

    for account in accounts:
        preview = compute_bill_preview(conn, dict(account), billing_period, due_date)
        if preview.get("can_generate"):
            bill_id = create_bill_from_preview(conn, preview, user["id"])
            generated.append({"account_number": preview["account_number"], "bill_id": bill_id, "total_due": preview["total_due"]})
        else:
            exceptions.append(preview)

    conn.commit()
    return {
        "message": "Bulk billing run completed.",
        "billing_period": billing_period,
        "generated_count": len(generated),
        "exception_count": len(exceptions),
        "generated": generated,
        "exceptions": exceptions,
    }


def list_bills(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    status = validate_status(query.get("status", [None])[0], BILL_STATUSES)
    billing_period = query.get("billing_period", [None])[0]
    account_number = query.get("account_number", [None])[0]

    sql = """
        SELECT b.*, ua.account_number, ua.meter_number, ua.service_type, ua.address,
               u.name AS customer_name,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.bill_id = b.id AND p.status='confirmed'), 0) AS paid_amount
        FROM bills b
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE 1 = 1
    """
    params = []
    if status:
        sql += " AND b.status = ?"
        params.append(status)
    if billing_period:
        validate_billing_period(billing_period)
        sql += " AND b.billing_period = ?"
        params.append(billing_period)
    if account_number:
        sql += " AND ua.account_number LIKE ?"
        params.append(f"%{account_number}%")
    sql += " ORDER BY b.created_at DESC, b.id DESC"

    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    for row in rows:
        row["balance"] = round(float(row["total_due"]) - float(row["paid_amount"] or 0), 2)
    return {"items": rows, "count": len(rows)}


def get_bill_by_id(conn, user_id, bill_id):
    require_roles(conn, user_id, OFFICER_ROLES)
    bill = conn.execute(
        """
        SELECT b.*, ua.account_number, ua.meter_number, ua.service_type, ua.address,
               u.name AS customer_name, u.phone AS customer_phone,
               mr.previous_reading, mr.current_reading, mr.consumption
        FROM bills b
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        LEFT JOIN meter_readings mr ON mr.id = b.meter_reading_id
        WHERE b.id = ?
        """,
        (bill_id,),
    ).fetchone()
    if not bill:
        raise APIError("Bill not found.", 404)
    result = row_to_dict(bill)
    result["payments"] = rows_to_dicts(conn.execute(
        "SELECT * FROM payments WHERE bill_id = ? ORDER BY created_at DESC", (bill_id,)
    ).fetchall())
    result["paid_amount"] = get_confirmed_paid_amount(conn, bill_id)
    result["balance"] = round(float(result["total_due"]) - float(result["paid_amount"]), 2)
    return result


def mark_overdue_bills(conn, user_id):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    rows = conn.execute(
        """
        SELECT id FROM bills
        WHERE status IN ('issued','partially_paid') AND date(due_date) < date('now')
        """
    ).fetchall()
    bill_ids = [row["id"] for row in rows]
    conn.execute(
        """
        UPDATE bills
        SET status = 'overdue'
        WHERE status IN ('issued','partially_paid') AND date(due_date) < date('now')
        """
    )
    for bill_id in bill_ids:
        log_action(conn, user["id"], "bill_marked_overdue", f"bill:{bill_id}", "Due date passed")
    conn.commit()
    return {"message": "Overdue check completed.", "updated_count": len(bill_ids), "bill_ids": bill_ids}


def apply_penalty(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    bill_id = as_int(data.get("bill_id"), "bill_id")
    amount = validate_positive_amount(data.get("amount"), "amount")
    reason = data.get("reason", "Manual overdue penalty")

    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        raise APIError("Bill not found.", 404)

    conn.execute(
        """
        UPDATE bills
        SET penalty_amount = penalty_amount + ?,
            total_due = total_due + ?,
            status = CASE WHEN status = 'paid' THEN 'partially_paid' ELSE status END
        WHERE id = ?
        """,
        (amount, amount, bill_id),
    )
    log_action(conn, user["id"], "penalty_applied", f"bill:{bill_id}", f"Penalty {amount}. Reason: {reason}")
    conn.commit()
    return {"message": "Penalty applied.", "bill": get_bill_by_id(conn, user_id, bill_id)}


def send_issued_bill_notifications(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    billing_period = data.get("billing_period")
    if billing_period:
        validate_billing_period(billing_period)

    sql = """
        SELECT b.id AS bill_id, b.total_due, b.due_date, ua.account_number, ua.customer_id, u.name
        FROM bills b
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE b.status = 'issued'
    """
    params = []
    if billing_period:
        sql += " AND b.billing_period = ?"
        params.append(billing_period)

    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        message = f"Your utility bill for account {row['account_number']} is KES {row['total_due']:.2f}, due on {row['due_date']}."
        conn.execute(
            """
            INSERT INTO notification_queue (user_id, bill_id, channel, message, status, sent_at)
            VALUES (?, ?, 'in_app', ?, 'sent', datetime('now'))
            """,
            (row["customer_id"], row["bill_id"], message),
        )
        log_action(conn, user["id"], "bill_notification_sent", f"bill:{row['bill_id']}", message)

    conn.commit()
    return {"message": "Issued bill notifications queued/sent.", "sent_count": len(rows)}
