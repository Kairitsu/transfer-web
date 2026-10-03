from http import HTTPStatus


class AppError(Exception):
    """An error whose message is safe to show to the user."""

    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status
