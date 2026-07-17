class APIError(Exception):
    """Exception used to return clean JSON errors from the backend."""

    def __init__(self, message, status_code=400, details=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}
