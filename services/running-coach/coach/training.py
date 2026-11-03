"""Coach-facing CLI for the constrained training read API."""

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    history = subparsers.add_parser("history")
    history.add_argument("--start", required=True)
    history.add_argument("--end", required=True, help="Exclusive end date")
    history.add_argument("--before", type=int)
    history.add_argument("--limit", type=int, default=100)
    activity = subparsers.add_parser("activity")
    activity.add_argument("id", type=int)
    activity.add_argument("--streams", action="store_true")
    args = parser.parse_args()
    token = Path(os.environ["COACH_DATA_TOKEN_FILE"]).read_text().strip()
    origin = os.getenv("COACH_DATA_URL", "http://coach-data:8080")
    if args.operation == "history":
        query = {"start": args.start, "end": args.end, "limit": args.limit}
        if args.before is not None:
            query["before"] = args.before
        path = "/activities?" + urllib.parse.urlencode(query)
    else:
        path = f"/activities/{args.id}?streams={str(args.streams).lower()}"
    request = urllib.request.Request(origin + path, headers={"Authorization": "Bearer " + token})
    # This internal capability must never be sent through the public egress proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise SystemExit("Training response exceeded byte budget")
    print(json.dumps(json.loads(data), indent=2))


if __name__ == "__main__":
    main()
