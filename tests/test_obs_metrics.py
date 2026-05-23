"""MetricsEmitter buffers correctly, applies dimensions, and is fault tolerant."""
from unittest.mock import MagicMock

import pytest

from obs.metrics import MetricsEmitter, NullMetricsEmitter, NAMESPACE


@pytest.fixture
def fake_cw():
    return MagicMock()


def test_count_buffers_and_flushes(fake_cw):
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    em.count("ImagesDownloaded")
    em.count("ImagesDownloaded", 4)
    em.flush()

    fake_cw.put_metric_data.assert_called_once()
    kwargs = fake_cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == NAMESPACE
    md = kwargs["MetricData"]
    assert len(md) == 2
    assert {m["MetricName"] for m in md} == {"ImagesDownloaded"}
    assert {m["Unit"] for m in md} == {"Count"}
    # Both default dimensions present, in order.
    for m in md:
        names = [d["Name"] for d in m["Dimensions"]]
        assert names[:2] == ["Customer", "Stage"]
        values = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        assert values["Customer"] == "acme"
        assert values["Stage"] == "scraper"


def test_extra_dimensions_are_added(fake_cw):
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    em.count("ShardsSkipped", shard="chunk_3.csv", region="eu-central-1")
    em.flush()

    md = fake_cw.put_metric_data.call_args.kwargs["MetricData"]
    dim_names = {d["Name"] for d in md[0]["Dimensions"]}
    assert {"Customer", "Stage", "shard", "region"} <= dim_names


def test_timer_emits_started_done_and_seconds(fake_cw):
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    with em.timer("Shard", shard="x"):
        pass
    em.flush()

    md = fake_cw.put_metric_data.call_args.kwargs["MetricData"]
    names = [m["MetricName"] for m in md]
    assert "ShardStarted" in names
    assert "ShardDone" in names
    assert "ShardSeconds" in names
    # ShardFailed is *not* emitted on the happy path.
    assert "ShardFailed" not in names


def test_timer_emits_failed_on_exception(fake_cw):
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    with pytest.raises(RuntimeError):
        with em.timer("Shard"):
            raise RuntimeError("boom")
    em.flush()

    md = fake_cw.put_metric_data.call_args.kwargs["MetricData"]
    names = [m["MetricName"] for m in md]
    assert "ShardStarted" in names
    assert "ShardFailed" in names
    assert "ShardDone" not in names
    assert "ShardSeconds" in names


def test_batch_limit_triggers_intermediate_flush(fake_cw):
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    for i in range(45):
        em.count("ImagesDownloaded")
    em.flush()

    # 45 items: one auto-flush at 20, another at 40, manual flush of 5.
    assert fake_cw.put_metric_data.call_count == 3
    sizes = [
        len(call.kwargs["MetricData"])
        for call in fake_cw.put_metric_data.call_args_list
    ]
    assert sizes == [20, 20, 5]


def test_put_metric_data_failure_is_swallowed(fake_cw):
    fake_cw.put_metric_data.side_effect = RuntimeError("aws down")
    em = MetricsEmitter(stage="scraper", customer="acme", client=fake_cw)
    em.count("ImagesDownloaded")
    # Must not raise — observability cannot crash production work.
    em.flush()


def test_null_emitter_is_inert():
    em = NullMetricsEmitter()
    em.count("x")
    em.value("y", 1, unit="Seconds")
    with em.timer("z"):
        pass
    em.flush()  # no-op
