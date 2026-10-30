"""Refusing a request: :class:`RequestError` and the job-failure mapping.

``RequestError`` is raised anywhere below a handler and answered once by
:func:`answers_errors`; :func:`job_errors` turns the failures of a profiling job
into one.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .assembly import SequenceError, SequenceTooLarge
from .limits import QUEUE_RETRY_SECONDS, QueueFullError
from .profiling import BadRequest

log = logging.getLogger("uvicorn.error")


class RequestError(Exception):
    """A request to refuse: ``str(error)`` is the message, ``status`` the code.

    Raised anywhere below a handler and answered once by :func:`answers_errors`.
    """

    def __init__(
        self, message: str, status: int = 400, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"error": str(self)}, status_code=self.status, headers=self.headers
        )


def answers_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Turn a :class:`RequestError` raised by ``handler`` into its response."""

    @functools.wraps(handler)
    async def wrapper(request: Request, *args: Any) -> JSONResponse:
        try:
            return await handler(request, *args)
        except RequestError as error:
            return error.response()

    return wrapper


def check_queue(state: Any) -> None:
    """Refuse the request now (before its body is read) if the queue is full."""
    try:
        state.jobs.check()
    except QueueFullError:
        raise _busy() from None


def _busy() -> RequestError:
    return RequestError(
        "the server is busy; retry shortly",
        503,
        {"Retry-After": str(QUEUE_RETRY_SECONDS)},
    )


#: Exceptions of a job that are the client's fault, with their status.
#: ``SequenceTooLarge`` is a ``SequenceError``, so it comes first.
_CLIENT_ERRORS: tuple[tuple[type[Exception], int], ...] = (
    (BadRequest, 400),
    (SequenceTooLarge, 413),
    (SequenceError, 400),
)


def _client_error(error: BaseException) -> RequestError | None:
    if isinstance(error, QueueFullError):
        return _busy()
    for kind, status in _CLIENT_ERRORS:
        if isinstance(error, kind):
            return RequestError(str(error), status)
    return None


@contextmanager
def job_errors(what: str) -> Iterator[None]:
    """Run profiling jobs; raise a :class:`RequestError` for any failure.

    Client mistakes and a full queue keep their message and status; anything
    else is logged and answered with a generic 500 (``"<what> failed"``).
    """
    try:
        yield
    except BaseExceptionGroup as group:  # from the task group of /profile
        for error in group.exceptions:
            if (refusal := _client_error(error)) is not None:
                raise refusal from None
        log.exception("%s failed", what)
        raise RequestError(f"{what} failed", 500) from None
    except Exception as error:
        if (refusal := _client_error(error)) is not None:
            raise refusal from error
        log.exception("%s failed", what)
        raise RequestError(f"{what} failed", 500) from None
