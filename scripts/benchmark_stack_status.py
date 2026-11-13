"""Measure read-only stack status queries; never start or stop a service."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", choices=("before", "after", "current"), default="current")
    args = parser.parse_args()
    report = {"process_count": len(list(Path("/proc").glob("[0-9]*"))), "queries": {}}
    for target in ("chat-server", "all"):
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            result = subprocess.run([str(ROOT / "openevent-stack/process.sh"), "status", target],
                                    capture_output=True, text=True, timeout=60)
            elapsed = time.perf_counter() - started
            # STOPPED and ORPHANED/DUPLICATE are valid status results, not
            # benchmark failures. Keep the actual output with each sample.
            if result.returncode not in (0, 3, 4):
                raise RuntimeError(result.stderr or result.stdout or
                                   f"status {target} exited {result.returncode}")
            samples.append({"seconds": elapsed, "returncode": result.returncode,
                            "stdout": result.stdout, "stderr": result.stderr})
        median = statistics.median(sample["seconds"] for sample in samples)
        report["queries"][target] = {"median_seconds": median, "samples": samples}
        print(f"status {target}: median {median:.4f}s (3 samples)", flush=True)
        print(samples[-1]["stdout"].rstrip(), flush=True)
    destination = ROOT / "build/benchmarks" / f"stack-status-{args.label}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {destination.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
