"""Download (and cache) a 17lands public replay-data CSV for a given set.

Files live at:
    https://17lands-public.s3.amazonaws.com/analysis_data/replay_data/
        replay_data_public.<SET>.<EVENT>.csv.gz

Where <SET> is a 3-letter Arena set code (EOE, OTJ, BLB, ...) and <EVENT> is
typically PremierDraft. Each file is ~300 MB gzipped (~2-3 GB uncompressed)
and contains one row per played game with full turn-by-turn state.

Usage:
    python -m tools.eval.seventeenlands.download --set EOE
    python -m tools.eval.seventeenlands.download --set OTJ --event TradDraft
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CACHE = REPO / "tools" / "eval" / "data" / "17lands"

BASE_URL = "https://17lands-public.s3.amazonaws.com/analysis_data/replay_data"


def url_for(set_code: str, event: str = "PremierDraft") -> str:
    return f"{BASE_URL}/replay_data_public.{set_code.upper()}.{event}.csv.gz"


def cache_path(set_code: str, event: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"replay_data_public.{set_code.upper()}.{event}.csv.gz"


def download(
    set_code: str, event: str = "PremierDraft", cache_dir: Path = DEFAULT_CACHE, force: bool = False
) -> Path:
    """Download the gzipped CSV if not already cached. Returns the local path."""
    out = cache_path(set_code, event, cache_dir)
    if out.exists() and not force:
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"[cache hit] {out} ({size_mb:.1f} MB) — pass --force to redownload")
        return out

    url = url_for(set_code, event)
    print(f"[download]  {url}")
    print(f"            -> {out}")

    tmp = out.with_suffix(out.suffix + ".part")
    started = time.time()
    bytes_written = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            content_length = int(resp.headers.get("Content-Length", "0") or "0")
            with open(tmp, "wb") as f:
                last_log = 0.0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)
                    now = time.time()
                    if now - last_log >= 2.0 or bytes_written == content_length:
                        pct = (bytes_written / content_length * 100) if content_length else 0
                        mb = bytes_written / (1024 * 1024)
                        rate = mb / max(0.001, now - started)
                        print(f"            {pct:5.1f}%  {mb:6.1f} MB  {rate:5.1f} MB/s", flush=True)
                        last_log = now
    except urllib.error.HTTPError as e:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        if e.code == 404:
            print(
                f"\nError: 404 Not Found at {url}\n"
                f"Check that <SET>={set_code!r} and <EVENT>={event!r} are correct.\n"
                f"Browse https://17lands-public.s3.amazonaws.com/ for available files.",
                file=sys.stderr,
            )
            sys.exit(2)
        raise

    tmp.replace(out)
    elapsed = time.time() - started
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"[done]      {size_mb:.1f} MB in {elapsed:.0f}s ({size_mb / elapsed:.1f} MB/s)")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "--set", required=True, dest="set_code", help="3-letter Arena set code (e.g. EOE, OTJ, BLB)"
    )
    parser.add_argument("--event", default="PremierDraft", help="Event type (default: PremierDraft)")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"Where to cache the CSV (default: {DEFAULT_CACHE})",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached")
    args = parser.parse_args()
    download(args.set_code, args.event, args.cache_dir, args.force)


if __name__ == "__main__":
    main()
