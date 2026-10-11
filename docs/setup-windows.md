# Windows laptop setup

How to get a Windows laptop ready for the SIH demo. The demo itself — what `make demo` /
`.\demo.ps1` does, the five-minute script, extra things to show and troubleshooting — is in
**[SIH demo](demo.md)**. Likely judge questions: [FAQ](faq.md).

All well data is **synthetic**; say so once at the start of the demo.

## 1. One-time setup (admin PowerShell, needs a reboot)

1. **Turn on virtualisation in BIOS/UEFI** (Intel VT-x / AMD-V, "Virtualization Technology").
   Check afterwards: Task Manager > Performance > CPU > "Virtualization: Enabled".
2. Install WSL2 and reboot:
   ```powershell
   wsl --install --no-distribution
   ```
3. Install Docker Desktop, start it once, accept its licence yourself, keep the WSL2 backend:
   ```powershell
   winget install -e --id Docker.DockerDesktop
   ```
4. Give WSL enough memory. Create `%UserProfile%\.wslconfig`:
   ```ini
   [wsl2]
   memory=10GB
   swap=8GB
   ```
   then `wsl --shutdown` and restart Docker Desktop.
5. Python 3.10+ on the PATH (`demo.ps1` uses it once to write `.env`):
   `winget install -e --id Python.Python.3.12`.
6. Start Docker Desktop and wait for "Engine running". If it says "unable to start" right after
   installing WSL, quit it (tray icon > Quit) and start it again: it does not retry on its own.

## 2. First run (internet needed, 45–90 min)

From the repository root in normal PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\demo.ps1
```

It builds and starts everything, pulls a small local LLM (about 1 GB), generates the synthetic
wells, trains the detectors once, replays SA-06 into its Barail loss and opens the Live Console.
After this first run nothing needs the internet: images, model and data are local.

If it stops with an error, the fix is usually in the table at the end of
[SIH demo → Troubleshooting](demo.md#troubleshooting) (Docker credential helper, a port already used by
a local PostgreSQL, a flaky network during the build). Rerun `.\demo.ps1` after fixing: it picks
up where it stopped.

Trust the stack's certificate once, then restart the browser:

```powershell
.\demo.ps1 trust
```

Windows asks you to confirm: answer Yes. Sign in as `planner` / `planner-dev-password`, or click
**Sign up** for your own (read-only) account. Full user guide: [User guide](guide.md).

## 3. Demo day

1. Start Docker Desktop and wait until it says "Engine running".
2. `.\demo.ps1` (two or three minutes when images are built).
3. Follow [the demo script](demo.md): "Ten minutes before the judges arrive", then the script.
   On this laptop use `.\demo.ps1 replay`, `.\demo.ps1 llm-down` and `.\demo.ps1 llm-up` where
   the script says `make demo-replay`, `make demo-llm-down` and `make demo-llm-up`.

## Optional: fill the review queue

The facts the demo loads are already accepted, so the Review console starts empty. To show it
with real extraction results, the evening before: open Dagster <http://localhost:3001> >
Automation > turn on the sensor **new_documents**. It queues one run per document;
`infra/dagster/dagster.yaml` runs one at a time so the laptop stays usable. Let it run overnight
(CPU OCR is slow), then turn it off. A few finished documents are enough.

## Optional: a better model

With an NVIDIA GPU, a larger model writes better explanations and answers. In `.env` set
`LLM_MODEL=qwen2.5:7b-instruct`, `ASSISTANT_DRAFTER=llm`, `LLM_MAX_TOKENS=2048`,
`LLM_TIMEOUT_S=300`, then run `.\demo.ps1` again (it pulls the model).

Answers and explanations are verified the same way whichever model writes them; with no model
at all, everything still works in rules mode.
