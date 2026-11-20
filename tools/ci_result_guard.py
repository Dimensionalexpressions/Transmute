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
    failed_names = []
    for s in suites:
        tests += int(s.get("tests", 0))
        failures += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
        for tc in s.iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                failed_names.append(f"{tc.get('classname')}::{tc.get('name')}")

    print(f"guard: xml says tests={tests} failures={failures} "
          f"errors={errors} skipped={skipped}; rc={rc}")

    if failures or errors:
        print(f"::error::pytest produced {failures} failures / {errors} errors")
        # Public channel: runner logs need a token, annotations do not.
        for name in failed_names[:12]:
            print(f"::error::FAILED {name}")
        if len(failed_names) > 12:
            print(f"::error::... and {len(failed_names) - 12} more, see junit xml")
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
