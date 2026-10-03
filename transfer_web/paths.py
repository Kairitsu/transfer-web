from .errors import AppError
from .helper import path_problem


def validate_rel(value):
    """Validate a path relative to an endpoint root; "" means the root itself."""
    if value is None:
        value = ""
    problem = path_problem(value)
    if problem:
        raise AppError(problem)
    return "" if value in ("", ".") else value
