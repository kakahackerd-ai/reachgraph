"""End-to-end proof: a real registry backfill -> published onto the real
event queue -> consumed by GraphIngestionWriter -> landed in the real
HydraDB instance, read back and verified. Nothing here is mocked.
"""

from graphplatform import schema
from graphplatform.ingestion.events import STREAM_REGISTRY
from graphplatform.ingestion.registry.npm import NpmConnector


def test_npm_backfill_flows_through_queue_and_writer_into_hydradb(queue, writer, service, cleanup, run_id):
    stream = f"{STREAM_REGISTRY}:test:{run_id}"
    group = f"test-group:{run_id}"

    conn = NpmConnector()
    try:
        # is-number is small (few versions, zero runtime deps) -- keeps this
        # test fast while still exercising the real npm registry end to end.
        events = [e.to_dict() for e in conn.backfill(["is-number"])]
    finally:
        conn.close()
    assert len(events) > 0

    for e in events:
        cleanup(schema.VERSION, f"npm:is-number@{e['version']}")
    cleanup(schema.PACKAGE, "npm:is-number")

    queue.publish_all(stream, events)
    processed: list[dict] = []
    queue.subscribe(
        stream, group, "writer-1", lambda ev: (writer.handle(ev), processed.append(ev)), block_ms=200, stop_after_idle_reads=1
    )
    assert len(processed) == len(events)

    package = service.get_package("npm:is-number", consistency="strong")
    assert package is not None
    assert package["ecosystem"] == "npm"
    assert package["name"] == "is-number"

    any_version = events[0]["version"]
    version = service.get_version(f"npm:is-number@{any_version}", consistency="strong")
    assert version is not None
    assert version["package_key"] == "npm:is-number"

    queue._r.delete(stream)
