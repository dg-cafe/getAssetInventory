#!/usr/bin/env python3
"""
getassetinventory.py - Qualys CSAM / GAV v2 Asset Inventory downloader
=======================================================================

Downloads host details for all assets from the Qualys CyberSecurity Asset
Management (CSAM) / Global AssetView (GAV) v2 API and writes one JSON file per
API call.

Workflow
--------
  1. Authenticate:  POST /auth  (token=true, permissions=true) -> JWT bearer token.
                    The token is refreshed automatically every 2 hours (configurable).
  2. Pre-flight:    POST /rest/2.0/count/am/asset  with the same filter and
                    assetLastUpdated -> reports how many assets will be downloaded.
  3. Download:      POST /rest/2.0/search/am/asset  paginated with lastSeenAssetId /
                    hasMore at up to 300 assets per call.
  4. Output:        qualys_asset_inventory_<YYYYMMDDhhmmss>/
                        asset_inventory_00001.json
                        asset_inventory_00002.json
                        ...
                        getassetinventory.log

Requirements
------------
  Python 3.9+ and two packages:  pip install requests psutil
  Runs on Windows, macOS and Linux. Must be run inside a virtual environment.

Quick start
-----------
  Linux / macOS:
      python3 -m venv qualys_env
      source qualys_env/bin/activate
      pip install requests psutil
      export q_username="your_user"
      export q_password="your_password"
      python3 getassetinventory.py --gateway gateway.qg1.apps.qualys.com

  Windows (PowerShell):
      python -m venv qualys_env
      .\\qualys_env\\Scripts\\Activate.ps1
      pip install requests psutil
      $env:q_username = "your_user"
      $env:q_password = "your_password"
      python getassetinventory.py --gateway gateway.qg1.apps.qualys.com

  Every option can be set with a command-line flag or a q_* environment variable.
  Command-line flags override environment variables. Run with -h for the full list.

Exit codes
----------
  0 = success, 1 = run failed or fewer assets downloaded than counted,
  2 = invalid configuration, 130 = interrupted.

License: Apache-2.0. Logging and monitoring design modeled on dg-cafe/gethld.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import math
import multiprocessing as mp
import os
import platform
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# =============================================================================
# PROGRAM METADATA
# =============================================================================
PROGRAM_NAME = "getassetinventory"
VERSION = "1.0"
VERSION_DATE = "2026-09-29"
LOGGER_NAME = PROGRAM_NAME

# =============================================================================
# QUALYS API CONSTANTS
# =============================================================================
AUTH_PATH = "/auth"
COUNT_PATH = "/rest/2.0/count/am/asset"
SEARCH_PATH = "/rest/2.0/search/am/asset"
MAX_PAGE_SIZE = 300  # hard limit documented by Qualys for pageSize

# URL parameters managed by this program; user-supplied values are ignored.
PROTECTED_URL_PARAMS = {"pageSize", "lastSeenAssetId", "assetLastUpdated"}

# Rate-limit headers returned by the CSAM/GAV gateway APIs, logged after every call.
RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Window-Sec",
    "X-RateLimit-Remaining",
)

# =============================================================================
# OUTPUT CONSTANTS
# =============================================================================
OUTPUT_DIR_PREFIX = "qualys_asset_inventory"
OUTPUT_FILE_PREFIX = "asset_inventory"
LOG_FILE_NAME = f"{PROGRAM_NAME}.log"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
REQUIRED_PACKAGES = ("requests", "psutil")


# =============================================================================
# ENVIRONMENT CHECKS (run before third-party imports)
# =============================================================================
def check_virtual_environment() -> bool:
    """Return True when running in a venv/conda env; otherwise print setup help."""
    if sys.prefix != sys.base_prefix or os.environ.get("CONDA_PREFIX"):
        return True

    if platform.system() == "Windows":
        create_cmd = "python -m venv qualys_env"
        activate_cmd = (
            "qualys_env\\Scripts\\activate          (Command Prompt)\n"
            "      .\\qualys_env\\Scripts\\Activate.ps1   (PowerShell)"
        )
    else:
        create_cmd = "python3 -m venv qualys_env"
        activate_cmd = "source qualys_env/bin/activate"

    print("\n" + "=" * 80)
    print("WARNING: NOT RUNNING IN A PYTHON VIRTUAL ENVIRONMENT")
    print("=" * 80)
    print("Run this script inside a dedicated virtual environment.\n")
    print("   1. Create a virtual environment:")
    print(f"      {create_cmd}")
    print("   2. Activate it:")
    print(f"      {activate_cmd}")
    print("   3. Install required packages:")
    print(f"      pip install {' '.join(REQUIRED_PACKAGES)}")
    print("   4. Run this script again from inside the activated environment.")
    print("=" * 80 + "\n")
    return False


def print_missing_packages_message(error: ImportError) -> None:
    """Explain how to install the third-party packages this script needs."""
    print("\n" + "=" * 80)
    print("MISSING REQUIRED PACKAGES")
    print("=" * 80)
    print(f"Error: {error}")
    print("\nThis script requires: " + ", ".join(REQUIRED_PACKAGES))
    print("   1. Activate your Python virtual environment")
    print(f"   2. pip install {' '.join(REQUIRED_PACKAGES)}")
    print("   3. Run the script again.")
    print("=" * 80 + "\n")


# Only the main process checks the venv; spawned monitor processes skip it.
if __name__ == "__main__" and not check_virtual_environment():
    sys.exit(1)

try:
    import psutil
    import requests
    import urllib3
except ImportError as import_error:
    print_missing_packages_message(import_error)
    sys.exit(1)


# =============================================================================
# OPTION DEFINITIONS
# Single source of truth for CLI flags, environment variables and defaults.
## type: "str" | "int" | "bool" | "list"   (list env values are ';'-separated)
# =============================================================================
OPTION_SPECS: List[Dict[str, Any]] = [
    {"key": "gateway", "flags": ["-g", "--gateway"], "env": "q_gateway", "type": "str",
     "default": None,
     "help": "Qualys gateway FQDN, e.g. gateway.qg1.apps.qualys.com (required)"},
    {"key": "username", "flags": ["-u", "--username"], "env": "q_username", "type": "str",
     "default": None, "help": "Qualys username (required)"},
    {"key": "password", "flags": ["-p", "--password"], "env": "q_password", "type": "str",
     "default": None, "secret": True,
     "help": "Qualys password (prompted securely if not supplied)"},
    {"key": "filter", "flags": ["-f", "--filter"], "env": "q_filter", "type": "str",
     "default": None,
     "help": "JSON filter for the request body, e.g. "
             "'{\"filters\":[{\"field\":\"operatingSystem.category1\","
             "\"operator\":\"EQUALS\",\"value\":\"Windows\"}]}'. "
             "Use @path/to/filter.json to read it from a file."},
    {"key": "asset_last_updated", "flags": ["--assetLastUpdated", "--asset-last-updated"],
     "env": "q_asset_last_updated", "type": "str", "default": None,
     "help": "Only assets updated on/after this UTC time (default: now - 7 days). "
             "For ALL assets use an old date such as 1970-01-01T00:00:00Z"},
    {"key": "page_size", "flags": ["--page-size"], "env": "q_page_size", "type": "int",
     "default": MAX_PAGE_SIZE,
     "help": f"Assets per API call, 1-{MAX_PAGE_SIZE} (default: {MAX_PAGE_SIZE})"},
    {"key": "url_params", "flags": ["--url-param"], "env": "q_url_params", "type": "list",
     "default": [],
     "help": "Extra search URL parameter as key=value (repeatable), e.g. "
             "includeFields=operatingSystem,hardware. Env var: ';'-separated list"},
    {"key": "max_pages", "flags": ["--max-pages"], "env": "q_max_pages", "type": "int",
     "default": 0, "help": "Test limit: stop after this many API calls (0 = unlimited)"},
    {"key": "max_retries", "flags": ["--max-retries"], "env": "q_max_retries", "type": "int",
     "default": 15, "help": "Retries per API call before giving up (default: 15)"},
    {"key": "retry_delay", "flags": ["--retry-delay"], "env": "q_retry_delay", "type": "int",
     "default": 30,
     "help": "Backoff step in seconds; waits grow 30, 60, 90 ... (default: 30)"},
    {"key": "retry_max_delay", "flags": ["--retry-max-delay"], "env": "q_retry_max_delay",
     "type": "int", "default": 300,
     "help": "Maximum wait between retries in seconds (default: 300)"},
    {"key": "timeout", "flags": ["--timeout"], "env": "q_timeout", "type": "int",
     "default": 300, "help": "HTTP request timeout in seconds (default: 300)"},
    {"key": "token_refresh_sec", "flags": ["--token-refresh-sec"], "env": "q_token_refresh_sec",
     "type": "int", "default": 7200,
     "help": "Refresh the bearer token after this many seconds (default: 7200 = 2 hours)"},
    {"key": "monitor_interval", "flags": ["--monitor-interval"], "env": "q_monitor_interval",
     "type": "int", "default": 30,
     "help": "Seconds between system/file monitor log entries (default: 30)"},
    {"key": "log_level", "flags": ["--log-level"], "env": "q_log_level", "type": "str",
     "default": "INFO", "choices": LOG_LEVELS,
     "help": "Log level; DEBUG adds full response headers and request params (default: INFO)"},
    {"key": "compact", "flags": ["--compact"], "env": "q_compact", "type": "bool",
     "default": False, "help": "Write compact JSON instead of pretty-printed JSON"},
    {"key": "insecure", "flags": ["--insecure"], "env": "q_insecure", "type": "bool",
     "default": False, "help": "Disable TLS certificate verification (not recommended)"},
]

HELP_EPILOG = f"""
Environment variables (CLI flags override them):
{chr(10).join(f"  {spec['env']:<24} {spec['flags'][-1]}" for spec in OPTION_SPECS)}

