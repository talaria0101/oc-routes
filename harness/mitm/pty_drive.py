#!/usr/bin/env python3
"""oc-routes pty drive: run an interactive CLI flow under a pty with a hard
deadline, capture the device-flow URLs, then SIGKILL. Never hangs: the loop
is select() with an absolute deadline, and the child is kill -9ed on exit.

Why: `opencode console login` polls forever and ignores SIGTERM (a bare
`timeout 30` run survived 7+ minutes and had to be kill -9ed by hand).
This runner encodes that lesson: expect patterns, grab them, kill.

Usage:
  python3 harness/mitm/pty_drive.py -- <cmd...>            # e.g. console login
  python3 harness/mitm/pty_drive.py --deadline 45 --expect device -- <cmd...>

Env passthrough: LD_PRELOAD / OC_ROUTES_NETLOG / OC_ROUTES_FETCHLOG must be
set by the caller so the taps keep working under the pty.

Stdlib only (pty, select, signal). Writes transcript tail to stdout only;
the caller redirects.
"""
from __future__ import annotations

import argparse
import os
import pty
import select
import signal
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", type=float, default=45)
    ap.add_argument("--expect", action="append", default=[],
                    help="substring to wait for (any match ends the run early)")
    ap.add_argument("--kill-after", type=float, default=3.0,
                    help="extra seconds to keep polling after all expects seen")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="use -- to separate: pty_drive.py -- <cmd...>")
    args = ap.parse_args()
    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        print("usage: pty_drive.py [--deadline S] [--expect SUB]... -- <cmd...>",
              file=sys.stderr)
        return 2

    try:
        pid, fd = pty.fork()
    except OSError as e:
        print(f"pty unavailable in this sandbox ({e}); "
              "use drive.sh timeout -k path instead", file=sys.stderr)
        return 3
    if pid == 0:
        try:
            os.execvp(cmd[0], cmd)
        except Exception as e:
            print(f"exec failed: {e}", file=sys.stderr)
            os._exit(127)

    out = b""
    start = time.time()
    seen: set[str] = set()
    done_at = None
    try:
        while True:
            now = time.time()
            if now - start > args.deadline:
                print("[pty_drive: deadline hit, killing]", file=sys.stderr)
                break
            if done_at and now - done_at > args.kill_after:
                break
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                break
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if len(out) > 1 << 20:
                    out = out[-(1 << 20):]
                text = out.decode("utf-8", "replace")
                for pat in args.expect:
                    if pat in text:
                        seen.add(pat)
                if args.expect and len(seen) == len(args.expect):
                    if done_at is None:
                        done_at = now
            # child gone?
            try:
                gone_pid, _status = os.waitpid(pid, os.WNOHANG)
                if gone_pid == pid:
                    # drain once more then leave
                    try:
                        while True:
                            r2, _, _ = select.select([fd], [], [], 0.2)
                            if not r2:
                                break
                            chunk = os.read(fd, 65536)
                            if not chunk:
                                break
                            out += chunk
                    except OSError:
                        pass
                    pid = -1
                    break
            except ChildProcessError:
                pid = -1
                break
    finally:
        if pid != -1:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    sys.stdout.write(out.decode("utf-8", "replace")[-6000:])
    print(f"\n[pty_drive: {len(out)} bytes, expects seen: {sorted(seen)}]",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
