"""
MajiPower Utilities Billing Backend

Run:
    python3 server.py

Demo headers:
    X-User-Id: 3  -> billing officer
    X-User-Id: 5  -> admin
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from app.database import get_connection, initialize_database
from app.errors import APIError
from app import billing, payments, reports, disputes, tariffs

HOST = "127.0.0.1"
PORT = 8000


def parse_int_path_part(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise APIError(f"{name} must be a number.", 400)


class MajiPowerHandler(BaseHTTPRequestHandler):
    server_version = "MajiPowerStdlibBackend/1.0"

    def _send_json(self, data, status_code=200):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status_code=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise APIError("Request body must be valid JSON.", 400)

    def _handle_error(self, error):
        if isinstance(error, APIError):
            self._send_json({"error": error.message, "details": error.details}, error.status_code)
        else:
            self._send_json({"error": "Internal server error", "details": str(error)}, 500)

    def do_GET(self):
        try:
            self._route("GET")
        except Exception as error:
            self._handle_error(error)

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as error:
            self._handle_error(error)

    def do_PATCH(self):
        try:
            self._route("PATCH")
        except Exception as error:
            self._handle_error(error)

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        user_id = self.headers.get("X-User-Id")
        data = self._read_json_body() if method in {"POST", "PATCH"} else {}

        with get_connection() as conn:
            # Public route
            if method == "GET" and path == "/health":
                return self._send_json({
                    "status": "ok",
                    "backend": "pure-python-standard-library",
                    "database": "sqlite",
                    "message": "MajiPower backend is running without Flask, FastAPI, or Pydantic.",
                })

            # Billing officer dashboard and bills
            if method == "GET" and path == "/officer/dashboard":
                return self._send_json(billing.dashboard(conn, user_id))

            if method == "GET" and path == "/billing/preview":
                return self._send_json(billing.preview_billing_run(conn, user_id, query))

            if method == "POST" and path == "/billing/generate-one":
                return self._send_json(billing.generate_one_bill(conn, user_id, data), 201)

            if method == "POST" and path == "/billing/generate-bulk":
                return self._send_json(billing.generate_bulk_bills(conn, user_id, data), 201)

            if method == "POST" and path == "/billing/mark-overdue":
                return self._send_json(billing.mark_overdue_bills(conn, user_id))

            if method == "POST" and path == "/billing/apply-penalty":
                return self._send_json(billing.apply_penalty(conn, user_id, data))

            if method == "POST" and path == "/billing/send-issued-notifications":
                return self._send_json(billing.send_issued_bill_notifications(conn, user_id, data))

            if method == "GET" and path == "/bills":
                return self._send_json(billing.list_bills(conn, user_id, query))

            if method == "GET" and len(parts) == 2 and parts[0] == "bills":
                bill_id = parse_int_path_part(parts[1], "bill_id")
                return self._send_json(billing.get_bill_by_id(conn, user_id, bill_id))

            # Payments and reconciliation
            if method == "GET" and path == "/payments":
                return self._send_json(payments.list_payments(conn, user_id, query))

            if method == "POST" and path == "/payments/initiate":
                return self._send_json(payments.initiate_payment(conn, user_id, data), 201)

            if method == "GET" and len(parts) == 2 and parts[0] == "payments":
                payment_id = parse_int_path_part(parts[1], "payment_id")
                return self._send_json(payments.payment_details(conn, user_id, payment_id))

            if method == "POST" and len(parts) == 3 and parts[0] == "payments" and parts[2] == "confirm":
                payment_id = parse_int_path_part(parts[1], "payment_id")
                return self._send_json(payments.confirm_payment(conn, user_id, payment_id, data))

            if method == "POST" and len(parts) == 3 and parts[0] == "payments" and parts[2] == "fail":
                payment_id = parse_int_path_part(parts[1], "payment_id")
                return self._send_json(payments.fail_payment(conn, user_id, payment_id, data))

            if method == "POST" and path == "/payments/manual-link":
                return self._send_json(payments.manual_link_payment(conn, user_id, data))

            if method == "GET" and path == "/receipts":
                return self._send_json(payments.list_receipts(conn, user_id, query))

            # Reports
            if method == "GET" and path == "/reports/overdue":
                return self._send_json(reports.overdue_accounts(conn, user_id, query))

            if method == "GET" and path == "/reports/collections/daily":
                return self._send_json(reports.daily_collections(conn, user_id, query))

            if method == "GET" and path == "/reports/payment-status":
                return self._send_json(reports.payment_status_summary(conn, user_id, query))

            if method == "GET" and path == "/reports/revenue-by-service":
                return self._send_json(reports.revenue_by_service_type(conn, user_id, query))

            if method == "GET" and path == "/reports/collections.csv":
                csv_text = reports.collections_csv(conn, user_id, query)
                return self._send_text(csv_text, 200, "text/csv; charset=utf-8")

            # Disputes
            if method == "POST" and path == "/disputes":
                return self._send_json(disputes.create_dispute(conn, user_id, data), 201)

            if method == "GET" and path == "/disputes":
                return self._send_json(disputes.list_disputes(conn, user_id, query))

            if method == "GET" and len(parts) == 2 and parts[0] == "disputes":
                dispute_id = parse_int_path_part(parts[1], "dispute_id")
                return self._send_json(disputes.get_dispute(conn, user_id, dispute_id))

            if method == "PATCH" and len(parts) == 2 and parts[0] == "disputes":
                dispute_id = parse_int_path_part(parts[1], "dispute_id")
                return self._send_json(disputes.update_dispute(conn, user_id, dispute_id, data))

            # Tariffs
            if method == "GET" and path == "/tariffs":
                return self._send_json(tariffs.list_tariffs(conn, user_id, query))

            if method == "POST" and path == "/tariffs":
                return self._send_json(tariffs.create_tariff(conn, user_id, data), 201)

            if method == "GET" and path == "/tariffs/test-bill":
                return self._send_json(tariffs.test_bill_calculator(conn, user_id, query))

            raise APIError(f"Route not found: {method} {path}", 404)


def run():
    initialize_database()
    httpd = HTTPServer((HOST, PORT), MajiPowerHandler)
    print(f"MajiPower pure Python backend running at http://{HOST}:{PORT}")
    print("Demo billing officer header: X-User-Id: 3")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
