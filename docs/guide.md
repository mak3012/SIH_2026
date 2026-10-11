# User guide

How to start Well Intelligence on a laptop, sign in or create an account, and use it. For the
five-minute judge demo see [SIH demo](demo.md); for servers see [Operations](operations/index.md).

All well data in this repository is **synthetic** (30 generated Upper Assam wells).

## 1. What you need

| | |
|---|---|
| Computer | 16 GB RAM (10 GB for Docker), 40 GB free disk, 64-bit Windows 10/11, macOS or Linux |
| Software | Docker Desktop (Windows: with WSL2), Git, Python 3.10+; `make` on macOS/Linux |
| Network | Internet for the first run only (images, a 1 GB model); afterwards it runs offline |

Windows: turn on virtualisation in the BIOS, then in an admin PowerShell `wsl --install
--no-distribution`, reboot, `winget install -e --id Docker.DockerDesktop`, and start Docker
Desktop once. Details: [Windows laptop setup](setup-windows.md).

## 2. Start it

Start Docker Desktop and wait for **Engine running**. Then, from the repository folder:

=== "Windows (PowerShell)"

    ```powershell
    Set-ExecutionPolicy -Scope Process Bypass
    .\demo.ps1
    ```

=== "macOS / Linux"

    ```bash
    make demo
    ```

The first run takes 45–90 minutes; later runs two or three minutes. It writes `.env` with random
passwords, builds and starts the services over TLS, copies the stack's certificate authority to
`.certs/ca.crt`, loads the synthetic wells, trains the detectors and opens the Live Console.
Rerun the same command after any error: it picks up where it stopped.

## 3. Trust the certificate (once)

Everything is HTTPS, signed by the stack's own certificate authority. Trust it once, then
**close and reopen the browser**:

=== "Windows"

    ```powershell
    .\demo.ps1 trust
    ```

    Windows asks you to confirm adding the certificate: answer **Yes**.

=== "macOS"

    Open `.certs/ca.crt` in Keychain Access, then set it to *Always Trust*.

=== "Linux"

    Import `.certs/ca.crt` in the browser (Chrome: Settings > Privacy and security > Security >
    Manage certificates > Authorities; Firefox: Settings > Certificates > Authorities).

Skipping this step also works: the browser shows one warning for `https://localhost:5173`;
choose *Advanced > Continue*. Sign-in, sign-up and the API all live on that one address, so one
exception covers them.

## 4. Sign in or sign up

Open **<https://localhost:5173>**.

**Sign in** with a ready-made account (password = user name + `-dev-password`):

| User | Password | Can |
|---|---|---|
| `planner` | `planner-dev-password` | Everything in the demo: offsets, roadmaps, program lint, briefings, assistant |
| `engineer` | `engineer-dev-password` | Live Console: acknowledge and shelve alerts, voice notes |
| `superintendent` | `superintendent-dev-password` | Fleet board, shift digest, alerts on every rig |
| `steward` | `steward-dev-password` | Review queue: accept, correct, reject extracted values |
| `admin` | `admin-dev-password` | System status, detector bank, audit chain (no well data) |
| `partner` | `partner-dev-password` | A JV partner's planner: sees one licence block only |

**Sign up** creates your own account: click **Sign up** (top right), fill in user name, email,
name and a password of at least 12 characters, and you are signed in. A new account is a
**viewer**: it sees every screen and the synthetic demo wells, read-only. To let it act, an
administrator gives it a role (next section).

## 5. Give a new account a role

1. Open the Keycloak admin console <https://localhost:8444/auth/admin> (laptop only) and sign in
   as `admin` with `KEYCLOAK_ADMIN_PASSWORD` from `.env`.
2. Switch the realm (top left) to **wells**, open **Users**, pick the account.
3. **Role mapping > Assign role > Filter by realm roles**, choose a role (`planner`,
   `wellsite_engineer`, `superintendent`, `data_steward` or `admin`) and, for other licences,
   `block:<id>` / `jv:<id>` roles.
4. The user signs out and in again: the new role is in the next token.

