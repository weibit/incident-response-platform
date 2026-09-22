"""Print signature only; never print the signing secret."""

import argparse
import hashlib
import hmac
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source", choices=["slack", "siem"])
parser.add_argument("timestamp")
parser.add_argument("file", type=Path)
args = parser.parse_args()
secret = os.environ[f"{args.source.upper()}_SIGNING_SECRET"]
prefix = "v0:" if args.source == "slack" else ""
body = (prefix + args.timestamp + ":").encode() + args.file.read_bytes()
signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
print(("v0=" if args.source == "slack" else "") + signature)
