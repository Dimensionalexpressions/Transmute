#!/usr/bin/env python3
"""Interpret pytest's junit-xml + exit code; tolerate ONLY interpreter crashes at teardown.

Why this exists (measured, not guessed): with QT_QPA_PLATFORM=offscreen,
PySide6 segfaults at interpreter shutdown on some hosts. Observed locally on
CachyOS and on ubuntu-24.04 GitHub runners: the suite prints "N passed" and
*then* the process dies with SIGSEGV (exit 139). The same teardown crash on
Windows surfaces as an access violation (0xC0000005 = 3221225477) or fastfail
(0xC0000409 = 3221226505).

The exit code is therefore an unreliable verdict, but the junit XML written by
pytest lands on disk BEFORE the crash. Trust the artifact, not the exit code:
pass only when the XML itself proves real tests ran with zero failures/errors,
and only then accept a known teardown-crash rc. Any other rc still fails.

Doctrine: measure the evidence the run actually produced.
"""
import sys
import xml.etree.ElementTree as ET

CRASH_RCS = {139, 3221225477, 3221226505}  # SIGSEGV, AV, fastfail


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if len(args) < 2 or not args[1].strip():
        # A silent traceback is the blind spot that hid every Windows failure.
        print(f"::error::guard needs <xml> <rc>; got {argv[1:]!r}")
        return 1
    xml_path, rc_s = args[0], args[1]
    rc = int(rc_s)

    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as e:
        # The runner log is token-only; annotations are public. Say WHY loudly.
        print(f"::error::junit xml unusable ({e!r}); pytest rc={rc} - if the "
              "file is missing pytest died mid-session, before writing it")
        return 1
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = failures = errors = skipped = 0
    failed = []  # (name, first-line assertion message)
    for s in suites:
        tests += int(s.get("tests", 0))
        failures += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
        for tc in s.iter("testcase"):
            node = tc.find("failure")
            if node is None:
                node = tc.find("error")
            if node is not None:
                name = f"{tc.get('classname')}::{tc.get('name')}"
                msg = (node.get("message") or (node.text or "")).strip()
                msg = msg.splitlines()[0][:60] if msg else "?"
                # ':' would break nothing, but ';' and ' | ' are our separators
                msg = msg.replace(";", ",").replace(" | ", " ")
                failed.append((name, msg))

    print(f"guard: xml says tests={tests} failures={failures} "
          f"errors={errors} skipped={skipped}; rc={rc}")

    if failures or errors:
        print(f"::error::pytest produced {failures} failures / {errors} errors")
        # Public channel: runner logs need a token, annotations do not. The
        # runner CAPS error annotations at 10 (measured run 44817220902: 12
        # FAILED lines emitted, only 8 arrived) - so pack EVERY name with its
        # assertion message into few dense chunks (~9 each), never more than
        # the cap once the summary/tail annotations are counted.
        chunk, lines = [], []
        for name, msg in failed:
            chunk.append(f"{name} :: {msg}")
            if len(chunk) >= 9:
                lines.append(chunk); chunk = []
        if chunk:
            lines.append(chunk)
        for i, part in enumerate(lines):
            print("::error::FAIL "
                  f"{i * 9}-{i * 9 + len(part) - 1}: " + " ;; ".join(part))
        return 1
    if tests == 0:
        print("::error::junit xml has zero tests - suite never ran")
        return 1
    if "--no-skips" in flags and skipped:
        print(f"::error::{skipped} test(s) skipped - coverage gate requires them to RUN")
        return 1
    if rc == 0:
        return 0
    if rc in CRASH_RCS:
        print(f"note: pytest died with {rc} AFTER writing a green junit xml - "
              "known Qt-offscreen interpreter-teardown crash; XML verdict stands")
        return 0
    print(f"::error::pytest exited {rc} with a green xml - suspicious, failing")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
