from app.auth import require_roles, log_action
from app.database import rows_to_dicts, row_to_dict
from app.errors import APIError
from app.validators import as_float, as_int, validate_service_type, validate_date

ADMIN_ROLES = {"admin"}
OFFICER_ROLES = {"billing_officer", "admin"}


def list_tariffs(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    service_type = validate_service_type(query.get("service_type", [None])[0])
    sql = "SELECT * FROM tariffs WHERE 1=1"
    params = []
    if service_type:
        sql += " AND service_type = ?"
        params.append(service_type)
    sql += " ORDER BY service_type, active_from DESC, id DESC"
    return {"items": rows_to_dicts(conn.execute(sql, params).fetchall())}


def create_tariff(conn, user_id, data):
    user = require_roles(conn, user_id, ADMIN_ROLES)
    service_type = validate_service_type(data.get("service_type"))
    if not service_type:
        raise APIError("service_type is required.", 400)
    price_per_unit = as_float(data.get("price_per_unit"), "price_per_unit")
    fixed_charge = as_float(data.get("fixed_charge", 0), "fixed_charge")
    tax_rate = as_float(data.get("tax_rate", 0), "tax_rate")
    overdue_penalty_flat = as_float(data.get("overdue_penalty_flat", 0), "overdue_penalty_flat")
    grace_period_days = int(data.get("grace_period_days", 14))
    active_from = data.get("active_from") or None
    if active_from:
        validate_date(active_from, "active_from")
    else:
        active_from = conn.execute("SELECT date('now') AS d").fetchone()["d"]

    cur = conn.execute(
        """
        INSERT INTO tariffs
        (service_type, price_per_unit, fixed_charge, tax_rate, overdue_penalty_flat,
         grace_period_days, active_from, created_by_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (service_type, price_per_unit, fixed_charge, tax_rate, overdue_penalty_flat, grace_period_days, active_from, user["id"]),
    )
    tariff_id = cur.lastrowid
    log_action(conn, user["id"], "tariff_created", f"tariff:{tariff_id}", f"{service_type} tariff created")
    conn.commit()
    tariff = conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
    return {"message": "Tariff created.", "tariff": row_to_dict(tariff)}


def test_bill_calculator(conn, user_id, query):
    require_roles(conn, user_id, OFFICER_ROLES)
    service_type = validate_service_type(query.get("service_type", [None])[0])
    if not service_type:
        raise APIError("service_type is required.", 400)
    units = as_float(query.get("units", [None])[0], "units")
    tariff = conn.execute(
        """
        SELECT * FROM tariffs
        WHERE service_type = ? AND (active_to IS NULL OR active_to >= date('now'))
        ORDER BY active_from DESC, id DESC
        LIMIT 1
        """,
        (service_type,),
    ).fetchone()
    if not tariff:
        raise APIError("No active tariff found.", 404)
    consumption_charge = round(units * float(tariff["price_per_unit"]), 2)
    fixed_charge = round(float(tariff["fixed_charge"]), 2)
    tax_amount = round((consumption_charge + fixed_charge) * float(tariff["tax_rate"]), 2)
    total = round(consumption_charge + fixed_charge + tax_amount, 2)
    return {
        "service_type": service_type,
        "units": units,
        "consumption_charge": consumption_charge,
        "fixed_charge": fixed_charge,
        "tax_amount": tax_amount,
        "total": total,
    }
