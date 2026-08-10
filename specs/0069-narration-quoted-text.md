# 0069 — narration detector: operators inside the quotes are prose

Status: implemented
Flag: none new (a correctness fix to the specs/0067 detector; rides `CODE_GUARD_NARRATION_STALL`)

## Goal

Close the hole that let the SECOND live narration loop through the 0067 guard untouched. On a "where did we
leave off?" turn, the model emitted ~30 consecutive `run_command('Write-Output "…"')` narration calls — and the
guard never fired. Cause: `_is_narration_command` rejected any command containing `| > & ` ; $(` ANYWHERE,
including inside the quoted message text. Recap narration is full of exactly that punctuation
(`"…use $LASTEXITCODE not $?, Stop-Process -Id not -Name."`, `"unread: specs/, tests/; internal src/"`), so
every few lines one was misclassified as a "real command", RESET the consecutive-streak counter, and the
guard never reached its threshold of `CODE_NARRATION_STALL_MAX` in a row.

## Concepts

- **Strip the quoted spans, then look for operators.** Punctuation INSIDE the quotes is prose being printed;
  only a shell operator OUTSIDE them (a pipe into a cmdlet, a redirect, a `;` chain, a backtick) makes it a
  real command. `_QUOTED_SPAN` removes `'…'` / `"…"` spans before `_NARRATION_META` runs.
- **`$(` stays fatal anywhere.** A PowerShell `$()` subexpression EXECUTES even inside double quotes
  (`Write-Output "$(Get-Content secret)"` runs the read), so it is checked on the FULL string before
  stripping — that can never be classified as narration.
- **Unbalanced quotes err conservative.** If quotes don't pair up (an escaped-quote edge case), leftover text
  keeps its operators and the command reads as real — the guard simply doesn't fire, which is the safe
  direction (0067's contract: never flag a real command).

## Acceptance

`scripts/check_narration_stall.py` extended (9/9):

- The three live-log narration shapes (text containing `;`, `|`, `>`, `$?`, `$LASTEXITCODE`) ARE narration.
- A real operator outside the quotes still disqualifies: `… "a | b" | Set-Content`, `… "done"; Remove-Item`,
  `… "x; y" > out.txt`, and the `$(…)` subexpression form.
- End-to-end: a planner looping on the punctuation-heavy live shape ends as `narration_stall` with the flag on.
- All prior 0067 assertions hold (plain narration trips; reads/pipes never; flag-off byte-identical).

## Non-goals

- No parsing of PowerShell escape semantics beyond the quote-span strip — conservative misses are acceptable;
  false fires are not.
- No change to thresholds, the nudge, or the outcome label.

## Byte-identity

Flag off, the guard block never runs — unchanged. Flag on, the only change is classification of
punctuation-in-quotes narration (previously misread as real commands). Verified: `check_narration_stall` 9/9,
full dep-free suite green.
