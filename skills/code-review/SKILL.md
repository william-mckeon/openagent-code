---
name: code-review
description: Review the current git diff by concern — one subagent per concern, then synthesize.
subskills: code-review-*
---

Merge every finding from every concern into ONE numbered report. Each finding must name a
specific file and line. Return them all — do not summarize away or drop findings. Order them
most-serious first. This is READ-ONLY: report findings, do not edit, create, or run anything.
If a concern found nothing, note that briefly rather than omitting it.
