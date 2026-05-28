#!/bin/bash
# Read all telegram plugin trace logs across the fleet, merge by timestamp,
# print a chronological timeline. Useful for diagnosing bun plugin deaths.
#
# Usage:
#   ./diagnose_bun_deaths.sh              # all events, last 24h
#   ./diagnose_bun_deaths.sh shutdown     # filter to shutdown events
#   ./diagnose_bun_deaths.sh ppid_change  # filter to PPID drift events
#   ./diagnose_bun_deaths.sh --since 1h   # last hour

FILTER="${1:-}"
SINCE="${SINCE:-24h}"
if [ "$1" = "--since" ]; then
    SINCE="$2"
    FILTER="${3:-}"
fi

python3 << PYEOF
import json, os, sys
from glob import glob
from datetime import datetime, timezone, timedelta

since_arg = "$SINCE"
filter_event = "$FILTER"

# Parse "24h" / "1h" / "30m" into a timedelta
def parse_since(s):
    if not s: return timedelta(hours=24)
    unit = s[-1]
    val = int(s[:-1])
    return {'h': timedelta(hours=val), 'm': timedelta(minutes=val), 'd': timedelta(days=val)}.get(unit, timedelta(hours=24))

cutoff = datetime.now(timezone.utc) - parse_since(since_arg)

events = []
for trace_file in glob(os.path.expanduser('~/.claude/channels/telegram_*/lifecycle-trace.jsonl')):
    agent = os.path.basename(os.path.dirname(trace_file)).replace('telegram_', '')
    with open(trace_file) as f:
        for line in f:
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e['ts'].replace('Z', '+00:00'))
                if ts < cutoff:
                    continue
                if filter_event and e.get('event') != filter_event:
                    continue
                e['agent'] = agent
                e['_ts'] = ts
                events.append(e)
            except Exception:
                pass

events.sort(key=lambda e: e['_ts'])
print(f"{'time':>20}  {'agent':<10} {'pid':>6} {'event':<18} extra")
print("-" * 80)
for e in events:
    extra = {k: v for k, v in e.items() if k not in ('ts', 'pid', 'ppid', 'event', 'agent', '_ts')}
    extra_str = ' '.join(f"{k}={v}" for k, v in extra.items())
    print(f"{e['ts'][:19]:>20}  {e['agent']:<10} {e['pid']:>6} {e['event']:<18} {extra_str}")

print()
print(f"{len(events)} events from {len(glob(os.path.expanduser('~/.claude/channels/telegram_*/lifecycle-trace.jsonl')))} agents (since {since_arg})")
PYEOF
