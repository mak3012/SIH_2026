# Windows stand-in for the Makefile's demo targets (no make / POSIX shell needed; Docker
# Desktop only). Run from the repo root, in PowerShell:
#   .\demo.ps1                 # = demo: everything `make demo` does (docs/demo.md)
#   .\demo.ps1 replay [WELL] [SPEED]   restart the held-out well's replay (default SA-06, 300x)
#   .\demo.ps1 llm-down        step 6: stop the LLM, replay at 600x (the alert still fires)
#   .\demo.ps1 llm-up | open | urls | down
#   .\demo.ps1 trust           trust the stack's certificate (once; Windows asks you to confirm)
#   .\demo.ps1 public-wells    sync the real Sodir wells now (also daily at 05:00 UTC)
#   .\demo.ps1 reset-auth      re-import the Keycloak realm (users, roles); deletes signed-up users
#   .\demo.ps1 up | seed | facts | models    the parts of `demo`, one at a time
param([string]$Step = "demo", [string]$Well = "SA-06", [string]$Speed = "300", [string]$Focus = "loss")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
# .env with generated secrets (kept on reruns); the dev overlay and profiles come from it.
python infra/env.py | Out-Null

# Settings (model, embeddings, timeouts) come from .env like every other compose command.
$Model = ((Get-Content .env | Where-Object { $_ -match '^LLM_MODEL=' }) -replace '^LLM_MODEL=', '')

function Run { & $args[0] $args[1..($args.Count - 1)]; if ($LASTEXITCODE) { throw "failed: $args" } }
function Tools { Run docker compose --profile tools run --rm apitools @args }

function Up {
    # Build (or rebuild after a code change). Offline with images already built, this fails
    # harmlessly and the stack starts from the built images.
    docker compose --profile tools build
    if ($LASTEXITCODE) { Write-Warning "build failed (offline?): starting from the images already built" }
    Run docker compose up -d --wait --wait-timeout 1800
    New-Item -ItemType Directory -Force .certs | Out-Null
    docker compose run --rm --no-deps -T --entrypoint cat init /certs/ca.crt | Set-Content -Encoding ascii .certs\ca.crt
    Run docker compose exec -T ollama ollama pull $Model
    Run docker compose exec -T ollama ollama run $Model "Reply OK" | Out-Null   # load it once
}
function Seed {
    Run docker compose exec -T api python -m api.seed
    Run docker compose --profile tools run --rm datatools python seed.py --offline
}
function PublicWells {
    # Sodir FactPages now (Dagster repeats it daily). Needs internet; the demo works without it.
    docker compose exec -T dagster dagster job execute -m pipelines -j public_wells_job
    if ($LASTEXITCODE) { Write-Warning "public wells not synced (offline?): the map shows the Assam wells only" }
}
function Facts  { Tools sh -c "cd data && python -m synthetic_assam.load_truth && cd .. && python -m api.assistant index" }
function Models {
    if (-not (Test-Path data\models\realtime\detectors.joblib)) {
        Tools python -m api.realtime train --wells 60 --hours 12 --test-wells 12
    }
}
function Replay {
    docker rm -f wells-demo-replay 2>$null | Out-Null
    Run docker compose --profile tools run -d --name wells-demo-replay apitools python -m api.replay well --well $Well --speed $Speed --explain --focus $Focus
    "replaying $Well at ${Speed}x: docker logs -f wells-demo-replay"
}
function Open {
    $e = @{}
    Get-Content .env | ForEach-Object { if ($_ -match '^([A-Z_]+)=(.*)$') { $e[$Matches[1]] = $Matches[2] } }
    $q = "SELECT b.id FROM wellbore b JOIN well w ON w.id = b.well_id WHERE w.name = '$Well' ORDER BY b.id LIMIT 1"
    $id = (docker compose exec -T postgres psql -U $e.POSTGRES_USER -d $e.POSTGRES_DB -tAc $q | Out-String).Trim()
    $url = "https://localhost:5173/#/live/$id"
    "Live Console: $url"
    Start-Process $url
}
function Trust {
    # The current user's root store: Windows shows its own confirmation dialog. Restart the browser.
    Import-Certificate -FilePath .certs\ca.crt -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    "Trusted .certs\ca.crt. Restart the browser, then open https://localhost:5173"
}
function ResetAuth {
    # Keycloak imports infra/keycloak/realm.json only into an empty database.
    $e = @{}; Get-Content .env | Where-Object { $_ -match '^\w+=' } | ForEach-Object { $k, $v = $_ -split '=', 2; $e[$k] = $v }
    Run docker compose stop keycloak
    Run docker compose exec -T postgres psql -U $e.POSTGRES_USER -d $e.POSTGRES_DB -c "DROP DATABASE IF EXISTS keycloak WITH (FORCE)" -c "CREATE DATABASE keycloak"
    Run docker compose up -d --wait keycloak proxy
}
function Urls {
    @"
Trust the certificate once: .\demo.ps1 trust (then restart the browser)
Web       https://localhost:5173   (Sign in: planner / planner-dev-password, or Sign up; docs/guide.md)
API       https://localhost:5173/api/docs
Grafana   https://localhost:3000   (admin / GRAFANA_ADMIN_PASSWORD in .env)
Keycloak  https://localhost:8444/auth/admin   (admin / KEYCLOAK_ADMIN_PASSWORD in .env)
Dagster   http://localhost:3001
S3        https://localhost:9000   (S3_ROOT_USER / S3_ROOT_PASSWORD in .env)
"@
}

switch ($Step) {
    "demo"     { Up; Seed; Facts; PublicWells; Models; Replay; Open; Urls }
    "up"       { Up }
    "seed"     { Seed }
    "facts"    { Facts }
    "models"   { Models }
    "replay"   { Replay }
    "open"     { Open }
    "llm-down" { Run docker compose stop ollama; $Speed = "600"; Replay }
    "llm-up"   { Run docker compose up -d --wait ollama }
    "urls"     { Urls }
    "public-wells" { PublicWells }
    "trust"    { Trust }
    "reset-auth" { ResetAuth }
    "down"     { docker rm -f wells-demo-replay 2>$null | Out-Null; Run docker compose down }
    default    { throw "unknown step $Step" }
}
