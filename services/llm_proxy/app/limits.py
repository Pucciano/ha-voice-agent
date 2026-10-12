"""Daily request limit, a guard against runaway cost with a paid upstream."""

import datetime
import typing


def _utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date()


class DailyLimit:
    """Count chat requests per UTC day; a limit of 0 means no limit.

    The count lives in memory, so a restart starts it again at zero.
    """

    def __init__(
        self,
        limit: int,
        today: typing.Callable[[], datetime.date] = _utc_today,
    ) -> None:
        self.limit = limit
        self._today = today
        self._day = today()
        self._count = 0

    @property
    def used(self) -> int:
        """Requests counted today."""

        self._roll()
        return self._count

    def try_acquire(self) -> bool:
        """Count one request; False when today's limit is already used up."""

        self._roll()
        if self.limit and self._count >= self.limit:
            return False
        self._count += 1
        return True

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self._count = 0
