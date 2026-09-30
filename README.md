# Qualys Asset Inventory Downloader (`getassetinventory.py`)

**`getassetinventory` — Resilient Qualys CSAM / GAV v2 asset inventory downloader**

A single-file, production-ready Python tool that:

- Authenticates to the Qualys gateway (`/auth`) and **refreshes the bearer token automatically** every 2 hours.
- Runs a **pre-flight asset count** (`/rest/2.0/count/am/asset`) with your filter and date so you know what's coming before the download starts.
- Downloads **host details for all matching assets** (`/rest/2.0/search/am/asset`) at up to **300 assets per API call**, paginating with `lastSeenAssetId` / `hasMore`.
- Saves each API response as its own JSON file (`asset_inventory_00001.json`, `asset_inventory_00002.json`, …).
- Includes full logging, configurable retries with backoff, rate-limit reporting, system resource and file monitoring, and graceful shutdown.

**Version:** 1.0 (2026-09-29)
**Runs on:** Windows, macOS, Linux · **Python:** 3.9+

---

## Table of Contents

1. [Features](#features) — what the tool does
2. [Quickstart (Test in < 5 minutes)](#quickstart-test-in--5-minutes) — shortest path to a first run
3. [Operating Guide by Operating System](#operating-guide-by-operating-system) — setup, environment variables, running, and results
   - [Windows PowerShell](#windows-powershell)
   - [Windows Command Prompt (cmd)](#windows-command-prompt-cmd)
   - [macOS](#macos)
   - [Linux](#linux)
4. [Finding your gateway FQDN](#finding-your-gateway-fqdn) — which gateway to use for your platform
5. [Configuration Options](#configuration-options) — every flag and environment variable
   - [Required](#required)
   - [Query options](#query-options)
   - [Resilience options](#resilience-options)
   - [Output & logging options](#output--logging-options)
6. [Filters](#filters) — JSON filter format and quoting per shell
7. [Date window (`assetLastUpdated`)](#date-window-assetlastupdated) — default 7 days, or all assets
8. [Extra URL parameters](#extra-url-parameters) — `includeFields`, `softwareType`, and more
9. [Usage Examples](#usage-examples) — common command lines
10. [How It Works](#how-it-works) — auth, count, and paginated download
    - [Authentication](#authentication)
    - [Retries and error handling](#retries-and-error-handling)
    - [Zero assets and incomplete downloads](#zero-assets-and-incomplete-downloads)
11. [Output](#output) — folder layout and JSON files
12. [Logging & Monitoring](#logging--monitoring) — log format, progress, and monitors
13. [Exit Codes](#exit-codes) — for schedulers and pipelines
14. [Troubleshooting](#troubleshooting) — common errors and fixes
15. [Security Notes](#security-notes) — handling credentials and output
16. [Requirements](#requirements)
17. [References](#references)
18. [License](#license)

---

## Features

- **Single file, functions only** — easy to download, read, and maintain.
- **Automatic token management** — JWT requested with `token=true&permissions=true`, refreshed on a schedule (default 2 hours) and once more on any HTTP 401.
- **Count before download** — reports and logs the number of matching assets and the expected number of API calls.
- **Configurable pagination** — page size 1–300 (default 300).
- **JSON filters** — pass any CSAM filter via `--filter`, the `q_filter` environment variable, or a file (`--filter @filter.json`).
- **Extra URL parameters** — `includeFields`, `excludeFields`, `softwareType`, or any other search parameter via `--url-param key=value`.
- **Retry resilience** — up to 15 retries (configurable) with linear backoff 30s, 60s, 90s … capped at 300s; honors `Retry-After`.
- **Response validation** — every response is checked before it is used or written; malformed JSON or missing `hasMore` / `lastSeenAssetId` / `count` is retried, so incomplete pages never reach disk.
- **Completeness check** — a download that returns fewer assets than the count exits with code `1`, so schedulers see the problem.
- **Detailed logging** — every run option and its source (CLI, environment, or default), rate-limit headers, per-page progress, retries, and a final summary.
- **Built-in monitors** — CPU/RAM/SWAP/disk usage and JSON file progress logged every 30 seconds.
- **Test mode** — `--max-pages` stops after a set number of API calls.
- **Safe output** — files are written as `.part` and renamed on completion, so an interrupted run never leaves a truncated JSON file.
- **Graceful Ctrl+C / EOF handling** with clean monitor shutdown and meaningful exit codes.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Quickstart (Test in < 5 minutes)

The short version is below. For full per-platform instructions (PowerShell, cmd, macOS, Linux), see the [Operating Guide by Operating System](#operating-guide-by-operating-system).

### 1. Create & activate a virtual environment (required)

The script exits with setup instructions if it is not run inside a virtual environment.

```bash
# Linux / macOS
python3 -m venv qualys_env
source qualys_env/bin/activate

# Windows (Command Prompt)
python -m venv qualys_env
qualys_env\Scripts\activate

# Windows (PowerShell)
python -m venv qualys_env
.\qualys_env\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install requests psutil
```

### 3. Set required environment variables

**Linux / macOS / Git Bash:**

```bash
export q_gateway="gateway.qg1.apps.qualys.com"   # your platform's gateway
export q_username="your_qualys_username"
export q_password="your_qualys_password"
```

**Windows (Command Prompt):**

```bat
set q_gateway=gateway.qg1.apps.qualys.com
set q_username=your_qualys_username
set q_password=your_qualys_password
```

**Windows (PowerShell):**

```powershell
$env:q_gateway  = "gateway.qg1.apps.qualys.com"
$env:q_username = "your_qualys_username"
$env:q_password = "your_qualys_password"
```

> **Tip:** You can also use `--gateway`, `--username`, and `--password` flags (they override environment variables). If no password is supplied, you are prompted for it securely.

### 4. Run a small test

```bash
python3 getassetinventory.py --max-pages 1
```

### 5. Run the full download

```bash
python3 getassetinventory.py
```

**What happens next:**

1. A new folder is created: `qualys_asset_inventory_YYYYMMDDhhmmss/`
2. All run options are logged, then a bearer token is requested.
3. The asset count is reported (for example, `Assets matching criteria : 12,345` and `Expected API calls : 42`).
4. JSON files appear as `asset_inventory_00001.json`, `asset_inventory_00002.json`, …
5. A final summary shows assets retrieved, files written, retries, and elapsed time.

Watch progress live:

```bash
# Linux / macOS
tail -f qualys_asset_inventory_*/getassetinventory.log
```

```powershell
# Windows PowerShell
Get-Content (Get-ChildItem qualys_asset_inventory_*\getassetinventory.log | Select-Object -Last 1) -Wait
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Operating Guide by Operating System

Step-by-step instructions for each platform, from a clean machine to viewing results:

- [Windows PowerShell](#windows-powershell)
- [Windows Command Prompt (cmd)](#windows-command-prompt-cmd)
- [macOS](#macos)
- [Linux](#linux)

Each guide covers the same steps: build the virtual environment, install `requests` and `psutil`, test the help, view/set/unset environment variables (including `q_filter` and `q_asset_last_updated`), run the application, and review the results.

> **Note:** The script checks for a virtual environment and the required packages before anything else, including `-h`. Complete steps 1–3 before testing the help.

> **Reminder:** Command-line flags always override environment variables. Every value used, and where it came from (`cli`, `env:q_...`, `default`, or `prompt`), is recorded at the top of the log.

---

### Windows PowerShell

#### 1. Check Python (3.9 or later)

```powershell
python --version
# or, if "python" opens the Microsoft Store or isn't found, use the Python launcher:
py -3 --version
```

If Python is missing, install it from [python.org](https://www.python.org/downloads/windows/) and tick **"Add python.exe to PATH"** during setup. Wherever this guide says `python`, you can use `py -3` instead.

#### 2. Build and activate the virtual environment

```powershell
cd C:\path\to\getassetinventory          # folder containing getassetinventory.py
python -m venv qualys_env
.\qualys_env\Scripts\Activate.ps1
```

If activation fails with *"running scripts is disabled on this system"*, allow scripts for this window only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\qualys_env\Scripts\Activate.ps1
```

The prompt now starts with `(qualys_env)`.

#### 3. Install requests and psutil into the virtual environment

```powershell
python -m pip install --upgrade pip
python -m pip install requests psutil
python -m pip show requests psutil       # confirm both are installed
```

#### 4. Test the application help

```powershell
python getassetinventory.py -h
python getassetinventory.py --version
```

#### 5. View, set, and unset environment variables

**View** all tool variables currently set:

```powershell
Get-ChildItem Env:q_*
```

**Set** the required variables:

```powershell
$env:q_gateway  = "gateway.qg1.apps.qualys.com"
$env:q_username = "your_qualys_username"
$env:q_password = "your_qualys_password"   # or leave unset to be prompted securely
```

**Set** the filter (single quotes keep the JSON intact):

```powershell
$env:q_filter = '{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}'
```

**Set** the date window (default is the last 7 days; use an old date for all assets):

```powershell
$env:q_asset_last_updated = "1970-01-01T00:00:00Z"
```

**Set** other options the same way, for example:

```powershell
$env:q_page_size = "300"
$env:q_max_pages = "2"        # test limit; remove it for a full run
```

**Unset** a single variable, or all tool variables:

```powershell
Remove-Item Env:q_filter
Remove-Item Env:q_asset_last_updated
Get-ChildItem Env:q_* | Remove-Item      # clears every q_* variable
```

Variables set this way last only for the current PowerShell window. To keep non-secret settings for future windows, use `[Environment]::SetEnvironmentVariable("q_gateway", "gateway.qg1.apps.qualys.com", "User")`. Avoid storing `q_password` permanently.

#### 6. Run the application

```powershell
# Small test: one API call
python getassetinventory.py --max-pages 1

# Full run using the environment variables
python getassetinventory.py

# Override an environment variable for one run
python getassetinventory.py --assetLastUpdated 2026-09-01T00:00:00Z

# Filter from a file instead of an environment variable
python getassetinventory.py --filter "@filter.json"

# Exit code of the last run (0 = success)
$LASTEXITCODE
```

To watch progress from a second PowerShell window:

```powershell
$run = Get-ChildItem -Directory qualys_asset_inventory_* | Sort-Object Name | Select-Object -Last 1
Get-Content "$($run.FullName)\getassetinventory.log" -Wait -Tail 20
```

#### 7. View the final results

```powershell
# Latest run folder and its files
$run = Get-ChildItem -Directory qualys_asset_inventory_* | Sort-Object Name | Select-Object -Last 1
Get-ChildItem $run.FullName

# Final summary from the log
Select-String -Path "$($run.FullName)\getassetinventory.log" -Pattern "RUN |Expected assets|Assets retrieved|Output files|Retries performed|Total time"

# Count assets across all output files
python -c "import json,glob,sys; print(sum(len((json.load(open(f,encoding='utf-8')).get('assetListData') or {}).get('asset') or []) for f in glob.glob(sys.argv[1]+'/asset_inventory_*.json')))" $run.FullName

# Open the first page or the log
notepad "$($run.FullName)\asset_inventory_00001.json"
notepad "$($run.FullName)\getassetinventory.log"
```

#### 8. Finish

```powershell
deactivate
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

### Windows Command Prompt (cmd)

#### 1. Check Python (3.9 or later)

```bat
python --version
REM or use the Python launcher:
py -3 --version
```

If Python is missing, install it from [python.org](https://www.python.org/downloads/windows/) and tick **"Add python.exe to PATH"** during setup.

#### 2. Build and activate the virtual environment

```bat
cd C:\path\to\getassetinventory
python -m venv qualys_env
qualys_env\Scripts\activate.bat
```

The prompt now starts with `(qualys_env)`.

#### 3. Install requests and psutil into the virtual environment

```bat
python -m pip install --upgrade pip
python -m pip install requests psutil
python -m pip show requests psutil
```

#### 4. Test the application help

```bat
python getassetinventory.py -h
python getassetinventory.py --version
```

#### 5. View, set, and unset environment variables

**View** all tool variables currently set:

```bat
set q_
```

**Set** the required variables (no spaces around `=`):

```bat
set q_gateway=gateway.qg1.apps.qualys.com
set q_username=your_qualys_username
set q_password=your_qualys_password
```

Leave `q_password` unset to be prompted securely instead.

**Set** the filter. Wrap the whole assignment in double quotes so cmd keeps the JSON intact:

```bat
set "q_filter={"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}"
```

**Set** the date window (default is the last 7 days; use an old date for all assets):

```bat
set q_asset_last_updated=1970-01-01T00:00:00Z
```

**Set** other options the same way, for example:

```bat
set q_page_size=300
set q_max_pages=2
```

**Unset** a variable by assigning nothing:

```bat
set q_filter=
set q_asset_last_updated=
set q_max_pages=
```

Variables set this way last only for the current Command Prompt window. `setx q_gateway gateway.qg1.apps.qualys.com` saves a value for future windows (not the current one). Avoid saving `q_password` with `setx`.

#### 6. Run the application

```bat
REM Small test: one API call
python getassetinventory.py --max-pages 1

REM Full run using the environment variables
python getassetinventory.py

REM Override an environment variable for one run
python getassetinventory.py --assetLastUpdated 2026-09-01T00:00:00Z

REM Filter from a file
python getassetinventory.py --filter @filter.json

REM Exit code of the last run (0 = success)
echo %ERRORLEVEL%
```

cmd has no built-in `tail`; to watch progress from a second window, use PowerShell's `Get-Content -Wait` (see the PowerShell guide) or open the log in Notepad and reopen it to refresh.

#### 7. View the final results

```bat
REM List run folders (newest first), then set RUN to the one you want
dir /b /ad /o-n qualys_asset_inventory_*
set RUN=qualys_asset_inventory_20260929121000

REM Files in the run folder
dir %RUN%

REM Final summary from the log
findstr /C:"RUN " /C:"Expected assets" /C:"Assets retrieved" /C:"Output files" /C:"Retries performed" /C:"Total time" "%RUN%\getassetinventory.log"

REM Count assets across all output files
python -c "import json,glob,sys; print(sum(len((json.load(open(f,encoding='utf-8')).get('assetListData') or {}).get('asset') or []) for f in glob.glob(sys.argv[1]+'/asset_inventory_*.json')))" %RUN%

REM Open the first page or the log
notepad %RUN%\asset_inventory_00001.json
notepad %RUN%\getassetinventory.log
```

#### 8. Finish

```bat
deactivate
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

### macOS

#### 1. Check Python (3.9 or later)

```bash
python3 --version
```

If Python 3.9+ is missing, install it from [python.org](https://www.python.org/downloads/macos/) or with Homebrew (`brew install python`).

#### 2. Build and activate the virtual environment

```bash
cd ~/path/to/getassetinventory
python3 -m venv qualys_env
source qualys_env/bin/activate
```

The prompt now starts with `(qualys_env)`. Inside the environment, `python` and `python3` both point to the venv's Python.

#### 3. Install requests and psutil into the virtual environment

```bash
python -m pip install --upgrade pip
python -m pip install requests psutil
python -m pip show requests psutil
```

#### 4. Test the application help

```bash
python getassetinventory.py -h
python getassetinventory.py --version
```

#### 5. View, set, and unset environment variables

**View** all tool variables currently set:

```bash
env | grep '^q_'
```

**Set** the required variables:

```bash
export q_gateway="gateway.qg1.apps.qualys.com"
export q_username="your_qualys_username"
read -s -p "Qualys password: " q_password && echo && export q_password   # typed without echo
```

Or leave `q_password` unset and the script prompts for it.

**Set** the filter (single quotes keep the JSON intact):

```bash
export q_filter='{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}'
```

**Set** the date window (default is the last 7 days; use an old date for all assets):

```bash
export q_asset_last_updated="1970-01-01T00:00:00Z"
```

**Set** other options the same way, for example:

```bash
export q_page_size=300
export q_max_pages=2
```

**Unset** a single variable, or all tool variables:

```bash
unset q_filter
unset q_asset_last_updated
unset $(env | grep -o '^q_[^=]*')        # clears every q_* variable
```

Variables set this way last only for the current Terminal session. To keep non-secret settings, add the `export` lines to `~/.zshrc` (the default macOS shell) and run `source ~/.zshrc`. Avoid storing `q_password` there.

#### 6. Run the application

```bash
# Small test: one API call
python getassetinventory.py --max-pages 1

# Full run using the environment variables
python getassetinventory.py

# Override an environment variable for one run
python getassetinventory.py --assetLastUpdated 2026-09-01T00:00:00Z

# Filter from a file
python getassetinventory.py --filter @filter.json

# Exit code of the last run (0 = success)
echo $?
```

To watch progress from a second Terminal window:

```bash
tail -f "$(ls -d qualys_asset_inventory_* | tail -1)/getassetinventory.log"
```

#### 7. View the final results

```bash
# Latest run folder and its files
RUN=$(ls -d qualys_asset_inventory_* | tail -1)
ls -lh "$RUN"

# Final summary from the log
grep -E "RUN |Expected assets|Assets retrieved|Output files|Retries performed|Total time" "$RUN/getassetinventory.log"

# Count assets across all output files
python -c "import json,glob,sys; print(sum(len((json.load(open(f,encoding='utf-8')).get('assetListData') or {}).get('asset') or []) for f in glob.glob(sys.argv[1]+'/asset_inventory_*.json')))" "$RUN"

# Browse the first page and the log
less "$RUN/asset_inventory_00001.json"
less "$RUN/getassetinventory.log"
open "$RUN"                               # open the folder in Finder
```

#### 8. Finish

```bash
deactivate
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

### Linux

#### 1. Check Python (3.9 or later) and the venv module

```bash
python3 --version
```

Install Python and the virtual environment module if needed:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# RHEL / Rocky / AlmaLinux / Fedora
sudo dnf install -y python3 python3-pip
```

#### 2. Build and activate the virtual environment

```bash
cd ~/path/to/getassetinventory
python3 -m venv qualys_env
source qualys_env/bin/activate
```

The prompt now starts with `(qualys_env)`.

#### 3. Install requests and psutil into the virtual environment

```bash
python -m pip install --upgrade pip
python -m pip install requests psutil
python -m pip show requests psutil
```

#### 4. Test the application help

```bash
python getassetinventory.py -h
python getassetinventory.py --version
```

#### 5. View, set, and unset environment variables

**View** all tool variables currently set:

```bash
env | grep '^q_'
```

**Set** the required variables:

```bash
export q_gateway="gateway.qg1.apps.qualys.com"
export q_username="your_qualys_username"
read -s -p "Qualys password: " q_password && echo && export q_password   # typed without echo
```

Or leave `q_password` unset and the script prompts for it.

**Set** the filter (single quotes keep the JSON intact):

```bash
export q_filter='{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}'
```

**Set** the date window (default is the last 7 days; use an old date for all assets):

```bash
export q_asset_last_updated="1970-01-01T00:00:00Z"
```

**Set** other options the same way, for example:

```bash
export q_page_size=300
export q_max_pages=2
```

**Unset** a single variable, or all tool variables:

```bash
unset q_filter
unset q_asset_last_updated
unset $(env | grep -o '^q_[^=]*')        # clears every q_* variable
```

Variables set this way last only for the current shell session. To keep non-secret settings, add the `export` lines to `~/.bashrc` and run `source ~/.bashrc`. For scheduled runs (cron, systemd), supply credentials from a secret manager rather than a profile file.

#### 6. Run the application

```bash
# Small test: one API call
python getassetinventory.py --max-pages 1

# Full run using the environment variables
python getassetinventory.py

# Override an environment variable for one run
python getassetinventory.py --assetLastUpdated 2026-09-01T00:00:00Z

# Filter from a file
python getassetinventory.py --filter @filter.json

# Long run that survives logging out of an SSH session
nohup python getassetinventory.py > run.out 2>&1 &

# Exit code of the last foreground run (0 = success)
echo $?
```

To watch progress from a second terminal:

```bash
tail -f "$(ls -d qualys_asset_inventory_* | tail -1)/getassetinventory.log"
```

#### 7. View the final results

```bash
# Latest run folder and its files
RUN=$(ls -d qualys_asset_inventory_* | tail -1)
ls -lh "$RUN"

# Final summary from the log
grep -E "RUN |Expected assets|Assets retrieved|Output files|Retries performed|Total time" "$RUN/getassetinventory.log"

# Count assets across all output files
python -c "import json,glob,sys; print(sum(len((json.load(open(f,encoding='utf-8')).get('assetListData') or {}).get('asset') or []) for f in glob.glob(sys.argv[1]+'/asset_inventory_*.json')))" "$RUN"

# Browse the first page and the log
less "$RUN/asset_inventory_00001.json"
less "$RUN/getassetinventory.log"
```

#### 8. Finish

```bash
deactivate
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Finding your gateway FQDN

Use the gateway for the Qualys platform your subscription is on, for example `gateway.qg1.apps.qualys.com`, `gateway.qg2.apps.qualys.com`, or `gateway.qg1.apps.qualys.eu`. See the [Qualys Platform Identification](https://www.qualys.com/platform-identification/) page for the full list. Pass the FQDN only; `https://` is added automatically (a leading `https://` or trailing `/` is also accepted).

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Configuration Options

All settings can be provided via **environment variables** (recommended for automation) **or** command-line flags. **Command-line flags override environment variables.** Every value and its source is recorded in the log at startup; the password is always masked.

### Required

| Variable / Flag                  | Description                          | Example                        |
| -------------------------------- | ------------------------------------ | ------------------------------ |
| `q_gateway` / `-g`, `--gateway`  | Qualys gateway FQDN                  | `gateway.qg1.apps.qualys.com`  |
| `q_username` / `-u`, `--username`| Qualys username                      | `myuser`                       |
| `q_password` / `-p`, `--password`| Qualys password                      | (prompted if missing)          |

### Query options

| Variable / Flag                                             | Default          | Description |
| ----------------------------------------------------------- | ---------------- | ----------- |
| `q_filter` / `-f`, `--filter`                               | none (all assets)| JSON filter sent in the request body. Use `@file.json` to read from a file. |
| `q_asset_last_updated` / `--assetLastUpdated`               | now − 7 days     | Only assets updated on/after this UTC time. |
| `q_page_size` / `--page-size`                               | `300`            | Assets per API call (1–300). |
| `q_url_params` / `--url-param`                              | none             | Extra search URL parameters as `key=value`. Flag is repeatable; env var is `;`-separated. |
| `q_max_pages` / `--max-pages`                               | `0` (unlimited)  | Test limit: stop after this many search API calls. |

### Resilience options

| Variable / Flag                           | Default | Description |
| ----------------------------------------- | ------- | ----------- |
| `q_max_retries` / `--max-retries`         | `15`    | Retries per API call before the run stops. |
| `q_retry_delay` / `--retry-delay`         | `30`    | Backoff step in seconds (waits grow 30, 60, 90 …). |
| `q_retry_max_delay` / `--retry-max-delay` | `300`   | Maximum wait between retries in seconds. |
| `q_timeout` / `--timeout`                 | `300`   | HTTP request timeout in seconds. |
| `q_token_refresh_sec` / `--token-refresh-sec` | `7200` | Refresh the bearer token after this many seconds (60–14399; Qualys tokens expire after 4 hours). |

### Output & logging options

| Variable / Flag                             | Default | Description |
| ------------------------------------------- | ------- | ----------- |
| `q_log_level` / `--log-level`               | `INFO`  | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `DEBUG` adds full response headers and request parameters. |
| `q_monitor_interval` / `--monitor-interval` | `30`    | Seconds between system and file monitor log entries (minimum 5). |
| `q_compact` / `--compact`                   | `false` | Write compact JSON instead of pretty-printed JSON. |
| `q_insecure` / `--insecure`                 | `false` | Disable TLS certificate verification (not recommended). |

Boolean environment variables accept `true/false`, `yes/no`, `1/0`, or `on/off`.

Run `python3 getassetinventory.py -h` for the built-in help.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Filters

Filters use the CSAM JSON filter format and are sent to **both** the count and search APIs, so the count always matches what will be downloaded. See Qualys' [Supported Operators](https://docs.qualys.com/en/csam/api/appendix/supported_operators.htm) for available fields and operators.

```json
{
  "filters": [
    { "field": "operatingSystem.category1", "operator": "EQUALS", "value": "Windows" }
  ]
}
```

The filter is validated at startup; invalid JSON or a missing `field`/`operator` stops the run before any API call.

**Linux / macOS:**

```bash
python3 getassetinventory.py \
  --filter '{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}'
```

**Windows:** quoting JSON on the command line is error-prone (PowerShell 5.1 strips inner double quotes), so use the environment variable or a file:

```powershell
$env:q_filter = '{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}'
python getassetinventory.py

# or
python getassetinventory.py --filter @filter.json
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Date window (`assetLastUpdated`)

By default the tool downloads assets updated in the **last 7 days**. To download **all assets**, set an old date:

```bash
python3 getassetinventory.py --assetLastUpdated 1970-01-01T00:00:00Z
```

Accepted formats (UTC): `2026-09-01T00:00:00Z`, `2026-09-01T00:00Z`, `2026-09-01T00:00:00`, `2026-09-01`. Values are converted to the API format `yyyy-MM-ddTHH:mmZ`; both the input and converted values are logged.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Extra URL parameters

Any search API parameter can be added with `--url-param` (repeatable):

```bash
python3 getassetinventory.py \
  --url-param includeFields=operatingSystem,hardware,softwareListData \
  --url-param softwareType=Application
```

Environment variable form (`;`-separated, because values may contain commas):

```bash
export q_url_params="includeFields=operatingSystem,hardware;softwareType=Application"
```

`pageSize`, `lastSeenAssetId`, and `assetLastUpdated` are managed by the tool. If supplied through `--url-param`, they are ignored and a warning is logged; use `--page-size` and `--assetLastUpdated` instead.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Usage Examples

```bash
# Default run: last 7 days, 300 assets per call (uses env vars)
python3 getassetinventory.py

# All assets
python3 getassetinventory.py --assetLastUpdated 1970-01-01T00:00:00Z

# Test with 2 API calls, compact output, debug logging
python3 getassetinventory.py --max-pages 2 --compact --log-level DEBUG

# Windows servers only, selected fields
python3 getassetinventory.py \
  --filter '{"filters":[{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}]}' \
  --url-param includeFields=operatingSystem,hardware

# Fewer, faster-failing retries
python3 getassetinventory.py --max-retries 5 --retry-delay 10 --retry-max-delay 60

# Everything on the command line
python3 getassetinventory.py -g gateway.qg1.apps.qualys.com -u myuser --page-size 200
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## How It Works

```
 ┌──────────────┐    ┌──────────────────────────┐    ┌─────────────────────────────────┐
 │ POST /auth   │ →  │ POST /rest/2.0/count/    │ →  │ POST /rest/2.0/search/am/asset  │
 │ token=true   │    │      am/asset            │    │ pageSize=300, lastSeenAssetId=N │
 │ permissions= │    │ filter + assetLastUpdated│    │ loop until hasMore = 0          │
 │ true         │    │ → report & log count     │    │ → asset_inventory_NNNNN.json    │
 └──────────────┘    └──────────────────────────┘    └─────────────────────────────────┘
        ↑                                                          │
        └──── token refreshed every 2h, or on HTTP 401 ────────────┘
```

Pagination is cursor-based: each call returns the `lastSeenAssetId` to pass to the next call, so pages are downloaded **sequentially** by design.

### Authentication

The token is requested with:

```bash
curl -X POST https://gateway.qg1.apps.qualys.com/auth \
  -d "username=...&password=...&token=true&permissions=true" \
  -H "Content-Type: application/x-www-form-urlencoded"
```

Before every API call the tool checks the token's age and requests a new one once it is older than `--token-refresh-sec` (default 2 hours, well within Qualys' 4-hour token lifetime). The token value is never logged, only its length.

### Retries and error handling

| Condition | Behavior |
| --------- | -------- |
| Timeout, connection error, dropped connection | Retried with backoff |
| HTTP 408, 429, 5xx | Retried with backoff; `Retry-After` honored if longer than the backoff |
| HTTP 2xx with malformed JSON (truncated body, HTML proxy page, non-object) | Retried with backoff; first 300 characters of the body are logged |
| Search page missing `hasMore`, `hasMore` not 0/1, or `assetListData.asset` not a list | Retried with backoff |
| Search page with `hasMore=1` but no `lastSeenAssetId` or no assets | Retried with backoff |
| Count response missing a numeric `count` | Retried with backoff |
| `/auth` response with no token | Retried with backoff |
| HTTP 401 on count/search | New bearer token requested immediately, request resent once. A second 401 stops the run. |
| HTTP 401 on `/auth` | Stops immediately (check username/password) |
| HTTP 400, 403, 404, other 4xx | Stops immediately; response body is logged |
| TLS/SSL certificate error | Stops immediately (check proxy/CA settings) |
| HTTP 200 with `responseCode` ≠ `SUCCESS` | Stops immediately; Qualys message is logged (usually a bad filter or permissions, so retrying won't help) |
| `hasMore=1` but `lastSeenAssetId` did not advance | Stops immediately to avoid an infinite loop |

With the defaults (15 retries, 30s step, 300s cap), the wait schedule is:

```
30s, 60s, 90s, 120s, 150s, 180s, 210s, 240s, 270s, 300s, 300s, 300s, 300s, 300s, 300s
```

That is about 52 minutes of worst-case waiting per API call before the run stops. The exact schedule for your settings is printed in the log at startup. Invalid response bodies share the same retry budget and are counted separately in the final summary.

### Zero assets and incomplete downloads

- **Count is 0:** the count report is logged with a hint to widen `--assetLastUpdated` or check the filter, the search API is skipped, and the run ends cleanly with status `COMPLETED` and exit code `0`. The output folder contains only the log.
- **Fewer assets than counted:** if a full download (not limited by `--max-pages`) retrieves fewer assets than the count reported, the run ends with status `INCOMPLETE`, logs how many are missing, and exits with code `1`.
- **More assets than counted:** assets added or updated during a long download can push the total above the count. This logs a warning and still exits with code `0`.

If a page still fails after all retries, the run stops (the cursor makes skipping a page impossible), keeps every file already written, logs the last good page number and `lastSeenAssetId`, and exits with code `1`.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Output

```
qualys_asset_inventory_20260929121000/
├── asset_inventory_00001.json
├── asset_inventory_00002.json
├── ...
└── getassetinventory.log
```

- The folder name uses the run's **workflow ID** (`YYYYMMDDhhmmss`, local time at start).
- Each `asset_inventory_NNNNN.json` is the **complete API response** for one call, including `count`, `hasMore`, `lastSeenAssetId`, and `assetListData.asset[]`.
- Files are pretty-printed by default; use `--compact` for smaller files.

Example: count all assets across the output files with `jq`:

```bash
cat qualys_asset_inventory_*/asset_inventory_*.json | jq '.assetListData.asset | length' | paste -sd+ | bc
```

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Logging & Monitoring

Every log line uses the same format, written to both the console and `getassetinventory.log`:

```
2026-09-29 12:10:01,234 | INFO     | PID:4242 | workflow:20260929121000 | <message>
```

The log contains:

- **Startup block** — program version, Python/OS details, endpoints, every run option with its value and source (`cli`, `env:q_...`, `default`, or `prompt`), the effective search parameters, filter, retry schedule, and test limit.
- **Count report** — matching assets, filter, date, page size, expected API calls.
- **Per-call details** — HTTP status, response time, and rate-limit headers (`X-RateLimit-Limit`, `X-RateLimit-Window-Sec`, `X-RateLimit-Remaining`).
- **Per-page progress** — assets in the page, file name and size, `lastSeenAssetId`, `hasMore`, page N of expected, and percent complete.
- **Token events** — initial authentication, scheduled refreshes, and 401-triggered refreshes.
- **Retries** — the error, retry number, and wait time.
- **Final summary** — status (`COMPLETED`, `INCOMPLETE`, `FAILED`, or `INTERRUPTED`), expected vs. retrieved assets, files written, total size, retries (and how many were invalid response bodies), tokens requested, and total time.

Example progress line:

```
SEARCH API page 00007 | 300 assets -> asset_inventory_00007.json (4.12 MB) | lastSeenAssetId=41233567 | hasMore=1 | page 7/42 | progress 2,100/12,345 (17.0%) | 3.4s
```

Two background processes also write to the same log:

1. **System Resource Monitor** — CPU, RAM, SWAP, and disk usage every 30 seconds.
2. **File Monitor** — completed JSON files, total size, and any file still being written, every 30 seconds.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Exit Codes

| Code  | Meaning |
| ----- | ------- |
| `0`   | Run completed (including runs stopped by `--max-pages` or with 0 matching assets) |
| `1`   | Run failed (API error, retries exhausted, unexpected error) or incomplete (fewer assets downloaded than counted) |
| `2`   | Invalid configuration (all problems are listed before exit) |
| `130` | Interrupted by user (Ctrl+C or Ctrl+D) |

Useful for schedulers and CI/CD pipelines.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Troubleshooting

- **"NOT RUNNING IN A PYTHON VIRTUAL ENVIRONMENT"** → Follow the on-screen instructions to create and activate one.
- **"MISSING REQUIRED PACKAGES"** → `pip install requests psutil` inside the activated environment.
- **"CONFIGURATION ERRORS"** → Every invalid or missing option is listed; fix them and rerun.
- **HTTP 401 on `AUTH API`** → Check username, password, and that the account has API access.
- **HTTP 401 twice on a search page** → The account likely lacks CSAM/GAV API permissions; check the `AUTH API | Permissions returned` log line.
- **HTTP 400 on count or search** → Usually an invalid filter field or operator; the Qualys error message is in the log.
- **Count is 0** → Widen the date (`--assetLastUpdated 1970-01-01T00:00:00Z`) or check the filter.
- **`RUN INCOMPLETE`, fewer assets than counted** → Rerun; if it repeats, check the per-page lines for pages with fewer assets than `--page-size` before the last page.
- **More assets retrieved than counted** → Assets were added or updated during the download; a warning is logged and the run still succeeds.
- **`Invalid response body: malformed JSON`** → Usually a proxy or load balancer returning an HTML page, or a truncated transfer; the body snippet in the log shows which.
- **TLS/SSL errors behind a proxy** → Configure your corporate CA (e.g. `REQUESTS_CA_BUNDLE=/path/to/ca.pem`). `--insecure` works but is not recommended.
- **Frequent 429 responses** → The tool backs off automatically; check the `X-RateLimit-*` values in the log against your subscription's limits.
- **Garbled characters in the Windows console** → The log file is always UTF-8; open it in an editor that supports UTF-8.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Security Notes

- Never commit credentials to scripts or version control. Use environment variables or a secret manager in CI/CD.
- The password and bearer token are held in memory only and never written to the log or output files.
- Passing `--password` on the command line can expose it in shell history and process listings; prefer `q_password` or the secure prompt.
- Output files contain sensitive inventory data (hostnames, IPs, software, users). Store and share them accordingly.
- Keep TLS verification enabled in production.

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## Requirements

- Python 3.9 or later
- [`requests`](https://pypi.org/project/requests/)
- [`psutil`](https://pypi.org/project/psutil/)
- A Qualys subscription with CSAM / Global AssetView API access

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## References

- [Get Host Details of All Assets](https://docs.qualys.com/en/csam/api/asset_host_data/get_host_details_of_all_assets.htm)
- [Count of Assets](https://docs.qualys.com/en/csam/api/asset_host_data/count_of_assets.htm)
- [Supported Operators](https://docs.qualys.com/en/csam/api/appendix/supported_operators.htm)
- Logging and monitoring design modeled on [dg-cafe/gethld](https://github.com/dg-cafe/gethld)

<sub>[↑ Back to top](#table-of-contents)</sub>

---

## License

This script is provided as-is under the Apache-2.0 License. Feel free to modify and use it in your environment.

<sub>[↑ Back to top](#table-of-contents)</sub>
