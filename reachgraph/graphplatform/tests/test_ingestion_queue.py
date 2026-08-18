def test_publish_and_read_all_roundtrip(queue, run_id):
    stream = f"test:stream:{run_id}"
    queue.publish(stream, {"type": "x", "n": 1})
    queue.publish(stream, {"type": "x", "n": 2})
    events = queue.read_all(stream)
    assert [e["n"] for e in events] == [1, 2]
    queue._r.delete(stream)


def test_subscribe_consumer_group_processes_and_acks(queue, run_id):
    stream = f"test:stream:{run_id}"
    group = f"test:group:{run_id}"
    queue.publish_all(stream, [{"type": "x", "n": i} for i in range(3)])

    seen: list[int] = []
    queue.subscribe(stream, group, "consumer-1", lambda e: seen.append(e["n"]), block_ms=200, stop_after_idle_reads=1)

    assert seen == [0, 1, 2]
    # a second subscribe with the same group sees nothing new -- already acked
    seen_again: list[int] = []
    queue.subscribe(
        stream, group, "consumer-1", lambda e: seen_again.append(e["n"]), block_ms=200, stop_after_idle_reads=1
    )
    assert seen_again == []
    queue._r.delete(stream)
