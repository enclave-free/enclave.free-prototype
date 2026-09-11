#!/usr/bin/env python3
"""Create and apply structured human/agent reviews against full evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.benches.quality_measurements import build_review_packet, measure_artifact, resource_artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("template", "apply"))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        artifact = json.loads(args.artifact.read_text())
        if artifact.get("schema") == "curated-resource-contact-evidence-v3":
            artifact = resource_artifact(artifact)
        if not artifact.get("candidates"):
            raise ValueError("expected a modern Conversation artifact with candidates; historical evidence needs explicit adaptation")
        if args.output.resolve() in {args.artifact.resolve(), args.review.resolve() if args.review else args.artifact.resolve()}:
            raise ValueError("write a new output file; never overwrite original evidence/review")
        if args.action == "template":
            result = build_review_packet(artifact)
            status = "unreviewed"
        else:
            if not args.review:
                raise ValueError("--review is required")
            result = {**artifact, "measurements": measure_artifact(artifact, json.loads(args.review.read_text()))}
            status = result["measurements"]["release_gate"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        print(f"Review status: {status}; output: {args.output}")
        return 0 if args.action == "template" or status == "passed" else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
