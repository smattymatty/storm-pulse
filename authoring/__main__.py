"""``python -m authoring`` - the release-side signer CLI (never on a node).

  python -m authoring keygen --private-out storm.key --public-out storm.pub
  python -m authoring sign  <source-tree> <dest-tree> --key storm.key

The public key from ``keygen`` is what an operator approves with
``stormpulse integration publisher add`` (or what the enroll bundle seeds).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from authoring import signer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m authoring",
        description="release-side Storm Pulse integration package signer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="generate an Ed25519 signing keypair")
    keygen.add_argument("--private-out", required=True)
    keygen.add_argument("--public-out", required=True)

    sign = sub.add_parser("sign", help="sign a package tree into a normalized copy")
    sign.add_argument("source")
    sign.add_argument("dest")
    sign.add_argument("--key", required=True, help="Ed25519 private key PEM file")

    args = parser.parse_args(argv)

    try:
        if args.command == "keygen":
            private_key = signer.generate_private_key()
            Path(args.private_out).write_bytes(signer.private_pem(private_key))
            Path(args.public_out).write_bytes(signer.public_pem(private_key))
            print(
                f"keypair written; fingerprint {signer.fingerprint_of(private_key.public_key())}",
                file=sys.stderr,
            )
            return 0
        if args.command == "sign":
            private_key = signer.load_private_key(Path(args.key).read_bytes())
            dest = signer.write_signed_package(
                Path(args.source), Path(args.dest), private_key
            )
            print(f"signed package written to {dest}", file=sys.stderr)
            return 0
    except signer.SigningError as exc:
        print(f"signing failed: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
