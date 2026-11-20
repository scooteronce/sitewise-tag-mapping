import os
import sys
import uuid
from datetime import datetime, timezone

# The scripts build their boto3 client at import time. Give them a fake region and
# credentials, and hide the developer's ~/.aws files, before any test imports them.
for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"):
    os.environ.pop(var, None)
os.environ.update(
    AWS_REGION="us-west-2",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_CONFIG_FILE=os.devnull,
    AWS_SHARED_CREDENTIALS_FILE=os.devnull,
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def uid(name):
    """Stable UUID per name, so expected params in a test can be written by name."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def asset(name, properties=(), hierarchies=()):
    """A describe_asset response for an asset whose ID is uid(name)."""
    return {
        "assetId": uid(name),
        "assetArn": f"arn:aws:iotsitewise:us-west-2:111122223333:asset/{uid(name)}",
        "assetName": name,
        "assetModelId": uid("model"),
        "assetProperties": [{"id": uid(f"{name}/{p}"), "name": p, "dataType": "DOUBLE"} for p in properties],
        "assetHierarchies": [{"id": uid(f"{name}/{h}"), "name": h} for h in hierarchies],
        "assetCreationDate": NOW,
        "assetLastUpdateDate": NOW,
        "assetStatus": {"state": "ACTIVE"},
    }


def asset_summary(name):
    """An entry of list_associated_assets' assetSummaries."""
    return {
        "id": uid(name),
        "arn": f"arn:aws:iotsitewise:us-west-2:111122223333:asset/{uid(name)}",
        "name": name,
        "assetModelId": uid("model"),
        "creationDate": NOW,
        "lastUpdateDate": NOW,
        "status": {"state": "ACTIVE"},
        "hierarchies": [],
    }


def time_series(alias, asset_id=None, property_id=None):
    """A describe_time_series response; leave asset_id out for a disassociated stream."""
    ts = {
        "alias": alias,
        "dataType": "DOUBLE",
        "timeSeriesId": uid(alias),
        "timeSeriesCreationDate": NOW,
        "timeSeriesLastUpdateDate": NOW,
        "timeSeriesArn": f"arn:aws:iotsitewise:us-west-2:111122223333:time-series/{uid(alias)}",
    }
    if asset_id:
        ts.update(assetId=asset_id, propertyId=property_id)
    return ts
