import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import cleanup_tags
from conftest import asset, asset_summary, time_series, uid

PRESS, LINE, AREA, SITE = "slc.stamping.line3.press01", "slc.stamping.line3", "slc.stamping", "slc"
BOUND = "/slc-kep01/Channel1/Press01/N7_42_TT01"
NEVER_PUBLISHED = "/slc-kep01/Channel1/Press01/Hyd_Press_PSI"
UNBOUND = "/slc-kep01/Channel1/Press01/CycleCnt"

TAG_MAP = f"""\
stream_alias,site,area,line,equipment,equipment_type,property
{BOUND},slc,stamping,line3,press01,press,oil_temperature
{NEVER_PUBLISHED},slc,stamping,line3,press01,press,hydraulic_pressure
{UNBOUND},slc,stamping,line3,press01,press,cycle_count
"""


@pytest.fixture
def stub():
    with Stubber(cleanup_tags.sw) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def expect_delete(stub, name):
    stub.add_response("delete_asset", {"assetStatus": {"state": "DELETING"}}, {"assetId": uid(name)})
    # asset_not_exists waiter succeeds on the first poll
    stub.add_client_error("describe_asset", "ResourceNotFoundException", expected_params={"assetId": uid(name)})


def test_client_uses_aws_region():
    assert cleanup_tags.sw.meta.region_name == "us-west-2"


def test_describe_returns_none_for_missing_asset(stub):
    stub.add_client_error("describe_asset", "ResourceNotFoundException", expected_params={"assetId": "externalId:slc"})
    assert cleanup_tags.describe("slc") is None


def test_describe_reraises_other_errors(stub):
    stub.add_client_error("describe_asset", "AccessDeniedException")
    with pytest.raises(ClientError, match="AccessDenied"):
        cleanup_tags.describe("slc")


def test_main_deletes_streams_then_assets_bottom_up(stub, tmp_path, capsys):
    csv_path = tmp_path / "tag-map.csv"
    csv_path.write_text(TAG_MAP)
    line = asset(LINE, hierarchies=["presses"])
    area = asset(AREA, hierarchies=["lines"])

    # 1. Streams: unbind before deleting; skip aliases that never got data
    stub.add_response("describe_time_series", time_series(BOUND, uid(PRESS), uid("prop")), {"alias": BOUND})
    stub.add_response(
        "disassociate_time_series_from_asset_property", {}, {"alias": BOUND, "assetId": uid(PRESS), "propertyId": uid("prop")}
    )
    stub.add_response("delete_time_series", {}, {"alias": BOUND})
    stub.add_client_error("describe_time_series", "ResourceNotFoundException", expected_params={"alias": NEVER_PUBLISHED})
    stub.add_response("describe_time_series", time_series(UNBOUND), {"alias": UNBOUND})
    stub.add_response("delete_time_series", {}, {"alias": UNBOUND})

    # 2a. Press: unlinked from its line, then deleted
    stub.add_response("describe_asset", asset(PRESS), {"assetId": f"externalId:{PRESS}"})
    stub.add_response("describe_asset", line, {"assetId": f"externalId:{LINE}"})
    presses = {"assetId": uid(LINE), "hierarchyId": uid(f"{LINE}/presses")}
    stub.add_response("list_associated_assets", {"assetSummaries": [asset_summary(PRESS)]}, presses)
    stub.add_response("disassociate_assets", {}, {**presses, "childAssetId": uid(PRESS)})
    expect_delete(stub, PRESS)
    # 2b. Line: its area exists but no longer lists it, so there is nothing to unlink
    stub.add_response("describe_asset", line, {"assetId": f"externalId:{LINE}"})
    stub.add_response("describe_asset", area, {"assetId": f"externalId:{AREA}"})
    stub.add_response("list_associated_assets", {"assetSummaries": []})
    expect_delete(stub, LINE)
    # 2c. Area: already gone
    stub.add_client_error("describe_asset", "ResourceNotFoundException", expected_params={"assetId": f"externalId:{AREA}"})
    # 2d. Site: top level, no parent to look up
    stub.add_response("describe_asset", asset(SITE), {"assetId": f"externalId:{SITE}"})
    expect_delete(stub, SITE)

    cleanup_tags.main(str(csv_path))

    assert capsys.readouterr().out.splitlines() == [
        f"unbinding {BOUND}",
        f"deleting stream {BOUND}",
        f"deleting stream {UNBOUND}",
        f"deleting asset {PRESS}",
        f"deleting asset {LINE}",
        f"deleting asset {SITE}",
    ]


def test_main_reraises_unexpected_stream_errors(stub, tmp_path):
    csv_path = tmp_path / "tag-map.csv"
    csv_path.write_text(TAG_MAP)
    stub.add_client_error("describe_time_series", "AccessDeniedException")
    with pytest.raises(ClientError, match="AccessDenied"):
        cleanup_tags.main(str(csv_path))
