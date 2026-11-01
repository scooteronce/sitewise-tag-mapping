# Tagging manufacturing data on AWS IoT SiteWise

Companion code for the dev.to article "Tagging Manufacturing Data on AWS: From PLC Tags to IoT SiteWise Asset Models".

| File | Purpose |
| --- | --- |
| `tag-map.csv` | Source of truth: raw stream alias to ISA-95 location and property |
| `asset-models.yaml` | CloudFormation: Site, Area, Line, Press models, metrics, and rollup |
| `simulate_tags.py` | Writes simulated raw tags to SiteWise in place of a SiteWise Edge gateway |
| `sync_tags.py` | Creates assets and binds streams from `tag-map.csv` (idempotent) |
| `cleanup_tags.py` | Removes the streams and assets created from `tag-map.csv` |

```bash
pip install -r requirements.txt
export AWS_REGION=us-west-2
aws cloudformation deploy --template-file asset-models.yaml --stack-name sitewise-asset-models
python simulate_tags.py --backfill-hours 24
python sync_tags.py tag-map.csv
python simulate_tags.py --live   # leave running 5+ minutes so metrics compute

# teardown
python cleanup_tags.py tag-map.csv
aws cloudformation delete-stack --stack-name sitewise-asset-models
```
