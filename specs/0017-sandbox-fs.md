# 0017 — sandbox (FS confinement for run_command)

## Goal
Extend the workspace FENCE to `run_command`'s writes. File tools (`write_file`/`edit_file`/`delete_file`)
are already confined to the cwd + `CODE_ADD_DIRS` (permissions.py `_within_roots`), but `run_command`
can shell out to `echo x > /etc/passwd`, `cp secret ../out`, `dd of=/dev/sda` — writing anywhere, even
under an allow rule or `bypass`. This phase parses a command's WRITE TARGETS (output redirects and the
destination of common write commands) and REFUSES any that resolve outside the workspace roots — an
ENFORCEMENT below the classify/approve layer, so an approved-or-bypassed command still can't write out
of the fence. It reuses execpolicy's parse (0016) and keys off the same roots as the file-tool fence.

v1 fences the SHELL-level writes (redirects `> >> 2> &>`; `cp`/`mv`/`install`/`tee`/`dd of=`; PowerShell
`Out-File`/`Set-Content`/`Add-Content`/`>` ). A program that writes outside the fence via its OWN logic
still needs an OS-level jail (a Windows restricted-token / AppContainer launcher) — that is the deeper
enforcement this phase's seam is built to grow into, and it is validated by a live ride, not a dep-free
unit test. Default OFF -> `run_command` is byte-identical to today.

## Acceptance
- `src/sandbox.py`: `write_targets(command, shell)` returns the paths a command writes to (redirect
  targets + write-command destinations, via execpolicy's segment parse); `escapes(command, cwd, roots,
  shell)` returns the write targets that resolve OUTSIDE `roots` ([] == confined). Pure, dep-free, never
  raises.
- `src/tools.py`: when `CODE_SANDBOX` is on, `run_command` computes `sandbox.escapes(...)` against
  `ctx.cwd + CODE_ADD_DIRS`; a non-empty result REFUSES the command with a teaching error naming the
  out-of-fence target(s). It runs normally otherwise.
- **Flag OFF (default) is byte-identical to today** — `run_command` never consults the sandbox.
- `scripts/check_sandbox.py` proves: a redirect / cp / tee / Out-File to an absolute, parent-escaping, or
  device path is caught; a write INSIDE the workspace (or a `--add-dir` root) is allowed; a read-only
  command is never flagged; and flag-off parity. Dep-free, no model, no network.

## Non-goals
- An OS-level process jail (restricted token / AppContainer / WFP). v1 fences SHELL-level writes; the OS
  launcher is a follow-up on this seam. Network egress is 0018.
- Classifying danger (that is execpolicy, 0016) — this only decides *where a write lands*.
- Confining READS — reads don't damage the workspace; the concern there is secrets (handled separately).

## Notes
- Off by default (`CODE_SANDBOX=false`), like every adoption-track phase.
- Roots = the same cwd + `CODE_ADD_DIRS` the file-tool fence uses, so run_command and file tools agree on
  "inside the workspace."
- Parsing is best-effort + conservative: an unresolvable/quoted target it can't judge is left to the
  normal gate, never silently allowed as "inside."