Examples:
  # Last 7 days of asset updates (default window)
  python {PROGRAM_NAME}.py -g gateway.qg1.apps.qualys.com -u myuser

  # All assets regardless of update date
  python {PROGRAM_NAME}.py -g gateway.qg1.apps.qualys.com --assetLastUpdated 1970-01-01T00:00:00Z

  # Filter (Linux/macOS quoting)
  python3 {PROGRAM_NAME}.py -g gateway.qg1.apps.qualys.com \\
      --filter '{{"filters":[{{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}}]}}'

  # Filter on Windows: JSON quoting on the command line is error-prone, so use
  # the environment variable or a file instead
  $env:q_filter = '{{"filters":[{{"field":"operatingSystem.category1","operator":"EQUALS","value":"Windows"}}]}}'
  python {PROGRAM_NAME}.py -g gateway.qg1.apps.qualys.com --filter @filter.json

  # Extra URL parameters and a 2-call test run
  python {PROGRAM_NAME}.py -g gateway.qg1.apps.qualys.com \\
      --url-param includeFields=operatingSystem,hardware --url-param softwareType=Application \\
      --max-pages 2

Output: {OUTPUT_DIR_PREFIX}_YYYYMMDDhhmmss/{OUTPUT_FILE_PREFIX}_00001.json ... + {LOG_FILE_NAME}
Exit codes: 0 success, 1 run failed or incomplete, 2 invalid configuration, 130 interrupted.
"""


# =============================================================================
# CONFIGURATION
# =============================================================================
def build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI parser from OPTION_SPECS. Unset flags stay None so env/defaults apply."""
    parser = argparse.ArgumentParser(
        prog=f"{PROGRAM_NAME}.py",
        description=f"{PROGRAM_NAME} v{VERSION} ({VERSION_DATE}) - "
                    "Qualys CSAM/GAV v2 asset inventory downloader",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for spec in OPTION_SPECS:
        kwargs: Dict[str, Any] = {"dest": spec["key"], "default": None, "help": spec["help"]}
        if spec["type"] == "bool":
            kwargs["action"] = "store_true"
        elif spec["type"] == "list":
            kwargs["action"] = "append"
        elif spec["type"] == "int":
            kwargs["type"] = int
        if "choices" in spec:
            kwargs["choices"] = spec["choices"]
        parser.add_argument(*spec["flags"], **kwargs)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION} ({VERSION_DATE})")
    return parser


def parse_bool(text: str) -> bool:
    """Convert common true/false strings (env vars) to bool."""
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"'{text}' is not a boolean (use true/false)")


def convert_env_value(spec: Dict[str, Any], raw: str) -> Any:
    """Convert an environment variable string to the option's type."""
    if spec["type"] == "int":
        return int(raw)
    if spec["type"] == "bool":
        return parse_bool(raw)
    if spec["type"] == "list":
        return [item.strip() for item in raw.split(";") if item.strip()]
    if "choices" in spec and raw.upper() not in spec["choices"]:
        raise ValueError(f"'{raw}' must be one of {', '.join(spec['choices'])}")
    return raw.upper() if "choices" in spec else raw


