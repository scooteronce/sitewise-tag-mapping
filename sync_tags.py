# sync_tags.py
import csv
import os
import sys

import boto3
from botocore.exceptions import ClientError

# boto3 only reads AWS_DEFAULT_REGION on its own; also honor AWS_REGION like the CLI does
sw = boto3.client("iotsitewise", region_name=os.environ.get("AWS_REGION"))

LEVELS = [
    # (csv column, asset model external ID, hierarchy name on the parent)
    ("site", "model-site", None),
    ("area", "model-area", "areas"),
    ("line", "model-line", "lines"),
    ("equipment", None, "presses"),  # model comes from equipment_type
]
EQUIPMENT_MODELS = {"press": "model-press"}

_assets = {}  # external ID -> describe_asset response


def ensure_asset(ext_id, model_ext_id):
    if ext_id in _assets:
        return _assets[ext_id]
    try:
        asset = sw.describe_asset(assetId=f"externalId:{ext_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"creating asset {ext_id}")
        created = sw.create_asset(
            assetName=ext_id,
            assetModelId=f"externalId:{model_ext_id}",
            assetExternalId=ext_id,
        )
        sw.get_waiter("asset_active").wait(assetId=created["assetId"])
        asset = sw.describe_asset(assetId=created["assetId"])
    _assets[ext_id] = asset
    return asset


def ensure_child(parent, hierarchy_name, child):
    hierarchy_id = next(
        h["id"] for h in parent["assetHierarchies"] if h["name"] == hierarchy_name
    )
    existing = sw.list_associated_assets(
        assetId=parent["assetId"], hierarchyId=hierarchy_id
    )["assetSummaries"]
    if not any(a["id"] == child["assetId"] for a in existing):
        print(f"  linking {child['assetName']} under {parent['assetName']}")
        sw.associate_assets(
            assetId=parent["assetId"],
            hierarchyId=hierarchy_id,
            childAssetId=child["assetId"],
        )


def ensure_stream(alias, asset, property_name):
    prop_id = next(
        (p["id"] for p in asset["assetProperties"] if p["name"] == property_name), None
    )
    if prop_id is None:
        sys.exit(f"unknown property {property_name!r} on {asset['assetName']} ({alias})")
    try:
        ts = sw.describe_time_series(alias=alias)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        # The gateway hasn't published this tag yet; a later run will bind it
        print(f"  skipping {alias}: no data stream yet")
        return
    if ts.get("assetId") == asset["assetId"] and ts.get("propertyId") == prop_id:
        return
    print(f"  binding {alias} -> {asset['assetName']}.{property_name}")
    sw.associate_time_series_to_asset_property(
        alias=alias, assetId=asset["assetId"], propertyId=prop_id
    )


def main(path):
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            parent, path_parts = None, []
            for column, model_ext_id, hierarchy in LEVELS:
                path_parts.append(row[column])
                model = model_ext_id or EQUIPMENT_MODELS[row["equipment_type"]]
                asset = ensure_asset(".".join(path_parts), model)
                if parent:
                    ensure_child(parent, hierarchy, asset)
                parent = asset
            ensure_stream(row["stream_alias"], parent, row["property"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tag-map.csv")
