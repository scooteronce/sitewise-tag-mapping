from unittest.mock import call, patch

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import sync_tags
from conftest import asset, asset_summary, time_series, uid

PRESS = "slc.stamping.line3.press01"
LINE = "slc.stamping.line3"
ALIAS = "/slc-kep01/Channel1/Press01/N7_42_TT01"

# Two rows on the same line, so the second row reuses every asset above the presses
TAG_MAP = """\
stream_alias,site,area,line,equipment,equipment_type,property
/slc-kep01/Channel1/Press01/N7_42_TT01,slc,stamping,line3,press01,press,oil_temperature
/slc-kep01/Channel1/Press02/TT_OIL,slc,stamping,line3,press02,press,oil_temperature
"""


@pytest.fixture(autouse=True)
def clear_asset_cache():
    sync_tags._assets.clear()
    yield
    sync_tags._assets.clear()


@pytest.fixture
def stub():
    with Stubber(sync_tags.sw) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def test_client_uses_aws_region():
    assert sync_tags.sw.meta.region_name == "us-west-2"


def test_ensure_asset_returns_existing_asset_and_caches_it(stub):
    existing = asset(PRESS)
    stub.add_response("describe_asset", existing, {"assetId": f"externalId:{PRESS}"})

    assert sync_tags.ensure_asset(PRESS, "model-press") == existing
    # Second lookup is served from the cache; the stubber would fail on an extra call
    assert sync_tags.ensure_asset(PRESS, "model-press") is sync_tags._assets[PRESS]


def test_ensure_asset_creates_missing_asset_and_waits_for_active(stub, capsys):
    created = asset(PRESS)
    stub.add_client_error("describe_asset", "ResourceNotFoundException", expected_params={"assetId": f"externalId:{PRESS}"})
    stub.add_response(
        "create_asset",
        {"assetId": uid(PRESS), "assetArn": created["assetArn"], "assetStatus": {"state": "CREATING"}},
        {"assetName": PRESS, "assetModelId": "externalId:model-press", "assetExternalId": PRESS},
    )
    stub.add_response("describe_asset", created, {"assetId": uid(PRESS)})  # asset_active waiter
    stub.add_response("describe_asset", created, {"assetId": uid(PRESS)})

    assert sync_tags.ensure_asset(PRESS, "model-press") == created
    assert capsys.readouterr().out == f"creating asset {PRESS}\n"


def test_ensure_asset_reraises_other_errors(stub):
    stub.add_client_error("describe_asset", "AccessDeniedException")
    with pytest.raises(ClientError, match="AccessDenied"):
        sync_tags.ensure_asset(PRESS, "model-press")


def test_ensure_child_links_unlinked_child(stub, capsys):
    line = asset(LINE, hierarchies=["presses"])
    hierarchy = {"assetId": uid(LINE), "hierarchyId": uid(f"{LINE}/presses")}
    stub.add_response("list_associated_assets", {"assetSummaries": []}, hierarchy)
    stub.add_response("associate_assets", {}, {**hierarchy, "childAssetId": uid(PRESS)})

    sync_tags.ensure_child(line, "presses", asset(PRESS))
    assert capsys.readouterr().out == f"  linking {PRESS} under {LINE}\n"


def test_ensure_child_skips_already_linked_child(stub, capsys):
    stub.add_response("list_associated_assets", {"assetSummaries": [asset_summary(PRESS)]})

    sync_tags.ensure_child(asset(LINE, hierarchies=["presses"]), "presses", asset(PRESS))
    assert capsys.readouterr().out == ""


def test_ensure_stream_binds_disassociated_stream(stub, capsys):
    press = asset(PRESS, properties=["oil_temperature"])
    stub.add_response("describe_time_series", time_series(ALIAS), {"alias": ALIAS})
    stub.add_response(
        "associate_time_series_to_asset_property",
        {},
        {"alias": ALIAS, "assetId": uid(PRESS), "propertyId": uid(f"{PRESS}/oil_temperature")},
    )

    sync_tags.ensure_stream(ALIAS, press, "oil_temperature")
    assert capsys.readouterr().out == f"  binding {ALIAS} -> {PRESS}.oil_temperature\n"


def test_ensure_stream_skips_stream_already_bound_to_property(stub):
    press = asset(PRESS, properties=["oil_temperature"])
    stub.add_response("describe_time_series", time_series(ALIAS, uid(PRESS), uid(f"{PRESS}/oil_temperature")))

    sync_tags.ensure_stream(ALIAS, press, "oil_temperature")


def test_ensure_stream_rebinds_stream_bound_elsewhere(stub):
    press = asset(PRESS, properties=["oil_temperature"])
    stub.add_response("describe_time_series", time_series(ALIAS, uid("other-asset"), uid("other-property")))
    stub.add_response("associate_time_series_to_asset_property", {})

    sync_tags.ensure_stream(ALIAS, press, "oil_temperature")


def test_ensure_stream_skips_alias_with_no_data_yet(stub, capsys):
    stub.add_client_error("describe_time_series", "ResourceNotFoundException")

    sync_tags.ensure_stream(ALIAS, asset(PRESS, properties=["oil_temperature"]), "oil_temperature")
    assert capsys.readouterr().out == f"  skipping {ALIAS}: no data stream yet\n"


def test_ensure_stream_reraises_other_errors(stub):
    stub.add_client_error("describe_time_series", "ThrottlingException")
    with pytest.raises(ClientError, match="Throttling"):
        sync_tags.ensure_stream(ALIAS, asset(PRESS, properties=["oil_temperature"]), "oil_temperature")


def test_ensure_stream_exits_on_unknown_property():
    # No stubber: a typo in the CSV must fail before any API call
    with pytest.raises(SystemExit, match=f"unknown property 'oil_temp' on {PRESS} \\({ALIAS}\\)"):
        sync_tags.ensure_stream(ALIAS, asset(PRESS, properties=["oil_temperature"]), "oil_temp")


def test_main_walks_hierarchy_for_each_row(tmp_path):
    csv_path = tmp_path / "tag-map.csv"
    csv_path.write_text(TAG_MAP)
    with patch.object(sync_tags, "ensure_asset", side_effect=lambda ext_id, model: {"id": ext_id}) as ensure_asset, \
            patch.object(sync_tags, "ensure_child") as ensure_child, \
            patch.object(sync_tags, "ensure_stream") as ensure_stream:
        sync_tags.main(str(csv_path))

    assert ensure_asset.call_args_list[:4] == [
        call("slc", "model-site"),
        call("slc.stamping", "model-area"),
        call("slc.stamping.line3", "model-line"),
        call("slc.stamping.line3.press01", "model-press"),  # model from the equipment_type column
    ]
    assert ensure_asset.call_args_list[7] == call("slc.stamping.line3.press02", "model-press")
    # The top level has no parent to link under; every other level links to the one above it
    assert ensure_child.call_args_list[:3] == [
        call({"id": "slc"}, "areas", {"id": "slc.stamping"}),
        call({"id": "slc.stamping"}, "lines", {"id": "slc.stamping.line3"}),
        call({"id": "slc.stamping.line3"}, "presses", {"id": "slc.stamping.line3.press01"}),
    ]
    assert ensure_child.call_count == 6
    assert ensure_stream.call_args_list == [
        call("/slc-kep01/Channel1/Press01/N7_42_TT01", {"id": "slc.stamping.line3.press01"}, "oil_temperature"),
        call("/slc-kep01/Channel1/Press02/TT_OIL", {"id": "slc.stamping.line3.press02"}, "oil_temperature"),
    ]
