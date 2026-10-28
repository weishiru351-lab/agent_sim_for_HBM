"""Verify, replay or export completed experiments from pinned local Git history.

No checkout, network, archive edits or model calls. The temporary tree is removed
on exit; measured replay cost is separate from the original search budget.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
STUDIES = {
    "rules": ("rule_study_v1", "6904e0081142cdf5f51da91871443e09b28b64d7"),
    "v2": ("agent_v2_pilot", "48f2476ba20eb5988561ea6909a12d42b4598523"),
    "v3": ("agent_v3_pilot", "48f2476ba20eb5988561ea6909a12d42b4598523"),
}


def verify_sources(tree, study):
    """Respect original V2 byte hashes and rules/V3 text-normalized hashes."""
    protocol = json.loads((tree / "results" / study / "protocol.json").read_text(encoding="utf-8"))
    for name, expected in protocol["source_hashes"].items():
        path = tree / (name if "/" in name else "hbm_toy/" + name)
        content = path.read_bytes() if protocol["version"] == 2 else path.read_text(encoding="utf-8").encode()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"Historical source mismatch: {name}")
    return protocol


def export_snapshot(tree, key):
    study, revision = STUDIES[key]
    git = ["git", "-c", "safe.directory=" + ROOT.as_posix(), "-C", str(ROOT)]
    # V2 diagnosis consumes V2 results; the shared V2/V3 source tree includes both.
    records = [study] if key == "rules" else ["agent_v2_pilot", "agent_v2_diagnosis", "agent_v3_pilot"]
    paths = ["hbm_toy", "tests", "examples", *("results/" + name for name in records)]
    result = subprocess.run(git + ["archive", revision, *paths], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Pinned Git revision unavailable; obtain full repository history. "
                           + result.stderr.decode("utf-8", errors="replace"))
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = (tree / member.name).resolve()
            if not target.is_relative_to(tree.resolve()):
                raise ValueError("Invalid archive path")
            data = archive.extractfile(member).read()
            # Refuse to silently replay a different JSON archive than the user's.
            if member.name.startswith("results/") and member.name.endswith(".json"):
                local = (ROOT / member.name).read_bytes()
                if data != local:
                    # Most Git JSON is LF-normalized. Only the original byte-hashed
                    # V2 proposal/comparison artifacts must preserve exact bytes.
                    byte_frozen = member.name.startswith("results/agent_v2_pilot/") and (
                        "/one_shot/" in member.name or member.name.endswith("/comparison.json"))
                    if byte_frozen or data.replace(b"\r\n", b"\n") != local.replace(b"\r\n", b"\n"):
                        raise ValueError(f"Local archive differs from pinned revision: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return verify_sources(tree, study)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", choices=(*STUDIES, "all"))
    parser.add_argument("--check-only", action="store_true", help="verify source/data without simulating")
    parser.add_argument("--export", type=Path, help="restore source, tests and records into a NEW directory, without running")
    parser.add_argument("--tests", action="store_true", help="also run the original Python tests in the temporary tree")
    args = parser.parse_args()
    if args.export and (args.check_only or args.tests):
        parser.error("--export cannot be combined with --check-only or --tests")
    if args.export and args.export.exists():
        parser.error("--export requires a new directory; existing files are never overwritten")
    selected = STUDIES if args.study == "all" else {args.study: STUDIES[args.study]}
    try:
        for key, (study, revision) in selected.items():
            with tempfile.TemporaryDirectory(prefix="hbm_history_") as tmp:
                tree = Path(tmp)
                export_snapshot(tree, key)
                print(f"{key}: source/data verified at {revision}", flush=True)
                if args.export:
                    target = args.export / key if args.study == "all" else args.export
                    shutil.copytree(tree, target)
                    print(f"Original workflow restored to {target}", flush=True)
                    continue
                if args.tests:
                    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=tree, check=True)
                if args.check_only or key == "rules":
                    if key == "rules":
                        print("Rules: integrity checked; use --export for original discover/validate commands.")
                    continue
                module = "hbm_toy.agent_v2_report" if key == "v2" else "hbm_toy.agent_v3_report"
                # The original V2 report does not call check_frozen itself.
                if key == "v2":
                    subprocess.run([sys.executable, "-c",
                                    "from pathlib import Path; from hbm_toy.agent_v2 import check_frozen; "
                                    "check_frozen(Path('results/agent_v2_pilot'))"], cwd=tree, check=True)
                subprocess.run([sys.executable, "-m", module, "replay", "results/" + study], cwd=tree, check=True)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Historical replay failed: {error}\n")


if __name__ == "__main__":
    main()
