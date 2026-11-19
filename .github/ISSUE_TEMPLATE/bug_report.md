---
name: Bug report
about: Something did not do what you expected — a crash, a wrong result, a changed file, silence.
title: "[BUG] "
labels: bug
---

<!-- Short is fine. Rough is fine. Guesses are welcome. Just enough that we can re-run it on our own machine —
     a report we can re-run gets fixed; a report we cannot re-run is a guess. -->

**1. What were you doing?**
<!-- The exact command you typed (copy it, do not paraphrase), or the clicks 1-2-3. The folder you ran it from
matters if you are unsure — say so and include it. -->

**2. What did you expect, and what happened instead?**
<!-- Copy the exact error text, or describe what you saw. If it produced a file, say what is wrong with the file.
If it changed or removed something on disk, be precise about which files — that is the one case where exactness
matters most. -->

**3. Does it happen every time?**
- [ ] every time
- [ ] sometimes — how often:
- [ ] only with certain files (what kind: )
- [ ] once so far

**4. Everything it printed**
<!-- Not just the last line. The first warning is often the useful part. Truncate only if enormous. -->

```
paste output here
```

**5. Can we have a file to test with?**
<!-- Behaviour usually depends on the file, so a sample is what turns this report into a fix. -->
- [ ] attaching a sample
- [ ] the file is personal — will send privately if asked
- [ ] not possible (why: )

**6. Did it work before?**
- [ ] first time using it
- [ ] it used to work; the last version that worked:
- [ ] an update or system change came in between

**7. How you installed it, and your machine**
<!-- How it got onto this machine: pip / pipx / AppImage / .deb / nix / Flatpak / source / something else.
Then run one command — it collects versions, device and filesystem state, and masks your user and home folder,
so it is safe to paste publicly:
     ./scripts/bugreport.sh --command "the exact command you ran"
Paste everything between the BEGIN and END lines below. If the helper does not exist where you installed from,
write the same facts by hand: operating system, version of this program, and anything unusual about the setup. -->

```
paste bugreport.sh output here
```

**8. Anything else that might matter**
<!-- Special file names (accents, spaces, very long paths), external or network drives, other programs that
were running at the time, antivirus prompts, what you were working on. -->

## For Transmute, please also include

Run the helper — it collects most of this automatically:

```
./scripts/bugreport.sh --command "the exact command you ran"
```

- **What Transmute says about your helper programs.** Transmute uses the programs already on your machine
  (FFmpeg, Ghostscript, qpdf, LibreOffice, pandoc, poppler) and hides or greys out anything its probe cannot
  prove works. On the main screen it lists which ones it found. Which show a check and which show a warning?
  A screenshot of that list is the fastest possible answer. If Transmute will not start at all, skip this —
  that *is* the bug.
- **The file's shape**, even if you cannot send the file: `ffprobe -hide_banner <file>` for audio/video,
  `pdfinfo <file>` for PDFs, `identify <file>` for images.
- Whether the **source and destination are on the same disk**, and whether either is a network share,
  external drive or phone (`df -T` shows it) — the verify-before-replace step behaves differently there.
- The **log file**, if one was written (`--logs path/to/log`).

Special file names (accents, spaces, very long paths) and recently updated FFmpeg builds both change
behaviour; mention them if either applies.
