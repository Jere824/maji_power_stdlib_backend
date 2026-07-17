from datetime import datetime
import uuid
from app.auth import require_roles, log_action
from app.database import rows_to_dicts, row_to_dict
from app.errors import APIError
from app.validators import (
    as_int,
    validate_payment_method,
    validate_positive_amount,
    validate_status,
    PAYMENT_STATUSES,
)

OFFICER_ROLES = {"billing_officer", "admin"}
ADMIN_ROLES = {"admin"}


def get_bill(conn, bill_id):
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        raise APIError("Bill not found.", 404)
    return bill


def confirmed_paid_amount(conn, bill_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE bill_id = ? AND status = 'confirmed'",
        (bill_id,),
    ).fetchone()
    return float(row["total"] or 0)


def update_bill_status_after_payment(conn, bill_id):
    bill = get_bill(conn, bill_id)
    paid = confirmed_paid_amount(conn, bill_id)
    total_due = float(bill["total_due"])

    if paid <= 0:
        new_status = "issued"
    elif paid < total_due:
        new_status = "partially_paid"
    else:
        new_status = "paid"

    conn.execute("UPDATE bills SET status = ? WHERE id = ?", (new_status, bill_id))
    return new_status


def add_payment_event(conn, payment_id, status, detail=None):
    conn.execute(
        "INSERT INTO payment_status_events (payment_id, status, detail) VALUES (?, ?, ?)",
        (payment_id, status, detail),
    )


def list_payments(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    status = validate_status(query.get("status", [None])[0], PAYMENT_STATUSES)
    account_number = query.get("account_number", [None])[0]
    start_date = query.get("start_date", [None])[0]
    end_date = query.get("end_date", [None])[0]

    sql = """
        SELECT p.*, b.billing_period, b.total_due, b.status AS bill_status,
               ua.account_number, ua.service_type, u.name AS customer_name
        FROM payments p
        JOIN bills b ON b.id = p.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE 1 = 1
    """
    params = []
    if status:
        sql += " AND p.status = ?"
        params.append(status)
    if account_number:
        sql += " AND ua.account_number LIKE ?"
        params.append(f"%{account_number}%")
    if start_date:
        sql += " AND date(p.created_at) >= date(?)"
        params.append(start_date)
    if end_date:
        sql += " AND date(p.created_at) <= date(?)"
        params.append(end_date)
    sql += " ORDER BY p.created_at DESC, p.id DESC"

    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"items": rows, "count": len(rows)}


def payment_details(conn, user_id, payment_id):
    require_roles(conn, user_id, OFFICER_ROLES)
    payment = conn.execute(
        """
        SELECT p.*, b.billing_period, b.total_due, b.status AS bill_status,
               ua.account_number, ua.service_type, u.name AS customer_name
        FROM payments p
        JOIN bills b ON b.id = p.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE p.id = ?
        """,
        (payment_id,),
    ).fetchone()
    if not payment:
        raise APIError("Payment not found.", 404)
    result = row_to_dict(payment)
    result["timeline"] = rows_to_dicts(conn.execute(
        "SELECT status, detail, created_at FROM payment_status_events WHERE payment_id = ? ORDER BY created_at, id",
        (payment_id,),
    ).fetchall())
    receipt = conn.execute("SELECT * FROM receipts WHERE payment_id = ?", (payment_id,)).fetchone()
    result["receipt"] = row_to_dict(receipt)
    return result


def initiate_payment(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES | {"customer"})
    bill_id = as_int(data.get("bill_id"), "bill_id")
    amount = validate_positive_amount(data.get("amount"), "amount")
    method = validate_payment_method(data.get("payment_method"))
    provider_reference = data.get("provider_reference")
    initial_status = data.get("status", "pending")
    if initial_status not in {"initiated", "pending"}:
        raise APIError("Initial payment status must be initiated or pending.", 400)

    bill = get_bill(conn, bill_id)
    if bill["status"] == "paid":
        raise APIError("Bill is already paid.", 400)

    cur = conn.execute(
        """
        INSERT INTO payments (bill_id, amount, payment_method, status, provider_reference)
        VALUES (?, ?, ?, ?, ?)
        """,
        (bill_id, amount, method, initial_status, provider_reference),
    )
    payment_id = cur.lastrowid
    add_payment_event(conn, payment_id, "initiated", "Payment record created")
    if initial_status == "pending":
        add_payment_event(conn, payment_id, "pending", "Waiting for provider confirmation")
    log_action(conn, user["id"], "payment_initiated", f"payment:{payment_id}", f"Bill {bill_id}, amount {amount}")
    conn.commit()
    return {"message": "Payment initiated.", "payment": payment_details(conn, user_id, payment_id)}


