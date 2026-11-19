#!/usr/bin/env python3
"""
bisect_runner.py
================
Binary-search over a git commit range: for each candidate commit it
  1. generates a per-commit sequence YAML in client/sequence/bisect/tmp/
  2. invokes sequence_runner.py --sequence <generated.yaml> --host <host>
  3. run_sequence success → good, failure → bad, narrows the range

The commit list is derived dynamically from the kernel repo using
  git log --first-parent <begin_commit>^..<end_commit>
so no hard-coded commit list is needed.

Usage
-----
    python client/bisect_runner.py --host 10.64.68.116 \\
        --begin_commit a1f6d71a69b0 --end_commit 32eb26c08e43
"""

import argparse
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bisect_runner")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CLIENT_DIR        = Path(__file__).parent
_DEFAULT_TEMPLATE = CLIENT_DIR / "sequence" / "bisect" / "bisect_template.yaml"
LOG_DIR           = Path("/tmp/serial_logs")

KERNEL_REPO = Path(
    "/local/mnt/workspace/yyj/develop_linux/git-repository"
    "/Talos_repository/github/qli-mainline/kernel"
)

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_commits(begin_commit: str, end_commit: str) -> list:
    """
    Return list of (hash, message) tuples ordered oldest → newest.

    Simple approach:
      1. git log --oneline -1000  (read recent local history)
      2. Search for end_commit and begin_commit in the output
      3. Slice everything between them (inclusive)
      4. Reverse to oldest → newest
    """
    if not KERNEL_REPO.is_dir():
        log.error("Kernel repo not found: %s", KERNEL_REPO)
        sys.exit(1)

    result = subprocess.run(
        ["git", "-C", str(KERNEL_REPO),
         "log", "--oneline", "--no-decorate", "-10000"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("git log failed:\n%s", result.stderr.strip())
        sys.exit(1)

    lines = result.stdout.strip().splitlines()

    # Find indices of end_commit (newer, appears first) and begin_commit (older)
    end_idx   = None
    begin_idx = None
    for i, line in enumerate(lines):
        short = line.split()[0]
        if end_idx is None and short.startswith(end_commit[:7]):
            end_idx = i
        if begin_idx is None and short.startswith(begin_commit[:7]):
            begin_idx = i

    if end_idx is None:
        log.error("end_commit %s not found in last 1000 commits", end_commit)
        sys.exit(1)
    if begin_idx is None:
        log.error("begin_commit %s not found in last 1000 commits", begin_commit)
        sys.exit(1)
    if end_idx > begin_idx:
        log.error("end_commit [%d] appears after begin_commit [%d] in log "
                  "(end must be newer than begin)", end_idx, begin_idx)
        sys.exit(1)

    # Slice end..begin inclusive, then reverse to oldest first
    selected = lines[end_idx : begin_idx + 1]
    commits = []
    for line in reversed(selected):
        parts = line.split(None, 1)
        commit_hash = parts[0]
        message     = parts[1] if len(parts) > 1 else ""
        commits.append((commit_hash, message))

    log.info("Found %d commits between %s and %s",
             len(commits), begin_commit, end_commit)
    return commits


def verify_kernel_repo(begin_commit: str, end_commit: str):
    """Print the commit range (newest first, like git log) before bisect starts."""
    result = subprocess.run(
        ["git", "-C", str(KERNEL_REPO), "log", "--first-parent",
         "--oneline", "--no-decorate", f"{begin_commit}..{end_commit}"],
        capture_output=True, text=True,
    )
    log.info("Commit range %s..%s (newest first):\n%s",
             begin_commit, end_commit, result.stdout.strip())


# ---------------------------------------------------------------------------
# Sequence helpers
# ---------------------------------------------------------------------------

def show_commit_list(commits: list):
    """Print commits oldest first with index, like git log --oneline but reversed."""
    log.info("Bisect range (%d commits, index 0=oldest):", len(commits))
    for i, (commit_hash, message) in enumerate(commits):
        log.info("  [%2d] %s %s", i, commit_hash, message)


def generate_sequence(commit_hash: str, template: Path, out_dir: Path, index: int) -> Path:
    """Render the template for commit_hash and write to bisect/tmp/<commit_hash>.yaml."""
    log_path = str(LOG_DIR / "2"/f"{index}_{commit_hash}_serial_log")
    rendered = (
        template.read_text(encoding="utf-8")
        .replace("{commit_id}", commit_hash)
        .replace("{log_path}", log_path)
    )
    out_path = out_dir / f"{commit_hash}.yaml"
    out_path.write_text(rendered, encoding="utf-8")
    log.info("Generated sequence → %s", out_path)
    return out_path


def run_sequence(host: str, seq_path: Path) -> bool:
    """Invoke sequence_runner.py and return True if it exited 0."""
    runner = CLIENT_DIR / "sequence_runner.py"
    cmd = [sys.executable, str(runner), "--host", host, "--sequence", str(seq_path)]
    log.info("Running: %s", " ".join(cmd))
    return subprocess.run(cmd).returncode == 0


def cleanup_sequence(seq_path: Path):
    """Delete the generated per-commit sequence YAML after use."""
    try:
        seq_path.unlink()
        log.info("Deleted generated sequence → %s", seq_path)
    except OSError as exc:
        log.warning("Could not delete %s: %s", seq_path, exc)


# ---------------------------------------------------------------------------
# Binary search
# ---------------------------------------------------------------------------

def bisect(host: str, commits: list, template: Path, out_dir: Path):
    """
    Binary search over commits (oldest → newest).
    commits[0] is assumed good, commits[-1] is assumed bad.
    run_sequence success → good, failure → bad.
    """
    lo = 0
    hi = len(commits) - 1
    index = 0

    log.info("Starting bisect: begin=[%d] %s %s  end=[%d] %s %s",
             lo, commits[lo][0], commits[lo][1],
             hi, commits[hi][0], commits[hi][1])

    while hi - lo > 1:
        mid = (lo + hi) // 2
        commit_hash, message = commits[mid]
        remaining = hi - lo - 1
        log.info("─" * 60)
        log.info("Bisect step [%d]: testing [%d] %s %s  (%d commits remaining)",
                 index, mid, commit_hash, message, remaining)

        seq_path = generate_sequence(commit_hash, template, out_dir, index)
        success  = run_sequence(host, seq_path)
        cleanup_sequence(seq_path)
        index += 1

        if success:
            lo = mid
            log.info("Result GOOD → new range [%d..%d]", lo, hi)
        else:
            hi = mid
            log.info("Result BAD  → new range [%d..%d]", lo, hi)

    log.info("═" * 60)
    log.info("Bisect complete!")
    log.info("  Last known good : [%d] %s %s", lo, commits[lo][0], commits[lo][1])
    log.info("  First known bad : [%d] %s %s", hi, commits[hi][0], commits[hi][1])
    log.info("  → Introducing commit is likely: %s %s", commits[hi][0], commits[hi][1])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Binary-search over commits using sequence_runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python client/bisect_runner.py --host 10.64.68.116 \\
                  --begin_commit a1f6d71a69b0 --end_commit 32eb26c08e43

              # custom template:
              python client/bisect_runner.py --host 10.64.68.116 \\
                  --begin_commit a1f6d71a69b0 --end_commit 32eb26c08e43 \\
                  --sequence client/sequence/bisect/bisect_template.yaml
        """),
    )
    parser.add_argument("--host",         required=True, help="Bridge server IP")
    parser.add_argument("--begin_commit", required=True, help="Known-good commit ID (oldest end of range)")
    parser.add_argument("--end_commit",   required=True, help="Known-bad commit ID (newest end of range)")
    parser.add_argument(
        "--sequence",
        default=str(_DEFAULT_TEMPLATE),
        help=(
            f"Path to bisect sequence template YAML (default: {_DEFAULT_TEMPLATE}). "
            "Generated per-commit sequences are written to the same directory."
        ),
    )
    args = parser.parse_args()

    template = Path(args.sequence)
    out_dir  = template.parent / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not template.exists():
        log.error("Template not found: %s", template)
        sys.exit(1)

    verify_kernel_repo(args.begin_commit, args.end_commit)

    commits = get_commits(args.begin_commit, args.end_commit)
    show_commit_list(commits)

    bisect(host=args.host, commits=commits, template=template, out_dir=out_dir)


if __name__ == "__main__":
    main()
