# cleanup_tags.py: tear down everything sync_tags.py created, driven by the same CSV
import csv
import os
import sys

import boto3
from botocore.exceptions import ClientError

# boto3 only reads AWS_DEFAULT_REGION on its own; also honor AWS_REGION like the CLI does
sw = boto3.client("iotsitewise", region_name=os.environ.get("AWS_REGION"))
LEVELS = ["site", "area", "line", "equipment"]


def describe(ext_id):
    try:
        return sw.describe_asset(assetId=f"externalId:{ext_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def main(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    # 1. Unbind and delete every data stream in the CSV
    for row in rows:
        alias = row["stream_alias"]
        try:
            ts = sw.describe_time_series(alias=alias)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                continue
            raise
        if ts.get("assetId"):
            print(f"unbinding {alias}")
            sw.disassociate_time_series_from_asset_property(
                alias=alias, assetId=ts["assetId"], propertyId=ts["propertyId"]
            )
        print(f"deleting stream {alias}")
        sw.delete_time_series(alias=alias)

    # 2. Delete assets bottom-up: equipment, then line, area, site
    for depth in range(len(LEVELS), 0, -1):
        ext_ids = sorted({".".join(r[c] for c in LEVELS[:depth]) for r in rows})
        for ext_id in ext_ids:
            asset = describe(ext_id)
            if not asset:
                continue
            parent_ext = ext_id.rsplit(".", 1)[0] if depth > 1 else None
            parent = describe(parent_ext) if parent_ext else None
            if parent:
                for h in parent["assetHierarchies"]:
                    children = sw.list_associated_assets(
                        assetId=parent["assetId"], hierarchyId=h["id"]
                    )["assetSummaries"]
                    if any(c["id"] == asset["assetId"] for c in children):
                        sw.disassociate_assets(
                            assetId=parent["assetId"],
                            hierarchyId=h["id"],
                            childAssetId=asset["assetId"],
                        )
            print(f"deleting asset {ext_id}")
            sw.delete_asset(assetId=asset["assetId"])
            sw.get_waiter("asset_not_exists").wait(assetId=asset["assetId"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tag-map.csv")
