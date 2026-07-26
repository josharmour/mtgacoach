"""Upload an eval results JSON to the proxy admin endpoint.

Usage:
    python -m tools.eval.upload_results \\
        --json tools/eval/data/general_summary.json \\
        --proxy-url https://api.mtgacoach.com \\
        --admin-key "$MTGACOACH_ADMIN_KEY"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def upload(json_path: Path, proxy_url: str, admin_key: str) -> None:
    if not json_path.exists():
        print(f"Error: {json_path} does not exist", file=sys.stderr)
        sys.exit(1)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{proxy_url.rstrip('/')}/admin/api/eval/results",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Admin-Key": admin_key,
            # Cloudflare's WAF blocks Python's default urllib UA (~code 1010)
            # for the api.mtgacoach.com hostname. Send a realistic UA.
            "User-Agent": "mtgacoach-eval-uploader/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            print(f"OK {resp.status}: {content[:200]}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}", file=sys.stderr)
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument(
        "--proxy-url", default=os.environ.get("MTGACOACH_PROXY_URL", "https://api.mtgacoach.com")
    )
    parser.add_argument("--admin-key", default=os.environ.get("MTGACOACH_ADMIN_KEY", ""))
    args = parser.parse_args()
    if not args.admin_key:
        print("Error: --admin-key or MTGACOACH_ADMIN_KEY required", file=sys.stderr)
        sys.exit(1)
    upload(args.json, args.proxy_url, args.admin_key)


if __name__ == "__main__":
    main()
