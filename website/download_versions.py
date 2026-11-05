"""Read the same published indexes used by the desktop updater."""

import asyncio
import re
import time

import httpx

PLATFORMS = ("win-x64", "osx-arm64", "linux-x64")
_versions: dict[str, dict[str, str]] = {"master": {}, "stable": {}}
_checked_at = -float("inf")
_lock = asyncio.Lock()


def latest_version(index):
    versions = [
        asset.get("Version", "")
        for asset in index.get("Assets", [])
        if asset.get("Type") == "Full" and re.fullmatch(r"\d+\.\d+\.\d+", asset.get("Version", ""))
    ]
    return max(versions, key=lambda value: tuple(map(int, value.split("."))), default=None)


async def published_versions():
    global _checked_at
    async with _lock:
        if time.monotonic() - _checked_at < 300:
            return {lane: dict(versions) for lane, versions in _versions.items()}
        async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:

            async def read(lane, platform):
                url = (
                    "https://github.com/josharmour/mtgacoach/releases/download/"
                    f"coach-{lane}-feed/releases.{lane}-{platform}.json"
                )
                try:
                    response = await asyncio.wait_for(client.get(url), timeout=4)
                    response.raise_for_status()
                    version = latest_version(response.json())
                    if version:
                        _versions[lane][platform] = version
                except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
                    pass  # Keep the last published value; never substitute the source version.

            await asyncio.gather(*(read(lane, platform) for lane in _versions for platform in PLATFORMS))
        _checked_at = time.monotonic()
        return {lane: dict(versions) for lane, versions in _versions.items()}
