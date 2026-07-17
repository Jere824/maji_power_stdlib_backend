from app.database import row_to_dict
from app.errors import APIError


def get_current_user(conn, user_id):
    if not user_id:
        raise APIError("Missing X-User-Id header. Use X-User-Id: 3 for the demo billing officer.", 401)
    try:
        user_id = int(user_id)
    except ValueError:
        raise APIError("X-User-Id must be a number.", 400)

    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise APIError("User not found.", 401)
    return row_to_dict(user)


def require_roles(conn, user_id, allowed_roles):
    user = get_current_user(conn, user_id)
    if user["role"] not in allowed_roles:
        raise APIError(
            f"Access denied. Required role: {', '.join(allowed_roles)}. Current role: {user['role']}.",
            403,
        )
    return user


def log_action(conn, user_id, action, target, detail=None):
    conn.execute(
        "INSERT INTO audit_log (user_id, action, target, detail) VALUES (?, ?, ?, ?)",
        (user_id, action, target, detail),
    )
