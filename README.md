# Tagging manufacturing data on AWS IoT SiteWise

Companion code for the dev.to article "Tagging Manufacturing Data on AWS: From PLC Tags to IoT SiteWise Asset Models".

| File | Purpose |
| --- | --- |
| `tag-map.csv` | Source of truth: raw stream alias to ISA-95 location and property |
| `asset-models.yaml` | CloudFormation: Site, Area, Line, Press models, metrics, and rollup |
| `simulate_tags.py` | Writes simulated raw tags to SiteWise in place of a SiteWise Edge gateway |
| `sync_tags.py` | Creates assets and binds streams from `tag-map.csv` (idempotent) |
| `cleanup_tags.py` | Removes the streams and assets created from `tag-map.csv` |

## Prerequisites

- Python 3.10 or later
- AWS credentials for the account you want to use (set `AWS_PROFILE` if you have more than one profile)
- A region where AWS IoT SiteWise is available; see the [SiteWise endpoints list](https://docs.aws.amazon.com/general/latest/gr/iot-sitewise.html)

AWS IoT SiteWise is not part of the AWS Free Tier. A full run of the steps below costs a few cents.

## Run it

```bash
pip install -r requirements.txt
export AWS_REGION=<your-region>   # e.g. us-east-1, eu-west-1; the scripts also accept AWS_DEFAULT_REGION
aws cloudformation deploy --template-file asset-models.yaml --stack-name sitewise-asset-models
python simulate_tags.py --backfill-hours 24
python sync_tags.py tag-map.csv
python simulate_tags.py --live   # leave running 5+ minutes so metrics compute
```

Metrics only compute for data that arrives after `sync_tags.py` binds a stream to its property. The backfilled history is visible to queries such as `execute-query`, but metric values start with the `--live` run.

If a row in `tag-map.csv` points at a stream that has no data yet, `sync_tags.py` skips it with a warning. Run it again once data arrives.

## Teardown

```bash
python cleanup_tags.py tag-map.csv
aws cloudformation delete-stack --stack-name sitewise-asset-models
```

`cleanup_tags.py` deletes the data streams listed in `tag-map.csv`, including their stored history, and the assets created from it. Run it before deleting the stack; the asset models can't be deleted while assets still use them.
