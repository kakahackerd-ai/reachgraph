"""Shared interface every advisory connector implements."""

from __future__ import annotations

from typing import Iterator, Protocol

from ..events import AdvisoryPublished


class AdvisoryConnector(Protocol):
    name: str

    def backfill(self, **kwargs: object) -> Iterator[AdvisoryPublished]: ...

    def fetch_or_subscribe(self, **kwargs: object) -> Iterator[AdvisoryPublished]: ...

    def close(self) -> None: ...
