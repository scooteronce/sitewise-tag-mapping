"""
Publish simulated press tag data straight to AWS IoT SiteWise, standing in for a
SiteWise Edge gateway. Values are written by property alias, so each tag shows up
as a disassociated data stream, exactly what an OPC UA source on a gateway would
produce. The raw PLC tag names are deliberately inconsistent between presses
(e.g. /slc-kep01/Channel1/Press01/N7_42_TT01 vs /slc-kep01/Channel1/Press02/TT_OIL);
tag-map.csv is what gives them meaning.

  python simulate_tags.py --backfill-hours 24      # history for metrics and queries
  python simulate_tags.py --live                   # keep sending every 10s until Ctrl-C

Uses the standard AWS credential chain (AWS_PROFILE, AWS_REGION, ...).
"""
import argparse
import math
import random
import time
import zlib

import boto3

MAX_ENTRIES = 10        # BatchPutAssetPropertyValue: entries per request
MAX_VALUES = 10         # values per entry
MAX_BACKFILL_HOURS = 167  # SiteWise rejects timestamps older than 7 days

# measurement -> (SiteWise value type, generator(t_seconds, device_seed))
MEASUREMENTS = {
    'oil_temperature':    ('doubleValue',  lambda t, s: 120 + 8 * math.sin(2 * math.pi * (t + s * 3600) / 86400) + random.gauss(0, 1.0)),
    'hydraulic_pressure': ('doubleValue',  lambda t, s: 2200 + 150 * math.sin(2 * math.pi * t / 900) + random.gauss(0, 40)),
    'motor_current':      ('doubleValue',  lambda t, s: 45 + 5 * math.sin(2 * math.pi * t / 600) + random.gauss(0, 3.0)),
    # Derived from the clock so it only ever goes up, even across backfill and live runs
    'cycle_count':        ('integerValue', lambda t, s: int(t // 6) - 290_000_000 + s * 1000),
}

# device -> measurement -> raw PLC tag name, as the OPC UA server would expose it
RAW_TAGS = {
    'Press01': {'oil_temperature': 'N7_42_TT01', 'hydraulic_pressure': 'Hyd_Press_PSI',
                'motor_current': 'Motor_Amps_RAW', 'cycle_count': 'CycleCnt'},
    'Press02': {'oil_temperature': 'TT_OIL', 'hydraulic_pressure': 'HYD_P',
                'motor_current': 'MTR_I', 'cycle_count': 'CYCLES'},
}


def device_seed(device):
    return zlib.crc32(device.encode()) % 24


def property_value(measurement, t, seed):
    value_type, generate = MEASUREMENTS[measurement]
    return {
        'value': {value_type: generate(t, seed)},
        'timestamp': {'timeInSeconds': int(t), 'offsetInNanos': 0},
        'quality': 'GOOD',
    }


def build_entries(prefix, devices, timestamps):
    """ One entry per (alias, chunk of up to MAX_VALUES timestamps), interleaved across
    aliases so a request never piles onto one stream (SiteWise rate-limits per stream) """
    streams = [(device, measurement, tag) for device in devices for measurement, tag in RAW_TAGS[device].items()]
    entries = []
    for i in range(0, len(timestamps), MAX_VALUES):
        chunk = timestamps[i:i + MAX_VALUES]
        for device, measurement, tag in streams:
            seed = device_seed(device)
            entries.append({
                'entryId': f'{device}-{tag}-{i}'[:64],
                'propertyAlias': f'{prefix}/{device}/{tag}',
                'propertyValues': [property_value(measurement, t, seed) for t in chunk],
            })
    return entries


def send(client, entries, max_attempts=5):
    """ Returns the number of entries that still failed after retrying throttled ones """
    errors = {}
    for i in range(0, len(entries), MAX_ENTRIES):
        batch = entries[i:i + MAX_ENTRIES]
        for attempt in range(max_attempts):
            response = client.batch_put_asset_property_value(entries=batch)
            failed = {e['entryId']: e['errors'][0] for e in response.get('errorEntries', [])}
            retry = [entry for entry in batch if failed.get(entry['entryId'], {}).get('errorCode') == 'ThrottlingException']
            for entry_id, error in failed.items():
                if error['errorCode'] != 'ThrottlingException':
                    errors[entry_id] = error
            if not retry:
                break
            batch = retry
            time.sleep(0.5 * 2 ** attempt)
        else:
            errors.update({entry['entryId']: failed[entry['entryId']] for entry in batch})
    for entry_id, error in list(errors.items())[:5]:
        print(f"  {entry_id}: {error['errorCode']} {error['errorMessage']}")
    return len(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prefix', default='/slc-kep01/Channel1',
                        help='alias prefix: gateway measurementDataStreamPrefix + OPC UA browse path')
    parser.add_argument('--devices', default=','.join(RAW_TAGS), help=f'comma-separated subset of {",".join(RAW_TAGS)}')
    parser.add_argument('--backfill-hours', type=float, default=0, help=f'write history first (max {MAX_BACKFILL_HOURS})')
    parser.add_argument('--backfill-step', type=int, default=60, help='seconds between backfilled points')
    parser.add_argument('--live', action='store_true', help='keep publishing current values until Ctrl-C')
    parser.add_argument('--interval', type=int, default=10, help='seconds between live publishes')
    args = parser.parse_args()

    if not args.backfill_hours and not args.live:
        parser.error('pass --backfill-hours and/or --live')
    if args.backfill_hours > MAX_BACKFILL_HOURS:
        parser.error(f'--backfill-hours cannot exceed {MAX_BACKFILL_HOURS} (SiteWise 7-day limit)')

    client = boto3.client('iotsitewise')
    devices = [d.strip() for d in args.devices.split(',') if d.strip()]
    unknown = set(devices) - set(RAW_TAGS)
    if unknown:
        parser.error(f'unknown devices: {", ".join(sorted(unknown))}')
    for device in devices:
        print(f'{args.prefix}/{device}/{{{",".join(RAW_TAGS[device].values())}}}')

    if args.backfill_hours:
        now = int(time.time())
        start = now - int(args.backfill_hours * 3600)
        timestamps = list(range(start, now, args.backfill_step))
        entries = build_entries(args.prefix, devices, timestamps)
        print(f'Backfilling {len(timestamps)} points per stream ({len(entries)} entries)...')
        failures = send(client, entries)
        print(f'Backfill done, {failures} failed entries')

    if args.live:
        print(f'Publishing every {args.interval}s, Ctrl-C to stop')
        try:
            while True:
                failures = send(client, build_entries(args.prefix, devices, [time.time()]))
                print(f"{time.strftime('%H:%M:%S')} sent{'' if not failures else f', {failures} failed'}", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print('Stopped')


if __name__ == '__main__':
    main()
