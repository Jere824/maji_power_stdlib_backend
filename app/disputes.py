from app.auth import require_roles, log_action
from app.database import rows_to_dicts, row_to_dict
from app.errors import APIError
from app.validators import as_int, validate_status, DISPUTE_STATUSES

OFFICER_ROLES = {"billing_officer", "admin"}


def create_dispute(conn, user_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES | {"customer"})
    bill_id = as_int(data.get("bill_id"), "bill_id")
    reason = data.get("reason")
    if not reason:
        raise APIError("reason is required.", 400)
    bill = conn.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        raise APIError("Bill not found.", 404)
    cur = conn.execute(
        "INSERT INTO disputes (bill_id, raised_by_user_id, reason) VALUES (?, ?, ?)",
        (bill_id, user["id"], reason),
    )
    dispute_id = cur.lastrowid
    log_action(conn, user["id"], "dispute_created", f"dispute:{dispute_id}", f"Bill {bill_id}: {reason}")
    conn.commit()
    return {"message": "Dispute created.", "dispute": get_dispute(conn, user_id, dispute_id)}


def list_disputes(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    status = validate_status(query.get("status", [None])[0], DISPUTE_STATUSES)
    sql = """
        SELECT d.*, b.billing_period, ua.account_number, u.name AS raised_by_name
        FROM disputes d
        JOIN bills b ON b.id = d.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = d.raised_by_user_id
        WHERE 1 = 1
    """
    params = []
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    sql += " ORDER BY d.created_at DESC, d.id DESC"
    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"items": rows, "count": len(rows)}


def get_dispute(conn, user_id, dispute_id):
    require_roles(conn, user_id, OFFICER_ROLES | {"customer"})
    row = conn.execute(
        """
        SELECT d.*, b.billing_period, ua.account_number, u.name AS raised_by_name
        FROM disputes d
        JOIN bills b ON b.id = d.bill_id
        JOIN utility_accounts ua ON ua.id = b.utility_account_id
        JOIN users u ON u.id = d.raised_by_user_id
        WHERE d.id = ?
        """,
        (dispute_id,),
    ).fetchone()
    if not row:
        raise APIError("Dispute not found.", 404)
    return row_to_dict(row)


def update_dispute(conn, user_id, dispute_id, data):
    user = require_roles(conn, user_id, OFFICER_ROLES)
    status = validate_status(data.get("status"), DISPUTE_STATUSES)
    if not status:
        raise APIError("status is required.", 400)
    resolution_note = data.get("resolution_note")
    row = conn.execute("SELECT id FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
    if not row:
        raise APIError("Dispute not found.", 404)
    conn.execute(
        """
        UPDATE disputes
        SET status = ?, resolution_note = COALESCE(?, resolution_note), updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, resolution_note, dispute_id),
    )
    log_action(conn, user["id"], "dispute_updated", f"dispute:{dispute_id}", f"Status changed to {status}")
    conn.commit()
    return {"message": "Dispute updated.", "dispute": get_dispute(conn, user_id, dispute_id)}
