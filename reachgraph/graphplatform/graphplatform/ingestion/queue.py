"""Event queue between ingestion connectors and the Graph Write Service.

Redis Streams is the only implementation; EventQueue exists as an abstract
interface so a later phase can swap the backend (Kafka, SQS, ...) without
touching connector or writer code -- neither of those talk to redis
directly, only to this module.
"""

from __future__ import annotations

import abc
import json
import logging
from typing import Any, Callable, Iterable

import redis

log = logging.getLogger("graphplatform.ingestion.queue")


class EventQueue(abc.ABC):
    @abc.abstractmethod
    def publish(self, stream: str, event: dict[str, Any]) -> str:
        """Publish one event, return the queue's message id."""

    @abc.abstractmethod
    def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[dict[str, Any]], None],
        *,
        block_ms: int = 5000,
        count: int = 50,
        stop_after_idle_reads: int | None = None,
        max_messages: int | None = None,
    ) -> None:
        """Call handler(event) for every new message, acking only after the
        handler returns without raising. Blocks forever unless
        stop_after_idle_reads is set, in which case it returns after that
        many consecutive empty polls -- used for bounded runs/tests so a
        script doesn't hang waiting on a live feed that has gone quiet.
        max_messages stops after that many messages have been handled,
        mid-batch if needed -- for a deliberately bounded demo/backfill run.
        """


class RedisStreamQueue(EventQueue):
    def __init__(self, url: str = "redis://127.0.0.1:6379/0") -> None:
        self._r = redis.Redis.from_url(url, decode_responses=True)

    def publish(self, stream: str, event: dict[str, Any]) -> str:
        msg_id = self._r.xadd(stream, {"data": json.dumps(event)})
        log.debug("published event", extra={"stream": stream, "type": event.get("type"), "id": msg_id})
        return msg_id

    def ensure_group(self, stream: str, group: str) -> None:
        try:
            self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[dict[str, Any]], None],
        *,
        block_ms: int = 5000,
        count: int = 50,
        stop_after_idle_reads: int | None = None,
        max_messages: int | None = None,
    ) -> None:
        self.ensure_group(stream, group)
        handled = 0

        # Recover this consumer's own delivered-but-never-acked entries
        # from a prior crashed run first -- reading id "0" (instead of
        # ">") returns exactly this consumer's pending entries list and
        # never blocks. Without this, a handler crash mid-batch leaves
        # those entries permanently stuck: ">" only ever returns entries
        # that have never been delivered to *any* consumer in the group.
        while True:
            if max_messages is not None and handled >= max_messages:
                return
            resp = self._r.xreadgroup(group, consumer, {stream: "0"}, count=count)
            pending = resp[0][1] if resp else []
            if not pending:
                break
            for msg_id, fields in pending:
                if max_messages is not None and handled >= max_messages:
                    return
                event = json.loads(fields["data"])
                handler(event)
                self._r.xack(stream, group, msg_id)
                handled += 1

        idle_reads = 0
        while True:
            if max_messages is not None and handled >= max_messages:
                return
            resp = self._r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
            if not resp:
                idle_reads += 1
                if stop_after_idle_reads is not None and idle_reads >= stop_after_idle_reads:
                    return
                continue
            idle_reads = 0
            for _stream_name, messages in resp:
                for msg_id, fields in messages:
                    if max_messages is not None and handled >= max_messages:
                        return
                    event = json.loads(fields["data"])
                    handler(event)
                    self._r.xack(stream, group, msg_id)
                    handled += 1

    def read_all(self, stream: str, count: int = 1000) -> list[dict[str, Any]]:
        """Read every entry currently on the stream, ignoring consumer
        groups. Used by tests and one-shot inspection, not by the writer.
        """
        entries = self._r.xrange(stream, count=count)
        return [json.loads(fields["data"]) for _id, fields in entries]

    def publish_all(self, stream: str, events: Iterable[dict[str, Any]]) -> int:
        n = 0
        for event in events:
            self.publish(stream, event)
            n += 1
        return n

    def close(self) -> None:
        self._r.close()
