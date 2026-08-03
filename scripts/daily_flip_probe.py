# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W3-3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Deployment entry-point that reads
#     the W3-2 audit stream, runs the W3-1 analyzer, and evaluates the W3
#     Phase C shadow->enforce flip criterion.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""W3-3 daily flip probe -- deployment-facing entry-point.

Reads the durable W3-2 audit stream, runs the W3-1 consistency analyzer
over the trailing observation window, and evaluates the W3 Phase C
shadow -> enforce flip criterion. Prints the resulting
:class:`~oiagent_coworker.permissions.consistency.FlipCriterion` and
surfaces storage-level corruption counters to ops output.

This is a DEPLOYMENT entry-point (W6): it is the ONLY place a real clock
is read (``datetime.now(UTC)``), exempt from the library no-clock-read
red line. The library modules (``audit_tee.py`` / ``audit_stream.py`` /
``consistency.py``) never read a clock.

Cron-friendly: exit code 0 when the flip criterion is ready, non-zero
otherwise.

Usage::

    python scripts/daily_flip_probe.py \\
        --stream-path /var/log/oiagent/audit/permission_stream.jsonl \\
        --min-sample 1000 --window-days 7
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit_stream import read_audit_stream
from oiagent_coworker.permissions.consistency import (
    analyze_consistency,
    check_flip_criterion,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="daily_flip_probe",
        description=(
            "W3-3 daily flip probe: read the audit stream over the "
            "trailing window, run the consistency analyzer, and evaluate "
            "the shadow -> enforce flip criterion. Exit 0 when ready."
        ),
    )
    parser.add_argument(
        "--stream-path",
        required=True,
        type=Path,
        help="Path to the append-only JSONL permission audit stream.",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=1000,
        help="Minimum total_compared floor for the flip criterion "
        "(default: 1000).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Observation window length in days (default: 7).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the flip probe. Returns 0 when ready, non-zero otherwise."""
    args = _parse_args(argv)

    # Real clock read -- deployment entry-point exemption (W6).
    now = datetime.now(UTC)
    window = timedelta(days=args.window_days)

    # W3-2 probe recipe: reader bounds are a coarse I/O pre-filter only;
    # the analyzer window is authoritative.
    envelopes, stats = read_audit_stream(
        args.stream_path,
        since=now - window,
        until=now,
    )
    report = analyze_consistency(envelopes, now=now, window=window)
    flip = check_flip_criterion(report, min_sample=args.min_sample)

    print(f"stream_path: {args.stream_path}")
    print(f"now (UTC):   {now.isoformat()}")
    print(f"window:      {window}")
    print(f"min_sample:  {args.min_sample}")
    print(
        "read:        lines_read=%d envelopes_yielded=%d "
        "corrupt_lines=%d corrupt_last_line_truncated=%s"
        % (
            stats.lines_read,
            stats.envelopes_yielded,
            stats.corrupt_lines,
            stats.corrupt_last_line_truncated,
        )
    )
    print(
        "report:      total_compared=%d agreements=%d mismatches=%d "
        "new_engine_errors=%d malformed_records=%d consistency=%s"
        % (
            report.total_compared,
            report.agreements,
            report.mismatches,
            report.new_engine_errors,
            report.malformed_records,
            (
                f"{report.consistency:.4f}"
                if report.consistency is not None
                else "None"
            ),
        )
    )
    print(f"flip.ready:  {flip.ready}")
    print(f"flip.reason: {flip.reason}")

    return 0 if flip.ready else 1


if __name__ == "__main__":
    sys.exit(main())