def resolve_option(spec: Dict[str, Any], cli_value: Any) -> Tuple[Any, str]:
    """Return (value, source) using precedence CLI > environment variable > default."""
    if cli_value is not None:
        return cli_value, "cli"
    raw = os.environ.get(spec["env"])
    if raw is not None and raw.strip() != "":
        return convert_env_value(spec, raw), f"env:{spec['env']}"
    return spec["default"], "default"


def normalize_gateway(value: str) -> str:
    """Accept 'gateway.qg1.apps.qualys.com' or 'https://gateway...' and return the bare FQDN."""
    fqdn = value.strip()
    for scheme in ("https://", "http://"):
        if fqdn.lower().startswith(scheme):
            fqdn = fqdn[len(scheme):]
    fqdn = fqdn.rstrip("/")
    if not fqdn or "/" in fqdn or " " in fqdn:
        raise ValueError(f"'{value}' is not a valid gateway FQDN (e.g. gateway.qg1.apps.qualys.com)")
    return fqdn


def default_asset_last_updated() -> str:
    """Now minus 7 days in the API's yyyy-MM-ddTHH:mmZ UTC format."""
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%MZ")


def normalize_asset_last_updated(value: str) -> str:
    """Accept common UTC date/time formats and return yyyy-MM-ddTHH:mmZ as the API expects."""
    text = value.strip().replace("+00:00", "Z")
    accepted_formats = (
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
    )
    for fmt in accepted_formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%MZ")
        except ValueError:
            continue
    raise ValueError(
        f"assetLastUpdated '{value}' is not a valid UTC date/time "
        "(examples: 2026-09-01T00:00Z, 1970-01-01T00:00:00Z, 2026-09-01)"
    )


