import random
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.stub import ANY, Stubber

import simulate_tags

PREFIX = "/slc-kep01/Channel1"
T0 = 1_793_500_000  # a fixed clock, 2026-11-01


def throttled(entry_id):
    return {"entryId": entry_id, "errors": [{"errorCode": "ThrottlingException", "errorMessage": "Rate exceeded", "timestamps": []}]}


def rejected(entry_id):
    return {"entryId": entry_id, "errors": [{"errorCode": "InvalidRequestException", "errorMessage": "bad alias", "timestamps": []}]}


def run_main(*argv):
    with patch("sys.argv", ["simulate_tags.py", *argv]):
        simulate_tags.main()


def test_device_seed_is_stable_and_within_a_day():
    assert simulate_tags.device_seed("Press01") == simulate_tags.device_seed("Press01")
    assert all(0 <= simulate_tags.device_seed(d) < 24 for d in simulate_tags.RAW_TAGS)


def test_cycle_count_only_goes_up_and_differs_per_press():
    _, generate = simulate_tags.MEASUREMENTS["cycle_count"]
    counts = [generate(T0 + 6 * i, 3) for i in range(5)]
    assert counts == sorted(set(counts))
    assert generate(T0, 3) != generate(T0, 4)


@pytest.mark.parametrize("measurement, low, high", [
    ("oil_temperature", 100, 140),
    ("hydraulic_pressure", 1800, 2600),
    ("motor_current", 25, 65),
])
def test_analog_values_stay_in_a_plausible_range(measurement, low, high):
    # Seeded: the generators add Gaussian noise, and an unlucky draw shouldn't fail CI
    random.seed(42)
    _, generate = simulate_tags.MEASUREMENTS[measurement]
    assert all(low < generate(T0 + 37 * i, 5) < high for i in range(500))


def test_property_value_uses_the_measurement_value_type():
    assert simulate_tags.property_value("cycle_count", T0 + 0.9, 0) == {
        "value": {"integerValue": (T0 + 0.9) // 6 - 290_000_000},
        "timestamp": {"timeInSeconds": T0, "offsetInNanos": 0},
        "quality": "GOOD",
    }
    assert list(simulate_tags.property_value("oil_temperature", T0, 0)["value"]) == ["doubleValue"]


def test_build_entries_chunks_values_and_interleaves_streams():
    timestamps = list(range(T0, T0 + 25))  # 25 points -> chunks of 10, 10, 5
    entries = simulate_tags.build_entries(PREFIX, ["Press01", "Press02"], timestamps)

    assert len(entries) == 3 * 8
    assert [len(e["propertyValues"]) for e in entries[::8]] == [10, 10, 5]
    # Each run of 8 entries covers every stream once, so one request never piles onto one alias
    assert len({e["propertyAlias"] for e in entries[:8]}) == 8
    assert entries[0]["propertyAlias"] == f"{PREFIX}/Press01/N7_42_TT01"
    assert f"{PREFIX}/Press02/TT_OIL" in {e["propertyAlias"] for e in entries}
    assert len({e["entryId"] for e in entries}) == len(entries)
    assert all(len(e["entryId"]) <= 64 for e in entries)


def test_build_entries_truncates_long_entry_ids():
    with patch.dict(simulate_tags.RAW_TAGS, {"Press01": {"oil_temperature": "X" * 80}}):
        (entry,) = simulate_tags.build_entries(PREFIX, ["Press01"], [T0])
    assert len(entry["entryId"]) == 64


def test_send_batches_ten_entries_per_request():
    client = MagicMock()
    client.batch_put_asset_property_value.return_value = {"errorEntries": []}
    entries = [{"entryId": str(i)} for i in range(23)]

    assert simulate_tags.send(client, entries) == 0
    sizes = [len(c.kwargs["entries"]) for c in client.batch_put_asset_property_value.call_args_list]
    assert sizes == [10, 10, 3]


@patch("simulate_tags.time.sleep")
def test_send_retries_only_throttled_entries(sleep):
    client = MagicMock()
    client.batch_put_asset_property_value.side_effect = [
        {"errorEntries": [throttled("b")]},
        {"errorEntries": []},
    ]

    assert simulate_tags.send(client, [{"entryId": "a"}, {"entryId": "b"}]) == 0
    assert client.batch_put_asset_property_value.call_args.kwargs == {"entries": [{"entryId": "b"}]}
    sleep.assert_called_once_with(0.5)


@patch("simulate_tags.time.sleep")
def test_send_counts_rejected_and_persistently_throttled_entries(sleep, capsys):
    client = MagicMock()
    client.batch_put_asset_property_value.side_effect = (
        [{"errorEntries": [rejected("a"), throttled("b")]}] + [{"errorEntries": [throttled("b")]}] * 2
    )

    assert simulate_tags.send(client, [{"entryId": "a"}, {"entryId": "b"}], max_attempts=3) == 2
    assert [c.args[0] for c in sleep.call_args_list] == [0.5, 1.0, 2.0]
    assert capsys.readouterr().out == (
        "  a: InvalidRequestException bad alias\n"
        "  b: ThrottlingException Rate exceeded\n"
    )


@pytest.mark.parametrize("argv, message", [
    ([], "pass --backfill-hours and/or --live"),
    (["--backfill-hours", "168"], "cannot exceed 167"),
    (["--live", "--devices", "Press01,Press09"], "unknown devices: Press09"),
])
def test_main_rejects_bad_arguments(argv, message, capsys):
    with pytest.raises(SystemExit):
        run_main(*argv)
    assert message in capsys.readouterr().err


@patch("simulate_tags.time.time", return_value=T0)
def test_main_backfill_sends_schema_valid_requests(_, capsys):
    client = boto3.client("iotsitewise", region_name="us-west-2")
    with Stubber(client) as stub, patch("simulate_tags.boto3.client", return_value=client) as make_client:
        # Stubber checks each request against the real BatchPutAssetPropertyValue schema
        stub.add_response("batch_put_asset_property_value", {"errorEntries": []}, {"entries": ANY})
        run_main("--backfill-hours", "0.5", "--backfill-step", "300", "--devices", "Press02")
        stub.assert_no_pending_responses()

    make_client.assert_called_once_with("iotsitewise", region_name="us-west-2")
    out = capsys.readouterr().out
    assert f"{PREFIX}/Press02/{{TT_OIL,HYD_P,MTR_I,CYCLES}}" in out
    assert "Backfilling 6 points per stream (4 entries)..." in out
    assert "Backfill done, 0 failed entries" in out


@patch("simulate_tags.time.sleep", side_effect=[None, KeyboardInterrupt])
def test_main_live_publishes_until_interrupted(sleep, capsys):
    client = MagicMock()
    client.batch_put_asset_property_value.side_effect = [
        {"errorEntries": []},
        {"errorEntries": [rejected("Press01-CycleCnt-0")]},
    ]
    with patch("simulate_tags.boto3.client", return_value=client):
        run_main("--live", "--interval", "30")

    sleep.assert_called_with(30)
    lines = capsys.readouterr().out.splitlines()
    assert lines[-4].endswith(" sent")
    assert lines[-3] == "  Press01-CycleCnt-0: InvalidRequestException bad alias"
    assert lines[-2].endswith(" sent, 1 failed")
    assert lines[-1] == "Stopped"