def confirm_payment(conn, user_id, payment_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not payment:
        raise APIError("Payment not found.", 404)
    if payment["status"] == "confirmed":
        return {"message": "Payment was already confirmed.", "payment": payment_details(conn, user_id, payment_id)}
    if payment["status"] in {"failed", "cancelled"}:
        raise APIError("Failed or cancelled payments cannot be confirmed.", 400)

    provider_reference = data.get("provider_reference") or payment["provider_reference"] or f"MANUAL-{payment_id}-{uuid.uuid4().hex[:6].upper()}"
    receipt_number = payment["receipt_id"] or f"RCT-{datetime.now().strftime('%Y%m%d')}-{payment_id:05d}"
    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        UPDATE payments
        SET status = 'confirmed', provider_reference = ?, receipt_id = ?, paid_at = ?
        WHERE id = ?
        """,
        (provider_reference, receipt_number, paid_at, payment_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO receipts
        (receipt_number, payment_id, bill_id, amount, provider_reference, issued_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (receipt_number, payment_id, payment["bill_id"], payment["amount"], provider_reference, paid_at),
    )
    new_bill_status = update_bill_status_after_payment(conn, payment["bill_id"])
    add_payment_event(conn, payment_id, "confirmed", f"Provider reference: {provider_reference}")
    log_action(conn, user["id"], "payment_confirmed", f"payment:{payment_id}", f"Bill {payment['bill_id']} is now {new_bill_status}")
    conn.commit()
    return {"message": "Payment confirmed and receipt generated.", "payment": payment_details(conn, user_id, payment_id)}


def fail_payment(conn, user_id, payment_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    reason = data.get("reason", "Payment failed")
    payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not payment:
        raise APIError("Payment not found.", 404)
    if payment["status"] == "confirmed":
        raise APIError("Confirmed payments cannot be failed.", 400)
    conn.execute("UPDATE payments SET status = 'failed' WHERE id = ?", (payment_id,))
    add_payment_event(conn, payment_id, "failed", reason)
    log_action(conn, user["id"], "payment_failed", f"payment:{payment_id}", reason)
    conn.commit()
    return {"message": "Payment marked as failed.", "payment": payment_details(conn, user_id, payment_id)}


def manual_link_payment(conn, user_id, data):
    """Admin-only edge case: move a payment to a different bill."""
    user = require_roles(conn, user_id, ADMIN_ROLES)
    payment_id = as_int(data.get("payment_id"), "payment_id")
    new_bill_id = as_int(data.get("bill_id"), "bill_id")
    reason = data.get("reason", "Manual payment linking")

    payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not payment:
        raise APIError("Payment not found.", 404)
    get_bill(conn, new_bill_id)
    old_bill_id = payment["bill_id"]

    conn.execute("UPDATE payments SET bill_id = ? WHERE id = ?", (new_bill_id, payment_id))
    conn.execute("UPDATE receipts SET bill_id = ? WHERE payment_id = ?", (new_bill_id, payment_id))
    update_bill_status_after_payment(conn, old_bill_id)
    update_bill_status_after_payment(conn, new_bill_id)
    add_payment_event(conn, payment_id, "manual_linked", f"Moved from bill {old_bill_id} to bill {new_bill_id}. Reason: {reason}")
    log_action(conn, user["id"], "manual_payment_link", f"payment:{payment_id}", f"Moved from bill {old_bill_id} to bill {new_bill_id}. Reason: {reason}")
    conn.commit()
    return {"message": "Payment manually linked to bill.", "payment": payment_details(conn, user_id, payment_id)}


def list_receipts(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    account_number = query.get("account_number", [None])[0]
    sql = """
        SELECT r.*, ua.account_number, u.name AS customer_name
        FROM receipts r
        JOIN bills b ON b.id = r.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = ua.customer_id
        WHERE 1 = 1
    """
    params = []
    if account_number:
        sql += " AND ua.account_number LIKE ?"
        params.append(f"%{account_number}%")
    sql += " ORDER BY r.issued_at DESC, r.id DESC"
    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"items": rows, "count": len(rows)}