What each role may do: [Security → Identity and roles](security.md#identity-and-roles).

## 6. Use it

| Menu | Screen | Use it to |
|---|---|---|
| **Map** | All wells (live), offset map | Real map (Streets / Satellite / Offline). The 30 Upper Assam wells and about 2,200 real Norwegian wells synced daily from Sodir, newest flagged NEW; pick an Assam well for its offsets (wells near it that drilled the same formations) |
| **Well** | Hazard roadmap, lookahead, correlation | The hazards to expect, interval by interval, each with the offset pages that show it; formation tops side by side |
| **Live** | Live Console | Real-time depth, gauges and alerts while drilling (or a replay); rig mode is dark with large buttons |
| **Plan** | Program linter | Upload a drilling program (CSV) and get the intervals that repeat an offset's problem |
| **Ask** | Ask WellMind | Questions in plain words; every answer cites the report page, which opens with the value boxed |
| **Voice note** | Voice note capture | Record a note on the rig; it is transcribed and attached to the well, offline too |
| **Review** | Review console | Values the extractor was unsure of, with the page beside them (keyboard J/K/A/R/C) |
| **Fleet** | Fleet board | All rigs on one board, the shift digest |
| **Replay** | Replay studio | Replay a held-out well and see how early the alerts came |
| **Admin** | System status | Services, detector bank, LLM, audit chain (role `admin`) |

**Live public wells.** The real wells come from the Sodir FactPages (open data). Dagster syncs
them every day at 05:00 UTC and downloads the completion reports of the newest wells for
extraction. To sync now: `.\demo.ps1 public-wells` (`make public-wells`). Without internet the
map shows the Assam wells on the offline basemap. Details: [Data → Public wells](data.md#public-wells-live-sodir-factpages).

**Well**, **Live** and **Plan** work on the selected well: pick one on the map or in the well
selector at the top first.

Step-by-step guides for each screen and role: [Screens and user guides](ui/screens.md). The
demo story in six steps: [SIH demo](demo.md).

Other addresses (laptop): API docs <https://localhost:5173/api/docs>, Grafana
<https://localhost:3000> (`admin` / `GRAFANA_ADMIN_PASSWORD`), Dagster <http://localhost:3001>.
`.\demo.ps1 urls` (or `make urls`) prints them.

## 7. Stop and restart

```powershell
.\demo.ps1 down      # make demo-down; data is kept
.\demo.ps1           # make demo; start again
```

`docker compose down -v` deletes all data; the next start regenerates it.

## 8. When sign-in does not work

| What you see | Fix |
|---|---|
| Clicking **Sign in** shows the app again instead of Keycloak's form | An old copy of the app (service worker) from before the fix: reload the page once (Ctrl+Shift+R), then sign in |
| You opened `https://127.0.0.1:5173` or the machine's name | The proxy sends you to `https://localhost:5173` (`PUBLIC_HOST`): sign-in works only on that one name |
| "This site can't be reached" / connection refused | Docker Desktop is not running (it stops after a reboot). Start it, wait for *Engine running*, run `.\demo.ps1` |
| "Your connection is not private" | Trust the certificate (step 3) and restart the browser, or choose *Advanced > Continue* |
| Keycloak says "Invalid username or password" | Passwords are `<user>-dev-password`. After 5 failures the account is locked for 15 minutes |
| Keycloak asks to "Update account information" or a user is missing | The realm in the database is older than `infra/keycloak/realm.json`: `.\demo.ps1 reset-auth` (or `make reset-auth`). This deletes accounts created by sign-up |
| Signed in, but "no application role" / 403 everywhere | The account has no role of this app: assign one (step 5), sign out and in |
| Signed in, no wells on the map | The account's licence blocks do not cover the wells (e.g. `admin`); use `planner` |
| No **Sign up** link on Keycloak's page | Old realm in the database: `.\demo.ps1 reset-auth` |
| `curl` on Windows: `CRYPT_E_NO_REVOCATION_CHECK` | Add `--ssl-no-revoke` (a private CA has no revocation list) |

More: [SIH demo → Troubleshooting](demo.md#troubleshooting).
