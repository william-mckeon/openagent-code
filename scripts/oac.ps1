# =============================================================================
# oac - run openagent-code on the CURRENT directory, from anywhere.
# =============================================================================
# openagent-code self-locates its config (.env) and centralizes its data (trajectories,
# logs) into the OpenCode project dir, so it works from ANY repo. This wrapper just invokes
# the agent from this project's venv; the workspace defaults to wherever you run it.
#
# Install (one time): add this line to your PowerShell profile so `oac` is always available:
#     notepad $PROFILE
#     # then add (adjust the path if you moved the project):
#     . "C:\Users\willi\OneDrive\Desktop\OpenCode\scripts\oac.ps1"
#   reload:  . $PROFILE
#
# Use (in ANY repo):
#     cd C:\path\to\some\project
#     oac "fix the failing test in tests/"      # one-shot on the current repo
#     oac                                        # interactive REPL on the current repo
#     oac --mode acceptEdits "add type hints"    # flags pass straight through
#
# Where things go (centralized in the OpenCode project, NOT the repo you're editing):
#     config   -> OpenCode\.env          (your model + token, found automatically)
#     training -> OpenCode\trajectories\  (one growing corpus for the flywheel)
#     run log  -> OpenCode\logs\<session>.log   (grab it, hand it to Claude to review)
# =============================================================================

$OacRoot = Split-Path -Parent $PSScriptRoot
$OacExe = Join-Path $OacRoot ".venv\Scripts\openagent-code.exe"

function oac {
    if (-not (Test-Path $OacExe)) {
        Write-Error "openagent-code not found at $OacExe - run 'pip install -e .' in $OacRoot first."
        return
    }
    & $OacExe @args
}
