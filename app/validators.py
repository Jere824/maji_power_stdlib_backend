from datetime import datetime
from app.errors import APIError

PAYMENT_METHODS = {"mobile_money", "card", "bank"}
PAYMENT_STATUSES = {"initiated", "pending", "confirmed", "failed", "cancelled"}
BILL_STATUSES = {"draft", "issued", "paid", "partially_paid", "overdue"}
DISPUTE_STATUSES = {"open", "under_review", "resolved", "rejected"}
SERVICE_TYPES = {"water", "electricity"}


def require_fields(data, fields):
    missing = [field for field in fields if data.get(field) in (None, "")]
    if missing:
        raise APIError("Missing required fields.", 400, {"missing": missing})


def as_int(value, field_name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise APIError(f"{field_name} must be an integer.", 400)


def as_float(value, field_name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise APIError(f"{field_name} must be a number.", 400)


def validate_positive_amount(value, field_name="amount"):
    amount = as_float(value, field_name)
    if amount <= 0:
        raise APIError(f"{field_name} must be greater than 0.", 400)
    return amount


def validate_billing_period(period):
    if not period:
        raise APIError("billing_period is required, for example 2026-05.", 400)
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError:
        raise APIError("billing_period must use YYYY-MM format, for example 2026-05.", 400)
    return period


def validate_date(date_value, field_name):
    if not date_value:
        return None
    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        raise APIError(f"{field_name} must use YYYY-MM-DD format.", 400)
    return date_value


def validate_payment_method(method):
    if method not in PAYMENT_METHODS:
        raise APIError("payment_method must be mobile_money, card, or bank.", 400)
    return method


def validate_service_type(service_type):
    if service_type is None or service_type == "":
        return None
    if service_type not in SERVICE_TYPES:
        raise APIError("service_type must be water or electricity.", 400)
    return service_type


def validate_status(value, allowed, field_name="status"):
    if value is None or value == "":
        return None
    if value not in allowed:
        raise APIError(f"{field_name} has an invalid value.", 400, {"allowed": sorted(allowed)})
    return value
