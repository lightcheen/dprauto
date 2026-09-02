#!/usr/bin/env python3
"""Materialize the 60 pinned GitHub snapshots without repository histories."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import shutil
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath


HERE = Path(__file__).resolve().parent
MAX_DOWNLOAD_BYTES = 24 * 1024 * 1024


def download(case: dict[str, object]) -> tuple[str, str]:
    repo = str(case["repository"])
    revision = str(case["revision"])
    target = HERE / str(case["local_path"])
    if target.is_dir():
        return repo, "already-present"
    if target.exists():
        raise RuntimeError(f"target exists but is not a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://codeload.github.com/{repo}/tar.gz/{revision}"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "dprauto-corpus60"})
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(payload) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"snapshot exceeds {MAX_DOWNLOAD_BYTES} bytes")
            # A proxy can close a response cleanly even though the gzip stream
            # is incomplete. Parse the member table inside the retry boundary.
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                members = archive.getmembers()
            break
        except Exception as exc:  # network errors need bounded retries
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    else:  # pragma: no cover
        raise RuntimeError(str(last_error))

    with tempfile.TemporaryDirectory(prefix="corpus60-", dir=target.parent) as temporary:
        temp_root = Path(temporary)
        top_levels: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise RuntimeError(f"unsafe archive member for {repo}: {member.name}")
            top_levels.add(path.parts[0])
        if len(top_levels) != 1:
            raise RuntimeError(f"unexpected archive roots for {repo}: {sorted(top_levels)}")

        def safe_data_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
            try:
                return tarfile.data_filter(member, destination)
            except tarfile.FilterError:
                # Some upstream repositories contain absolute or escaping
                # documentation/deployment links. They are not safe corpus
                # members, and DPRAuto's scanner does not follow symlinks.
                return None

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            archive.extractall(temp_root, filter=safe_data_filter)
        extracted = temp_root / next(iter(top_levels))
        if not extracted.is_dir():
            raise RuntimeError(f"archive root missing for {repo}")
        shutil.move(str(extracted), str(target))
    return repo, f"downloaded-{len(payload) // 1024}KiB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", choices=("cpp", "python", "java"))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.workers <= 12:
        raise SystemExit("--workers must be between 1 and 12")

    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    cases = [
        case
        for case in manifest["cases"]
        if args.language is None or case["language"] == args.language
    ]
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, case): case for case in cases}
        for future in concurrent.futures.as_completed(futures):
            repo = futures[future]["repository"]
            try:
                name, status = future.result()
                print(f"{name}\t{status}", flush=True)
            except Exception as exc:
                failures += 1
                print(f"{repo}\tFAILED\t{type(exc).__name__}: {exc}", flush=True)
    if failures:
        raise SystemExit(f"{failures} snapshot(s) failed")
    print(f"materialized {len(cases)} snapshot(s)")


if __name__ == "__main__":
    main()
