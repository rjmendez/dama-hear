#!/usr/bin/env python3
"""Purge every clip that holds a human voice, and print the receipts that prove it happened.

    python3 tools/hear_privacy_purge.py --pool ~/hear-clips --dry-run    # preview, touches nothing
    python3 tools/hear_privacy_purge.py --pool ~/hear-clips              # destroy + audit
    python3 tools/hear_privacy_purge.py --pool ~/hear-clips --threshold 0.35 --min-speech-ms 150
    python3 tools/hear_privacy_purge.py --clip one.wav --audit-log ~/audit/purged.jsonl --json

This is the operator face of `hear/privacy/purge.py`. The module holds the policy and the
reasoning; this file parses arguments, prints a report and sets an exit code.

⚠️DRY RUN IS NOT THE DEFAULT, AND THAT IS DELIBERATE. Every other evidence tool here defaults to
`--plan` because its live mode changes a production system. This one's live mode DELETES A VOICE
RECORDING, which is the safe direction: a run that silently previewed would leave speech on disk
while reporting that it had found it, and "the cron job was in dry-run" is precisely how a
retention policy becomes a document nobody enforces. `--dry-run` exists for the operator asking
"what would this take?" and it touches neither the clips nor the audit log.

⚠️EXIT CODES ARE FOR A CRON JOB, NOT FOR A HUMAN.

  0  every clip was decided by the detector and the pool is clean.
  2  the run STOPPED, or a clip could not be handled at all: a purge that failed twice leaves a
     file this run judged speech-bearing on the disk, and scanning past it would finish green
     while the thing this tool exists to prevent is true (contract §9).
  3  the pool is clean, but one or more clips were destroyed WITHOUT being scored -- unreadable
     header, refused rate, detector fault. Fail-closed is the correct outcome and it is still an
     alarm: a lane that quietly destroys the backlog because a dependency broke has stopped
     being a privacy control and become a shredder.

⚠️WHAT IS PRINTED IS WHAT IS RECORDED. The per-clip line carries the receipt's own fields and
nothing else: no waveform statistics, no "sounded like" annotation, no path outside the pool.
`--json` emits the same receipts as JSONL on stdout so a pipeline can consume them without
parsing prose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear.privacy import purge as P  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hear_privacy_purge",
        description="Detect human speech in stored clips and destroy every clip that has it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A purged clip is gone. The receipt in the audit log is all that survives it.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pool", help="directory of clips to scan, recursively")
    src.add_argument("--clip", help="one WAV file to decide about")
    ap.add_argument("--threshold", type=float, default=P.DEFAULT_THRESHOLD,
                    help="per-frame speech probability that counts as speech "
                         "(default %(default)s; lower purges more)")
    ap.add_argument("--min-speech-ms", type=float, default=P.DEFAULT_MIN_SPEECH_MS,
                    help="shortest merged run of speech that triggers a purge "
                         "(default %(default)s ms)")
    ap.add_argument("--neg-threshold", type=float, default=P.DEFAULT_NEG_THRESHOLD,
                    help="a segment closes only below this (hysteresis; default %(default)s). "
                         "Raising it to --threshold reintroduces segment chatter")
    ap.add_argument("--min-silence-ms", type=float, default=P.DEFAULT_MIN_SILENCE_MS,
                    help="silence shorter than this is bridged, not a segment end "
                         "(default %(default)s ms)")
    ap.add_argument("--speech-pad-ms", type=float, default=P.DEFAULT_SPEECH_PAD_MS,
                    help="margin added to each end of an accepted segment (default %(default)s ms)")
    ap.add_argument("--receipt-clean", action="store_true",
                    help="also write a NO_SPEECH receipt for every clip that was scored and "
                         "kept, so the audit log records coverage and not only destructions")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be purged; no file and no audit line is written")
    ap.add_argument("--audit-log", default=None,
                    help="JSONL receipt sink (default <pool>/%s; ignored in a dry run)"
                         % P.RECEIPT_NAME)
    ap.add_argument("--vad", default="auto", choices=("auto", "silero", "band_energy"),
                    help="detector: silero v5 if installed, else the stdlib fallback "
                         "(default %(default)s)")
    ap.add_argument("--json", action="store_true",
                    help="print receipts as JSONL on stdout instead of a table")
    ap.add_argument("--quiet", action="store_true", help="summary line only")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = args.pool or args.clip
    target = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(target):
        print("no such pool or clip: %s" % target, file=sys.stderr)
        return 2
    if args.threshold <= 0.0 or args.threshold > 1.0:
        print("--threshold must be in (0, 1]; %r is not a probability" % args.threshold,
              file=sys.stderr)
        return 2
    if args.min_speech_ms < 0.0:
        print("--min-speech-ms cannot be negative", file=sys.stderr)
        return 2
    if args.neg_threshold > args.threshold:
        print("--neg-threshold %.3f is above --threshold %.3f: a segment that cannot close is "
              "not hysteresis" % (args.neg_threshold, args.threshold), file=sys.stderr)
        return 2

    try:
        vad = P.load_vad(args.vad, args.threshold, args.min_speech_ms)
    except P.PurgeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    def emit(outcome: P.ClipOutcome) -> None:
        if args.quiet:
            return
        if args.json:
            if outcome.receipt is not None:
                print(json.dumps(outcome.receipt.as_record(), sort_keys=True,
                                 separators=(",", ":")), flush=True)
            return
        name = os.path.basename(outcome.path)
        if outcome.receipt is not None:
            rec = outcome.receipt.as_record()
            digest = (rec["purged_sha256"] or "-")[:16]
            if rec["fail_closed_reason"]:
                print("%-12s %-40s FAIL-CLOSED %s  sha256 %s"
                      % (outcome.status, name, rec["fail_closed_reason"], digest), flush=True)
                return
            print("%-12s %-40s peak %.3f  speech %.2fs  sha256 %s"
                  % (outcome.status, name, rec["peak_speech_prob"], rec["speech_s"], digest),
                  flush=True)
        else:
            print("%-12s %-40s %s" % (outcome.status, name, outcome.detail), flush=True)

    report = P.scan_pool(target, engine=args.vad, threshold=args.threshold,
                         min_speech_ms=args.min_speech_ms, neg_threshold=args.neg_threshold,
                         min_silence_ms=args.min_silence_ms, speech_pad_ms=args.speech_pad_ms,
                         dry_run=args.dry_run, audit_log=args.audit_log, vad=vad,
                         receipt_clean=args.receipt_clean, on_outcome=emit)

    summary = report.as_record()
    if args.json:
        print(json.dumps({"summary": summary}, sort_keys=True, separators=(",", ":")))
    else:
        print("scanned %d  purged %d  would_purge %d  kept %d  already_absent %d  "
              "fail_closed %d  errors %d  vad %s%s"
              % (report.scanned, report.purged, report.would_purge, report.kept,
                 report.already_absent, report.fail_closed, report.errors, report.vad_engine,
                 "  audit %s" % report.audit_log if report.audit_log else "  (dry run)"))
        if report.halted:
            print("STOPPED: a purge failed and a clip judged speech-bearing is still on disk. "
                  "Nothing after it was scanned.", file=sys.stderr)
        if report.fail_closed:
            print("%d clip(s) were destroyed WITHOUT being scored (fail-closed). Read the "
                  "fail_closed_reason on those receipts before rearming this lane."
                  % report.fail_closed, file=sys.stderr)
        if report.dry_run and report.would_purge:
            print("dry run: %d clip(s) hold speech and are still on disk. Re-run without "
                  "--dry-run to destroy them." % report.would_purge)
    if report.errors or report.halted:
        return 2
    return 3 if report.fail_closed else 0


if __name__ == "__main__":
    raise SystemExit(main())
