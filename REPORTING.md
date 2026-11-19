# How to report a problem

If something broke, or does something you did not want, please open an issue here:

- **something is broken** — it crashed, produced the wrong result, or changed your files:
  open a *Bug report*.
- **you want it to behave differently** — a default, a message, a missing option:
  open a *Feature or change request*.

The forms ask short questions on purpose; every answer is something a maintainer can
**re-run**. A report that can be re-run gets fixed; a report that cannot is a guess.

Before you write, run this one command and paste what it prints into the form:

```
./scripts/bugreport.sh --command "the exact command you ran"
```

It gathers versions, device and filesystem state, and the tail of any log you point it
at. Names and your home folder are masked by default, so the output is safe to paste into
a public issue. It does not read the contents of your files.

If the problem involves a specific file, please offer it as a sample — behaviour usually
depends on the file. If the file is personal, say so and it can be sent privately.
