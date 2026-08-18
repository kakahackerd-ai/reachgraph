"""Shared interface every registry connector implements. A third registry
(crates.io, RubyGems, ...) becomes addable later as a new class satisfying
this Protocol, without touching RegistryIngestionService or the writer.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..events import PackageVersionPublished


class RegistryConnector(Protocol):
    name: str
    source_type: str

    def backfill(self, package_names: list[str]) -> Iterator[PackageVersionPublished]:
        """Yield every known version for a bounded, explicit set of packages."""
        ...

    def fetch_or_subscribe(self, **kwargs: object) -> Iterator[PackageVersionPublished]:
        """Yield newly published versions, live/incremental. Polls/blocks
        internally; callers iterate this in a loop and publish each yielded
        event to the queue.
        """
        ...

    def close(self) -> None: ...
