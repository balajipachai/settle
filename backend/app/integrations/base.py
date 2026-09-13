"""Integration errors and bounded retries (PRD §17)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from app.domain.errors import ErrorCode, SettleError

T = TypeVar("T")

_SYSTEM_CODES = {
    "stripe": ErrorCode.STRIPE_READ_ERROR,
    "hubspot": ErrorCode.HUBSPOT_ERROR,
    "gmail": ErrorCode.GMAIL_ERROR,
    "slack": ErrorCode.SLACK_ERROR,
}


class IntegrationError(SettleError):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        system: str,
        retryable: bool = False,
        status_code: int | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code, message, retryable=retryable, external_system=system)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


class TransientError(IntegrationError):
    """429 / 5xx / timeouts. For writes, `outcome_unknown` means the server may have committed."""

    def __init__(self, code: ErrorCode, message: str, *, system: str, status_code: int | None = None,
                 outcome_unknown: bool = False) -> None:
        super().__init__(code, message, system=system, retryable=True, status_code=status_code,
                         outcome_unknown=outcome_unknown)


def code_for(system: str, *, write: bool = False) -> ErrorCode:
    if system == "stripe" and write:
        return ErrorCode.STRIPE_WRITE_ERROR
    return _SYSTEM_CODES[system]


def error_from_status(system: str, status_code: int, detail: str, *, write: bool = False) -> IntegrationError:
    code = code_for(system, write=write)
    msg = f"{system} HTTP {status_code}: {detail}"[:500]
    if status_code == 429 or status_code >= 500:
        return TransientError(code, msg, system=system, status_code=status_code)
    return IntegrationError(code, msg, system=system, status_code=status_code)


def error_from_transport(system: str, exc: Exception, *, write: bool = False) -> TransientError:
    # A connect failure means the request never left; a read timeout on a write is ambiguous.
    unknown = write and not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
    return TransientError(code_for(system, write=write), f"{system} transport error: {type(exc).__name__}",
                          system=system, outcome_unknown=unknown)


def response_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)[:300]
        return str(body.get("message") or err or body)[:300]
    return str(body)[:300]


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, IntegrationError, float], None] | None = None,
) -> T:
    """Bounded exponential backoff with jitter (0.5s, 1s, 2s...). Only retryable errors are retried."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except IntegrationError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25) if delay else 0.0
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")