def parse_filter(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse and validate the JSON filter. Accepts inline JSON or @path/to/file.json."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("@"):
        try:
            text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read filter file '{text[1:]}': {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"filter is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from exc

    example = '{"filters": [{"field": "...", "operator": "...", "value": "..."}]}'
    if not isinstance(data, dict) or not isinstance(data.get("filters"), list):
        raise ValueError(f"filter must be a JSON object like {example}")
    for index, criterion in enumerate(data["filters"], start=1):
        if not isinstance(criterion, dict) or "field" not in criterion or "operator" not in criterion:
            raise ValueError(f"filter criterion #{index} needs 'field' and 'operator': {example}")
    return data


def parse_url_params(entries: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """Turn ['key=value', ...] into a dict. Returns (params, ignored_protected_keys)."""
    params: Dict[str, str] = {}
    ignored: List[str] = []
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"url parameter '{entry}' must be in key=value form")
        key, value = (part.strip() for part in entry.split("=", 1))
        if not key:
            raise ValueError(f"url parameter '{entry}' has an empty key")
        if key in PROTECTED_URL_PARAMS:
            ignored.append(key)
            continue
        params[key] = value
    return params, ignored


def validate_numbers(cfg: Dict[str, Any]) -> List[str]:
    """Range-check numeric options. Returns a list of problems."""
    problems = []
    if not 1 <= cfg["page_size"] <= MAX_PAGE_SIZE:
        problems.append(f"--page-size must be between 1 and {MAX_PAGE_SIZE} (got {cfg['page_size']})")
    if cfg["max_pages"] < 0:
        problems.append("--max-pages must be 0 (unlimited) or greater")
    if cfg["max_retries"] < 0:
        problems.append("--max-retries must be 0 or greater")
    if cfg["retry_delay"] < 1:
        problems.append("--retry-delay must be at least 1 second")
    if cfg["retry_max_delay"] < cfg["retry_delay"]:
        problems.append("--retry-max-delay must be >= --retry-delay")
    if cfg["timeout"] < 1:
        problems.append("--timeout must be at least 1 second")
    if not 60 <= cfg["token_refresh_sec"] < 14400:
        problems.append("--token-refresh-sec must be between 60 and 14399 (Qualys tokens expire after 4 hours)")
    if cfg["monitor_interval"] < 5:
        problems.append("--monitor-interval must be at least 5 seconds")
    return problems


def build_config(args: argparse.Namespace) -> Tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """Resolve every option, then validate and derive settings.

    Returns (cfg, sources, problems). `sources` records where each value came from.
    """
    cfg: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    problems: List[str] = []

    for spec in OPTION_SPECS:
        try:
            cfg[spec["key"]], sources[spec["key"]] = resolve_option(spec, getattr(args, spec["key"]))
        except ValueError as exc:
            problems.append(f"{spec['env']}: {exc}")
            cfg[spec["key"]], sources[spec["key"]] = spec["default"], "default"

    if not cfg["gateway"]:
        problems.append("gateway FQDN is required (--gateway or q_gateway)")
    else:
        try:
            cfg["gateway"] = normalize_gateway(cfg["gateway"])
        except ValueError as exc:
            problems.append(str(exc))

    if not cfg["username"]:
        problems.append("username is required (--username or q_username)")

    if cfg["asset_last_updated"] is None:
        cfg["asset_last_updated"] = default_asset_last_updated()
        sources["asset_last_updated"] = "default (now - 7 days)"
    cfg["asset_last_updated_input"] = cfg["asset_last_updated"]
    try:
        cfg["asset_last_updated"] = normalize_asset_last_updated(cfg["asset_last_updated"])
    except ValueError as exc:
        problems.append(str(exc))

    cfg["filter_input"] = cfg["filter"]
    try:
        cfg["filter"] = parse_filter(cfg["filter"])
    except ValueError as exc:
        problems.append(str(exc))

    cfg["url_params_input"] = list(cfg["url_params"] or [])
    try:
        cfg["url_params"], cfg["ignored_url_params"] = parse_url_params(cfg["url_params_input"])
    except ValueError as exc:
        problems.append(str(exc))
        cfg["url_params"], cfg["ignored_url_params"] = {}, []

    problems.extend(validate_numbers(cfg))
    cfg["tls_verify"] = not cfg["insecure"]
    return cfg, sources, problems


def ensure_password(cfg: Dict[str, Any], sources: Dict[str, str]) -> None:
    """Prompt for the password when neither CLI nor env var supplied it."""
    if not cfg["password"]:
        cfg["password"] = getpass.getpass(f"Qualys password for {cfg['username']}: ")
        sources["password"] = "prompt"


# =============================================================================
# LOGGING (same format as gethld: time | level | PID | workflow | message)
# =============================================================================
def setup_logger(log_file: Path, workflow: str, level: str = "INFO") -> logging.Logger:
    """Configure file + console logging. Safe to call again in child processes."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(
        f"%(asctime)s | %(levelname)-8s | PID:%(process)d | workflow:{workflow} | %(message)s"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8", delay=True)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def display_option_value(spec: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """Human-readable option value for the log, with secrets masked."""
    key = spec["key"]
    if spec.get("secret"):
        return "******** (set)" if cfg.get(key) else "(not set)"
    if key == "filter":
        return json.dumps(cfg["filter"]) if cfg["filter"] else "(none - all assets)"
    if key == "url_params":
        return str(cfg["url_params_input"]) if cfg["url_params_input"] else "(none)"
    if key == "asset_last_updated":
        return cfg["asset_last_updated_input"]
    return str(cfg.get(key))


def describe_retry_policy(cfg: Dict[str, Any]) -> str:
    """Summarize the retry schedule, e.g. '15 retries | 30s, 60s, 90s, ... | worst case ~52 min'."""
    delays = [compute_backoff_delay(n, cfg) for n in range(1, cfg["max_retries"] + 1)]
    if not delays:
        return "retries disabled (max_retries=0)"
    preview = ", ".join(f"{d}s" for d in delays[:6]) + (", ..." if len(delays) > 6 else "")
    return (f"{cfg['max_retries']} retries | linear backoff +{cfg['retry_delay']}s capped at "
            f"{cfg['retry_max_delay']}s | schedule: {preview} | "
            f"worst-case wait per call ~{sum(delays) / 60:.1f} min")


def log_startup_configuration(logger: logging.Logger, cfg: Dict[str, Any], sources: Dict[str, str],
                              output_dir: Path, log_file: Path) -> None:
    """Record every option for this run (value + source) and the effective settings."""
    logger.info("=" * 70)
    logger.info(f"{PROGRAM_NAME} v{VERSION} ({VERSION_DATE})")
    logger.info("=" * 70)
    logger.info(f"Python version         : {platform.python_version()}")
    logger.info(f"OS version             : {platform.platform()}")
    logger.info(f"Machine                : {platform.machine()} | Processor: {platform.processor() or 'N/A'}")
    logger.info(f"Workflow               : {cfg['workflow']}")
    logger.info(f"Output directory       : {output_dir.resolve()}")
    logger.info(f"Log file               : {log_file.resolve()}")
    logger.info(f"User-Agent             : {cfg['user_agent']}")
    logger.info(f"Auth endpoint          : {build_url(cfg, AUTH_PATH)}")
    logger.info(f"Count endpoint         : {build_url(cfg, COUNT_PATH)}")
    logger.info(f"Search endpoint        : {build_url(cfg, SEARCH_PATH)}")

    logger.info("--- Run options (value | source) ---")
    for spec in OPTION_SPECS:
        logger.info(f"{spec['flags'][-1]:<22} : {display_option_value(spec, cfg)} | "
                    f"source={sources.get(spec['key'], 'default')}")

    logger.info("--- Effective settings ---")
    logger.info(f"assetLastUpdated (API) : {cfg['asset_last_updated']}")
    logger.info(f"Filter request body    : {json.dumps(cfg['filter']) if cfg['filter'] else '(none - all assets)'}")
    logger.info(f"Search URL params      : {build_search_params(cfg, None)}")
    if cfg["ignored_url_params"]:
        logger.warning(f"Ignored protected URL params (managed by this program): "
                       f"{', '.join(cfg['ignored_url_params'])}")
    logger.info(f"Retry policy           : {describe_retry_policy(cfg)}")
    logger.info("Retryable conditions   : timeouts, connection errors, HTTP 408, 429, 5xx "
                "(standard Retry-After header honored if present), and HTTP 2xx responses with "
                "malformed JSON or missing hasMore/lastSeenAssetId/count. HTTP 401 triggers one token refresh.")
    logger.info(f"Token refresh interval : {cfg['token_refresh_sec']}s ({cfg['token_refresh_sec'] / 3600:.2f} h)")
    logger.info(f"TLS verification       : {'ENABLED' if cfg['tls_verify'] else 'DISABLED'}")
    logger.info(f"Monitor interval       : {cfg['monitor_interval']}s")
    if cfg["max_pages"]:
        logger.info(f"Test limit             : {cfg['max_pages']} API call(s) "
                    f"(up to {cfg['max_pages'] * cfg['page_size']:,} assets)")
    else:
        logger.info("Test limit             : disabled (download all pages)")
    logger.info("=" * 70)


def log_count_report(logger: logging.Logger, cfg: Dict[str, Any], count: int) -> None:
    """Report the pre-flight asset count and what the download will do."""
    expected_calls = math.ceil(count / cfg["page_size"]) if count else 0
    logger.info("=" * 70)
    logger.info("ASSET COUNT (Count API pre-flight)")
    logger.info("=" * 70)
    logger.info(f"Assets matching criteria : {count:,}")
    logger.info(f"assetLastUpdated         : {cfg['asset_last_updated']}")
    logger.info(f"Filter                   : {json.dumps(cfg['filter']) if cfg['filter'] else '(none - all assets)'}")
    logger.info(f"Page size                : {cfg['page_size']}")
    logger.info(f"Expected API calls       : {expected_calls:,}")
    if cfg["max_pages"]:
        planned = min(count, cfg["max_pages"] * cfg["page_size"])
        logger.info(f"Test limit               : {cfg['max_pages']} call(s) -> up to {planned:,} assets")
    logger.info("=" * 70)


def log_final_summary(logger: logging.Logger, status: str, stats: Dict[str, Any], cfg: Dict[str, Any],
                      output_dir: Path, log_file: Path, duration: float) -> None:
    """Final run report, written whether the run succeeded, failed or was interrupted."""
    expected = stats["expected_count"]
    logger.info("=" * 70)
    logger.info(f"RUN {status}")
    logger.info("=" * 70)
    logger.info(f"Expected assets (count)  : {expected:,}" if expected is not None
                else "Expected assets (count)  : not retrieved")
    logger.info(f"Assets retrieved         : {stats['assets']:,}")
    logger.info(f"Output files written     : {stats['pages']:,}")
    logger.info(f"Total output size        : {stats['bytes'] / (1024 ** 2):.1f} MB")
    logger.info(f"Last lastSeenAssetId     : {stats['last_seen_asset_id']}")
    logger.info(f"Retries performed        : {stats['retries']} "
                f"(invalid response bodies: {stats['invalid_responses']})")
    logger.info(f"Bearer tokens requested  : {stats['token_requests']}")
    if stats["stopped_by_test_limit"]:
        logger.info(f"Stopped by test limit    : --max-pages {cfg['max_pages']}")
    logger.info(f"Output directory         : {output_dir.resolve()}")
    logger.info(f"Log file                 : {log_file.resolve()}")
    logger.info(f"Total time               : {duration / 60:.1f} minutes")
    if status == "COMPLETED" and expected is not None and not stats["stopped_by_test_limit"] \
            and stats["assets"] > expected:
        logger.warning(f"Retrieved {stats['assets']:,} assets, {stats['assets'] - expected:,} more than the "
                       f"count of {expected:,}. Assets were likely added or updated during the download.")
    if status == "INCOMPLETE":
        logger.error(f"Retrieved {stats['assets']:,} of {expected:,} assets counted "
                     f"({expected - stats['assets']:,} missing). Rerun to get a complete inventory.")
    if status not in ("COMPLETED", "INCOMPLETE") and stats["pages"]:
        logger.warning(f"Files written before the stop are kept. Last good page: {stats['pages']:05d}, "
                       f"lastSeenAssetId={stats['last_seen_asset_id']}")
    logger.info("=" * 70)


# =============================================================================
# BACKGROUND MONITORS (separate processes, same log file)
# =============================================================================
def system_monitor(log_file: Path, output_dir: Path, workflow: str, log_level: str, interval: int) -> None:
    """Log CPU, RAM, swap and disk usage every `interval` seconds."""
    logger = setup_logger(log_file, workflow, log_level)
    logger.info("=== SYSTEM RESOURCE MONITOR STARTED ===")
    logger.info(f"Monitoring output directory: {output_dir.resolve()}")
    gib = 1024 ** 3
    try:
        while True:
            try:
                cpu = psutil.cpu_percent(interval=1)
                mem = psutil.virtual_memory()
                swap = psutil.swap_memory()
                disk = psutil.disk_usage(str(output_dir))
                logger.info(
                    f"MONITOR | CPU: {cpu:5.1f}% | "
                    f"RAM: {mem.percent:5.1f}% ({mem.used / gib:.1f}/{mem.total / gib:.1f} GB) | "
                    f"SWAP: {swap.percent:5.1f}% ({swap.used / gib:.1f}/{swap.total / gib:.1f} GB) | "
                    f"DISK: {disk.percent:5.1f}% ({disk.used / gib:.1f}/{disk.total / gib:.1f} GB)"
                )
            except Exception as exc:  # keep monitoring even if one sample fails
                logger.warning(f"System monitor error: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("System monitor received shutdown signal")


def file_monitor(log_file: Path, output_dir: Path, workflow: str, log_level: str, interval: int) -> None:
    """Log how many JSON files are complete and which are still being written."""
    logger = setup_logger(log_file, workflow, log_level)
    logger.info("=== FILE MONITOR STARTED ===")
    logger.info(f"Watching directory: {output_dir.resolve()}")
    try:
        while True:
            try:
                completed = list(output_dir.glob(f"{OUTPUT_FILE_PREFIX}_*.json"))
                in_progress = [
                    f"{part.name} ({part.stat().st_size / (1024 ** 2):.1f} MB)"
                    for part in output_dir.glob(f"{OUTPUT_FILE_PREFIX}_*.json.part")
                ]
                total_mb = sum(f.stat().st_size for f in completed) / (1024 ** 2)
                logger.info(f"FILE MONITOR | Completed JSON files: {len(completed)} ({total_mb:.1f} MB) | "
                            f"Still writing: {', '.join(in_progress) if in_progress else 'None'}")
            except Exception as exc:
                logger.warning(f"File monitor error: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("File monitor received shutdown signal")


def start_monitors(cfg: Dict[str, Any], output_dir: Path, log_file: Path,
                   logger: logging.Logger) -> List[Tuple[mp.Process, str]]:
    """Start the system and file monitors as daemon processes."""
    monitors = []
    for target, name in ((system_monitor, "System resource monitor"), (file_monitor, "File monitor")):
        process = mp.Process(
            target=target,
            args=(log_file, output_dir, cfg["workflow"], cfg["log_level"], cfg["monitor_interval"]),
            daemon=True,
            name=name,
        )
        process.start()
        logger.info(f"{name} started (PID {process.pid})")
        monitors.append((process, name))
    return monitors


def stop_monitors(monitors: List[Tuple[mp.Process, str]], logger: logging.Logger) -> None:
    """Terminate monitor processes, forcing them if they don't exit within 2 seconds."""
    for process, name in monitors:
        if process.is_alive():
            try:
                process.terminate()
                process.join(timeout=2.0)
                if process.is_alive():
                    process.kill()
            except Exception as exc:
                logger.warning(f"Could not stop {name}: {exc}")
        logger.info(f"{name} stopped")


# =============================================================================
# HTTP + RETRY RESILIENCE
# =============================================================================
def build_url(cfg: Dict[str, Any], path: str) -> str:
    return f"https://{cfg['gateway']}{path}"


def create_session(cfg: Dict[str, Any], logger: logging.Logger) -> requests.Session:
    """Reusable HTTP session (connection pooling) with the configured TLS setting."""
    session = requests.Session()
    session.verify = cfg["tls_verify"]
    if not cfg["tls_verify"]:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning("TLS certificate verification is DISABLED (--insecure)")
    return session


def build_request_headers(cfg: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": cfg["user_agent"],
        "X-Requested-With": cfg["user_agent"],
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def is_retryable_status(status_code: int) -> bool:
    """Retry throttling, request timeouts and server errors; everything else is final."""
    return status_code in (408, 429) or 500 <= status_code <= 599


def compute_backoff_delay(retry_number: int, cfg: Dict[str, Any]) -> int:
    """Linear backoff: retry_delay * n, capped at retry_max_delay (30, 60, 90 ... 300)."""
    return min(cfg["retry_delay"] * retry_number, cfg["retry_max_delay"])


def get_server_requested_wait(response: requests.Response) -> int:
    """Seconds the server asked us to wait via the standard HTTP Retry-After header (0 if absent)."""
    value = response.headers.get("Retry-After", "").strip()
    return int(value) if value.isdigit() else 0


def log_response_metadata(response: requests.Response, label: str, elapsed: float,
                          logger: logging.Logger) -> None:
    """Log status, timing and rate-limit headers (full headers at DEBUG)."""
    rate_info = {key: response.headers[key] for key in RATE_LIMIT_HEADERS if key in response.headers}
    logger.info(f"{label} | HTTP {response.status_code} {response.reason} | {elapsed:.2f}s | "
                f"Rate Limit Info: {rate_info or 'none reported'}")
    logger.debug(f"{label} | Response headers: {dict(response.headers)}")


def call_with_retry(session: requests.Session, cfg: Dict[str, Any], logger: logging.Logger,
                    stats: Dict[str, Any], method: str, url: str, label: str,
                    validate: Callable[[requests.Response], Any],
                    params: Optional[Dict[str, Any]] = None,
                    json_body: Optional[Dict[str, Any]] = None,
                    form_data: Optional[Dict[str, str]] = None,
                    token_state: Optional[Dict[str, Any]] = None,
                    extra_headers: Optional[Dict[str, str]] = None) -> Any:
    """Send a request and return validate(response), retrying transient failures with linear backoff.

    Retried:     timeouts, connection errors, HTTP 408/429/5xx, and HTTP 2xx responses whose
                 body fails validation (validate raises ValueError: malformed JSON, missing
                 hasMore/lastSeenAssetId/count, wrong field types).
    Not retried: TLS errors, other HTTP 4xx, and RuntimeError raised by validate (for example
                 a Qualys responseCode other than SUCCESS, which usually means a bad filter).
    When token_state is given, a valid bearer token is attached to every attempt and one
    HTTP 401 forces a token refresh. form_data (credentials) is never logged.
    """
    retries_used = 0
    refreshed_after_401 = False

    while True:
        headers = build_request_headers(cfg, extra_headers)
        if token_state is not None:
            headers["Authorization"] = f"Bearer {get_valid_token(session, cfg, token_state, stats, logger)}"
        logger.debug(f"{label} | {method} {url} | params={params} | body={json.dumps(json_body) if json_body else None}")

        server_wait = 0
        started = time.monotonic()
        try:
            response = session.request(method, url, params=params, json=json_body, data=form_data,
                                        headers=headers, timeout=cfg["timeout"])
        except requests.exceptions.SSLError as exc:
            logger.error(f"{label} | TLS/SSL error (not retried): {exc}. "
                         "Check proxy/CA settings; --insecure disables verification.")
            raise RuntimeError(f"{label} failed with a TLS/SSL error") from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            log_response_metadata(response, label, time.monotonic() - started, logger)
            status = response.status_code
            if response.ok:
                try:
                    return validate(response)
                except ValueError as exc:
                    stats["invalid_responses"] += 1
                    failure = f"Invalid response body: {exc}"
                except RuntimeError as exc:
                    logger.error(f"{label} | {exc} (not retried)")
                    raise RuntimeError(f"{label} | {exc}") from exc
            elif status == 401 and token_state is not None and not refreshed_after_401:
                logger.warning(f"{label} | HTTP 401 Unauthorized - forcing bearer token refresh and retrying")
                get_valid_token(session, cfg, token_state, stats, logger, force=True)
                refreshed_after_401 = True
                continue
            elif not is_retryable_status(status):
                logger.error(f"{label} | Non-retryable HTTP {status} {response.reason}. "
                             f"Response body: {response.text[:1000]}")
                raise RuntimeError(f"{label} failed with non-retryable HTTP {status} {response.reason}")
            else:
                failure = f"HTTP {status} {response.reason}"
                server_wait = get_server_requested_wait(response)

        if retries_used >= cfg["max_retries"]:
            logger.error(f"{label} | Giving up after {retries_used} retries. Last error: {failure}")
            raise RuntimeError(f"{label} failed after {retries_used} retries: {failure}")

        retries_used += 1
        stats["retries"] += 1
        wait = max(compute_backoff_delay(retries_used, cfg), server_wait)
        hint = f" (server requested {server_wait}s)" if server_wait else ""
        logger.warning(f"{label} | {failure} | Retry {retries_used}/{cfg['max_retries']} in {wait}s{hint}")
        time.sleep(wait)


# =============================================================================
# RESPONSE VALIDATION
# Validators raise ValueError for problems worth retrying (malformed or incomplete
# body) and RuntimeError for final answers from Qualys (responseCode != SUCCESS).
# =============================================================================
def body_snippet(response: requests.Response, limit: int = 300) -> str:
    text = response.text.strip().replace("\n", " ")
    return text[:limit] + (" ..." if len(text) > limit else "") if text else "(empty body)"


def decode_json_object(response: requests.Response) -> Dict[str, Any]:
    """Parse the body as a JSON object; malformed JSON is a retryable ValueError."""
    try:
        data = json.loads(response.text)
    except ValueError as exc:
        raise ValueError(f"malformed JSON ({exc}); body starts: {body_snippet(response)}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}; body starts: {body_snippet(response)}")
    return data


def check_response_code(data: Dict[str, Any]) -> None:
    """A responseCode other than SUCCESS is a definitive Qualys answer, so it is not retried."""
    code = data.get("responseCode")
    if code is not None and code != "SUCCESS":
        raise RuntimeError(f"Qualys responseCode={code}: {data.get('responseMessage')}")


def parse_has_more(value: Any) -> bool:
    """Accept 0/1, true/false (int, bool or string); anything else is invalid."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true"):
        return True
    if text in ("0", "false"):
        return False
    raise ValueError(f"hasMore has unexpected value {value!r}")


def validate_auth_response(response: requests.Response, logger: logging.Logger) -> str:
    """Return the JWT from /auth.

    Plain-text tokens are the classic format. With permissions=true the response may
    be JSON, in which case the non-token fields are logged as permissions.
    """
    body = response.text.strip()
    try:
        payload: Any = json.loads(body)
    except ValueError:
        payload = body

    token: Optional[str] = None
    if isinstance(payload, str):
        token = payload
    elif isinstance(payload, dict):
        token_key = next((k for k in ("token", "access_token", "jwt", "authToken") if payload.get(k)), None)
        token = payload.get(token_key) if token_key else None
        details = {k: v for k, v in payload.items() if k != token_key}
        if details:
            summary = json.dumps(details)
            logger.info(f"AUTH API | Permissions returned: {summary[:2000]}"
                        f"{' ... (full detail at DEBUG)' if len(summary) > 2000 else ''}")
            logger.debug(f"AUTH API | Full permissions: {summary}")

    if not isinstance(token, str) or not token.strip() or " " in token.strip():
        raise ValueError(f"no bearer token in /auth response; body starts: {body_snippet(response, 100)}")
    return token.strip()


def validate_count_response(response: requests.Response) -> int:
    """Return the asset count; a missing or non-numeric 'count' is retryable."""
    data = decode_json_object(response)
    check_response_code(data)
    count = data.get("count")
    if isinstance(count, bool) or not str(count).strip().isdigit():
        raise ValueError(f"'count' missing or not a non-negative integer (got {count!r}); "
                         f"body starts: {body_snippet(response)}")
    return int(count)


def validate_search_response(response: requests.Response) -> Dict[str, Any]:
    """Check a search page is complete and return {'data', 'assets', 'has_more', 'last_seen_asset_id'}.

    Retryable (ValueError): malformed JSON, missing/invalid hasMore, assetListData or
    assetListData.asset of the wrong type, hasMore=1 with no assets or no lastSeenAssetId.
    """
    data = decode_json_object(response)
    check_response_code(data)

    if "hasMore" not in data:
        raise ValueError(f"'hasMore' missing from response; body starts: {body_snippet(response)}")
    has_more = parse_has_more(data["hasMore"])

    asset_list_data = data.get("assetListData")
    if asset_list_data is None:
        assets: List[Any] = []
    elif isinstance(asset_list_data, dict):
        assets = asset_list_data.get("asset") or []
        if not isinstance(assets, list):
            raise ValueError(f"assetListData.asset is {type(assets).__name__}, expected a list")
    else:
        raise ValueError(f"assetListData is {type(asset_list_data).__name__}, expected an object")

    last_seen_asset_id = data.get("lastSeenAssetId")
    if has_more and last_seen_asset_id is None:
        raise ValueError("hasMore=1 but 'lastSeenAssetId' is missing, cannot request the next page")
    if has_more and not assets:
        raise ValueError("hasMore=1 but the page contains no assets")

    return {"data": data, "assets": assets, "has_more": has_more, "last_seen_asset_id": last_seen_asset_id}


# =============================================================================
# AUTHENTICATION (bearer token, refreshed every token_refresh_sec)
# =============================================================================
def new_token_state() -> Dict[str, Any]:
    return {"token": None, "acquired_at": 0.0}


def request_bearer_token(session: requests.Session, cfg: Dict[str, Any], stats: Dict[str, Any],
                         logger: logging.Logger) -> str:
    """POST /auth with username, password, token=true and permissions=true."""
    form_data = {
        "username": cfg["username"],
        "password": cfg["password"],
        "token": "true",
        "permissions": "true",
    }
    return call_with_retry(
        session, cfg, logger, stats, "POST", build_url(cfg, AUTH_PATH), "AUTH API",
        validate=lambda response: validate_auth_response(response, logger),
        form_data=form_data,
        extra_headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
    )


def get_valid_token(session: requests.Session, cfg: Dict[str, Any], token_state: Dict[str, Any],
                    stats: Dict[str, Any], logger: logging.Logger, force: bool = False) -> str:
    """Return the cached token, requesting a new one if missing, too old or forced."""
    age = time.monotonic() - token_state["acquired_at"]
    if token_state["token"] and not force and age < cfg["token_refresh_sec"]:
        return token_state["token"]

    if not token_state["token"]:
        reason = "initial authentication"
    elif force:
        reason = "forced refresh after HTTP 401"
    else:
        reason = f"scheduled refresh (token age {age / 60:.1f} min)"
    logger.info(f"AUTH API | Requesting bearer token: {reason}")

    token_state["token"] = request_bearer_token(session, cfg, stats, logger)
    token_state["acquired_at"] = time.monotonic()
    stats["token_requests"] += 1
    logger.info(f"AUTH API | Bearer token acquired ({len(token_state['token'])} chars, value not logged). "
                f"Next refresh in {cfg['token_refresh_sec'] / 60:.0f} min")
    return token_state["token"]


# =============================================================================
# ASSET COUNT + ASSET INVENTORY
# =============================================================================
def new_run_stats() -> Dict[str, Any]:
    return {
        "expected_count": None,
        "pages": 0,
        "assets": 0,
        "bytes": 0,
        "retries": 0,
        "invalid_responses": 0,
        "token_requests": 0,
        "last_seen_asset_id": None,
        "stopped_by_test_limit": False,
    }


def get_asset_count(session: requests.Session, cfg: Dict[str, Any], token_state: Dict[str, Any],
                    stats: Dict[str, Any], logger: logging.Logger) -> int:
    """Count assets matching the filter and assetLastUpdated (POST /rest/2.0/count/am/asset)."""
    logger.info("--- Starting Count API call ---")
    count = call_with_retry(
        session, cfg, logger, stats, "POST", build_url(cfg, COUNT_PATH), "COUNT API",
        validate=validate_count_response,
        params={"assetLastUpdated": cfg["asset_last_updated"]},
        json_body=cfg["filter"],
        token_state=token_state,
    )
    logger.info("--- Count API call completed ---")
    return count


def build_search_params(cfg: Dict[str, Any], last_seen_asset_id: Optional[Any]) -> Dict[str, Any]:
    """User URL params plus the parameters this program manages."""
    params: Dict[str, Any] = dict(cfg["url_params"])
    params["assetLastUpdated"] = cfg["asset_last_updated"]
    params["pageSize"] = cfg["page_size"]
    if last_seen_asset_id is not None:
        params["lastSeenAssetId"] = last_seen_asset_id
    return params


def write_page_to_file(output_dir: Path, page_number: int, data: Dict[str, Any], compact: bool) -> Tuple[Path, int]:
    """Write one API response to asset_inventory_NNNNN.json.

    Data goes to a .part file first and is renamed when complete, so a crash
    never leaves a truncated .json file behind.
    """
    final_path = output_dir / f"{OUTPUT_FILE_PREFIX}_{page_number:05d}.json"
    temp_path = final_path.with_name(final_path.name + ".part")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=None if compact else 2, ensure_ascii=False)
    os.replace(temp_path, final_path)
    return final_path, final_path.stat().st_size


def download_asset_inventory(session: requests.Session, cfg: Dict[str, Any], token_state: Dict[str, Any],
                             stats: Dict[str, Any], output_dir: Path, logger: logging.Logger) -> None:
    """Page through POST /rest/2.0/search/am/asset using lastSeenAssetId until hasMore=0.

    Every page is validated before it is written, so only complete pages reach disk.
    """
    url = build_url(cfg, SEARCH_PATH)
    expected = stats["expected_count"] or 0
    expected_pages = math.ceil(expected / cfg["page_size"]) if expected else 0
    last_seen_asset_id: Optional[Any] = None
    page_number = 0

    logger.info("--- Starting Asset Inventory (Search API) download ---")
    while True:
        page_number += 1
        label = f"SEARCH API page {page_number:05d}"
        started = time.monotonic()

        page = call_with_retry(
            session, cfg, logger, stats, "POST", url, label,
            validate=validate_search_response,
            params=build_search_params(cfg, last_seen_asset_id),
            json_body=cfg["filter"],
            token_state=token_state,
        )
        path, size = write_page_to_file(output_dir, page_number, page["data"], cfg["compact"])

        next_cursor = page["last_seen_asset_id"]
        stats["pages"] = page_number
        stats["assets"] += len(page["assets"])
        stats["bytes"] += size
        if next_cursor is not None:
            stats["last_seen_asset_id"] = next_cursor

        progress = f"{stats['assets']:,}/{expected:,} ({stats['assets'] / expected * 100:.1f}%)" if expected \
            else f"{stats['assets']:,}"
        logger.info(f"{label} | {len(page['assets'])} assets -> {path.name} ({size / (1024 ** 2):.2f} MB) | "
                    f"lastSeenAssetId={next_cursor} | hasMore={int(page['has_more'])} | "
                    f"page {page_number}/{expected_pages or '?'} | progress {progress} | "
                    f"{time.monotonic() - started:.1f}s")

        if not page["has_more"]:
            logger.info("hasMore=0 - all pages retrieved")
            break
        if next_cursor == last_seen_asset_id:
            raise RuntimeError(f"{label} reported hasMore=1 but lastSeenAssetId did not advance "
                               f"({next_cursor}); stopping to avoid an infinite loop")
        if cfg["max_pages"] and page_number >= cfg["max_pages"]:
            stats["stopped_by_test_limit"] = True
            logger.warning(f"Test limit reached (--max-pages {cfg['max_pages']}) - stopping early")
            break
        last_seen_asset_id = next_cursor

    logger.info("--- Asset Inventory download completed ---")


def is_download_short(stats: Dict[str, Any]) -> bool:
    """True when a full (non-test-limited) download returned fewer assets than the count."""
    expected = stats["expected_count"]
    return (expected is not None and not stats["stopped_by_test_limit"]
            and stats["assets"] < expected)


# =============================================================================
# MAIN
# =============================================================================
def print_config_problems(problems: List[str]) -> None:
    print("\n" + "=" * 80)
    print("CONFIGURATION ERRORS")
    print("=" * 80)
    for problem in problems:
        print(f"  - {problem}")
    print(f"\nRun 'python {PROGRAM_NAME}.py -h' for help.")
    print("=" * 80 + "\n")


def create_output_dir(workflow: str) -> Path:
    output_dir = Path(f"{OUTPUT_DIR_PREFIX}_{workflow}")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def main() -> None:
    workflow = datetime.now().strftime("%Y%m%d%H%M%S")
    run_started = time.monotonic()

    args = build_argument_parser().parse_args()
    cfg, sources, problems = build_config(args)
    if problems:
        print_config_problems(problems)
        sys.exit(2)
    try:
        ensure_password(cfg, sources)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled before start.")
        sys.exit(130)
    if not cfg["password"]:
        print_config_problems(["password is required"])
        sys.exit(2)

    cfg["workflow"] = workflow
    cfg["user_agent"] = f"{PROGRAM_NAME}_v{VERSION}_{VERSION_DATE}_{workflow}"

    output_dir = create_output_dir(workflow)
    log_file = output_dir / LOG_FILE_NAME
    logger = setup_logger(log_file, workflow, cfg["log_level"])

    stats = new_run_stats()
    monitors: List[Tuple[mp.Process, str]] = []
    session: Optional[requests.Session] = None
    status, exit_code = "FAILED", 1

    try:
        log_startup_configuration(logger, cfg, sources, output_dir, log_file)
        monitors = start_monitors(cfg, output_dir, log_file, logger)
        session = create_session(cfg, logger)
        token_state = new_token_state()

        get_valid_token(session, cfg, token_state, stats, logger)
        stats["expected_count"] = get_asset_count(session, cfg, token_state, stats, logger)
        log_count_report(logger, cfg, stats["expected_count"])

        if stats["expected_count"] == 0:
            logger.warning("Count API returned 0 assets for this filter/date - nothing to download. "
                           "To widen the search, use an older --assetLastUpdated "
                           "(e.g. 1970-01-01T00:00:00Z) or check the filter.")
            status, exit_code = "COMPLETED", 0
        else:
            download_asset_inventory(session, cfg, token_state, stats, output_dir, logger)
            if is_download_short(stats):
                status, exit_code = "INCOMPLETE", 1
            else:
                status, exit_code = "COMPLETED", 0

    except KeyboardInterrupt:
        status, exit_code = "INTERRUPTED (Ctrl+C)", 130
        logger.warning("Script interrupted by user (Ctrl+C) - shutting down")
    except EOFError:
        status, exit_code = "INTERRUPTED (Ctrl+D / EOF)", 130
        logger.warning("Script interrupted by user (Ctrl+D / EOF) - shutting down")
    except RuntimeError as exc:
        logger.error(f"Run failed: {exc}")
    except Exception as exc:  # unexpected: keep the traceback for troubleshooting
        logger.error(f"Unexpected error: {exc}", exc_info=True)
    finally:
        if session is not None:
            session.close()
        stop_monitors(monitors, logger)
        log_final_summary(logger, status, stats, cfg, output_dir, log_file, time.monotonic() - run_started)

    sys.exit(exit_code)


if __name__ == "__main__":
    mp.freeze_support()  # required for Windows frozen executables
    main()
