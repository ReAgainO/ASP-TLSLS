"""Decode approved JSON attachments as bounded data, never executable input."""
import argparse
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import re


def decode(body):
    match = re.fullmatch(r"SIMULATION_ARCHIVE_V1\nsha256: ([0-9a-f]{64})\ndata: ([A-Za-z0-9+/=]+)", body.strip())
    if not match:
        raise ValueError("Invalid envelope")
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(match[2], validate=True))) as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != match[1]:
        raise ValueError("Invalid size/hash")
    json.loads(raw)
    return raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = decode(args.source.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)


if __name__ == "__main__":
    main()
