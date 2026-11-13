#!/usr/bin/env bash
# ==============================================================================
# scripts/deploy_sandbox.sh
# Automated Provisioning & Verification Script for Cloud Trading Bot Instances
# Compatible with: Instabox sandboxes, GitHub Actions runners, Ubuntu/Debian containers
# ==============================================================================
set -euo pipefail

# ------------------------------------------------------------------------------
# Constants & Colors
# ------------------------------------------------------------------------------
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_TARGET_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly CLR_RESET="\033[0m"
readonly CLR_RED="\033[1;31m"
readonly CLR_GREEN="\033[1;32m"
readonly CLR_YELLOW="\033[1;33m"
readonly CLR_BLUE="\033[1;34m"
readonly CLR_CYAN="\033[1;36m"

# Distinct POSIX Exit Codes
readonly EXIT_SUCCESS=0
readonly EXIT_GENERAL=1
readonly EXIT_SYS_DEP_MISSING=2
readonly EXIT_PIP_INSTALL_FAIL=3
readonly EXIT_IMPORT_VERIFY_FAIL=4
readonly EXIT_ENV_VALIDATION_FAIL=5
readonly EXIT_DIR_PERM_FAIL=6
readonly EXIT_SYNTAX_CHECK_FAIL=7

# Default Parameters
TARGET_DIR="${DEFAULT_TARGET_DIR}"
USE_VENV=true
VENV_PATH=""
SKIP_PIP=false
STRICT_ENV=false
INSTALL_EXTENDED=true

# ------------------------------------------------------------------------------
# Logging Utilities
# ------------------------------------------------------------------------------
log_ts() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_info() {
    echo -e "${CLR_BLUE}[$(log_ts)] [INFO]${CLR_RESET} $*"
}

log_success() {
    echo -e "${CLR_GREEN}[$(log_ts)] [SUCCESS]${CLR_RESET} $*"
}

log_warn() {
    echo -e "${CLR_YELLOW}[$(log_ts)] [WARN]${CLR_RESET} $*" >&2
}

log_error() {
    echo -e "${CLR_RED}[$(log_ts)] [ERROR]${CLR_RESET} $*" >&2
}

log_critical() {
    echo -e "${CLR_RED}[$(log_ts)] [CRITICAL]${CLR_RESET} $*" >&2
}

error_trap() {
    local exit_code=$?
    local line_no=$1
    if [[ ${exit_code} -ne 0 ]]; then
        log_critical "Deployment script failed at line ${line_no} with exit code ${exit_code}."
    fi
    exit ${exit_code}
}
trap 'error_trap ${LINENO}' ERR

# ------------------------------------------------------------------------------
# Usage & Help
# ------------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Provisions a pristine Linux sandbox environment for the Hyperliquid Trading Bot.

Options:
  --target-dir <DIR>      Base directory of trading bot repository (default: ${DEFAULT_TARGET_DIR})
  --venv-path <PATH>      Path to python virtualenv (default: <target-dir>/.venv)
  --no-venv               Do not use virtualenv; install directly into current Python environment
  --skip-pip              Skip package installation (useful if pre-installed in image)
  --no-extended           Skip extended monitoring dependencies (ccxt, aiohttp, rich, pydantic)
  --check-env             Strictly validate .env credentials and exit with code 5 if invalid
  --verify-only           Run import and syntax checks only (no system packages or pip installs)
  -h, --help              Show this help message and exit

Exit Codes:
  0: Success / Clean environment ready
  2: Missing system dependencies (python3, pip, curl)
  3: Pip installation failure
  4: Critical Python import verification failed
  5: Mandatory environment variable validation failed
  6: Directory creation or disk write permission failed
  7: Python syntax compilation failed
EOF
}

# ------------------------------------------------------------------------------
# Parse Arguments
# ------------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-dir)
            TARGET_DIR="$2"
            shift 2
            ;;
        --venv-path)
            VENV_PATH="$2"
            shift 2
            ;;
        --no-venv)
            USE_VENV=false
            shift
            ;;
        --skip-pip)
            SKIP_PIP=true
            shift
            ;;
        --no-extended)
            INSTALL_EXTENDED=false
            shift
            ;;
        --check-env)
            STRICT_ENV=true
            shift
            ;;
        --verify-only)
            SKIP_PIP=true
            shift
            ;;
        -h|--help)
            usage
            exit ${EXIT_SUCCESS}
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit ${EXIT_GENERAL}
            ;;
    esac
done

if [[ -z "${VENV_PATH}" ]]; then
    VENV_PATH="${TARGET_DIR}/.venv"
fi

# ------------------------------------------------------------------------------
# Step 1: System Prerequisite Check & Toolchain
# ------------------------------------------------------------------------------
detect_python() {
    if command -v python3 &>/dev/null && python3 --version &>/dev/null; then
        echo "python3"
    elif command -v python &>/dev/null && python --version &>/dev/null; then
        echo "python"
    else
        echo ""
    fi
}

check_system_prerequisites() {
    log_info "Step 1/6: Checking system toolchain & prerequisites..."

    local py_bin
    py_bin=$(detect_python)

    if [[ -z "${py_bin}" ]]; then
        log_warn "python3/python not found. Attempting package manager installation..."
        if command -v apt-get &>/dev/null; then
            export DEBIAN_FRONTEND=noninteractive
            if [[ $(id -u) -eq 0 ]]; then
                apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv python3-dev build-essential curl procps
            elif command -v sudo &>/dev/null; then
                sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip python3-venv python3-dev build-essential curl procps
            else
                log_critical "Cannot install python3: root or sudo privileges required."
                exit ${EXIT_SYS_DEP_MISSING}
            fi
            py_bin=$(detect_python)
        else
            log_critical "Unsupported package manager. Please install python3 manually."
            exit ${EXIT_SYS_DEP_MISSING}
        fi
    fi

    local py_version
    py_version=$("${py_bin}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    log_success "Found Python version: ${py_version} (using ${py_bin})"

    local py_major py_minor
    py_major=$("${py_bin}" -c "import sys; print(sys.version_info.major)")
    py_minor=$("${py_bin}" -c "import sys; print(sys.version_info.minor)")
    if [[ "${py_major}" -lt 3 ]] || [[ "${py_major}" -eq 3 && "${py_minor}" -lt 9 ]]; then
        log_critical "Python version >= 3.9 is required (detected ${py_version})."
        exit ${EXIT_SYS_DEP_MISSING}
    fi
}

# ------------------------------------------------------------------------------
# Step 2: Directory Initialization & Write Verification
# ------------------------------------------------------------------------------
init_directories() {
    log_info "Step 2/6: Initializing directory structure in ${TARGET_DIR}..."

    local dirs=(
        "${TARGET_DIR}/state"
        "${TARGET_DIR}/logs"
        "${TARGET_DIR}/historical_data"
        "${TARGET_DIR}/sync_staging"
    )

    for d in "${dirs[@]}"; do
        if ! mkdir -p "${d}"; then
            log_critical "Failed to create required directory: ${d}"
            exit ${EXIT_DIR_PERM_FAIL}
        fi

        # Verify write permission
        local probe="${d}/.write_probe_$$"
        if ! touch "${probe}" 2>/dev/null; then
            log_critical "Directory is not writable: ${d}"
            exit ${EXIT_DIR_PERM_FAIL}
        fi
        rm -f "${probe}"
        log_success "Initialized and verified writable: ${d}"
    done
}

# ------------------------------------------------------------------------------
# Step 3: Virtual Environment & Package Installation
# ------------------------------------------------------------------------------
setup_python_env() {
    log_info "Step 3/6: Setting up Python runtime environment..."

    local python_bin
    python_bin=$(detect_python)
    local pip_bin="pip3"
    if ! command -v pip3 &>/dev/null && command -v pip &>/dev/null; then
        pip_bin="pip"
    fi

    if [[ "${USE_VENV}" = true ]]; then
        if [[ ! -d "${VENV_PATH}" ]]; then
            log_info "Creating virtual environment at: ${VENV_PATH}..."
            if ! python3 -m venv "${VENV_PATH}"; then
                log_warn "python3 -m venv failed. Attempting fallback virtualenv creation..."
                pip3 install --user virtualenv || true
                python3 -m virtualenv "${VENV_PATH}" || {
                    log_critical "Failed to create virtual environment."
                    exit ${EXIT_PIP_INSTALL_FAIL}
                }
            fi
        else
            log_info "Using existing virtual environment at: ${VENV_PATH}"
        fi

        # Activate virtualenv if activate script exists
        if [[ -f "${VENV_PATH}/bin/activate" ]]; then
            # shellcheck disable=SC1091
            source "${VENV_PATH}/bin/activate"
            python_bin="${VENV_PATH}/bin/python"
            pip_bin="${VENV_PATH}/bin/pip"
        elif [[ -f "${VENV_PATH}/Scripts/activate" ]]; then
            # Windows bash support
            # shellcheck disable=SC1091
            source "${VENV_PATH}/Scripts/activate"
            python_bin="${VENV_PATH}/Scripts/python"
            pip_bin="${VENV_PATH}/Scripts/pip"
        fi
        log_success "Activated virtualenv: ${VENV_PATH}"
    else
        log_info "Virtualenv disabled (--no-venv). Using host Python environment."
    fi

    if [[ "${SKIP_PIP}" = true ]]; then
        log_info "Skipping pip package installation (--skip-pip)."
        return 0
    fi

    log_info "Upgrading core pip tooling..."
    "${pip_bin}" install --upgrade --no-cache-dir pip setuptools wheel || {
        log_warn "Pip tool upgrade warning, continuing..."
    }

    local req_file="${TARGET_DIR}/requirements.txt"
    if [[ -f "${req_file}" ]]; then
        log_info "Installing dependencies from ${req_file}..."
        "${pip_bin}" install --no-cache-dir -r "${req_file}" || {
            log_critical "Failed to install dependencies from ${req_file}."
            exit ${EXIT_PIP_INSTALL_FAIL}
        }
    else
        log_warn "No requirements.txt found in ${TARGET_DIR}. Installing default core packages..."
        "${pip_bin}" install --no-cache-dir numpy pandas httpx requests python-dotenv eth-account hyperliquid-python-sdk || {
            log_critical "Failed to install core Python packages."
            exit ${EXIT_PIP_INSTALL_FAIL}
        }
    fi

    if [[ "${INSTALL_EXTENDED}" = true ]]; then
        log_info "Installing extended monitoring packages (ccxt, aiohttp, rich, pydantic)..."
        "${pip_bin}" install --no-cache-dir ccxt aiohttp rich pydantic || {
            log_warn "Extended package install encountered warnings; continuing."
        }
    fi

    log_success "Python package installation completed."
}

# ------------------------------------------------------------------------------
# Step 4: Verification of Critical Imports
# ------------------------------------------------------------------------------
verify_critical_imports() {
    log_info "Step 4/6: Verifying critical Python module imports..."

    local verify_script="
import sys

core_modules = [
    ('numpy', 'numpy'),
    ('pandas', 'pandas'),
    ('httpx', 'httpx'),
    ('requests', 'requests'),
    ('dotenv', 'python-dotenv'),
    ('eth_account', 'eth-account'),
    ('hyperliquid', 'hyperliquid-python-sdk'),
]

failed_core = []
print(f'| {\"Module\":<18} | {\"Status\":<8} | {\"Version\":<16} |')
print('|' + '-'*20 + '|' + '-'*10 + '|' + '-'*18 + '|')

for mod_name, pkg_name in core_modules:
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, '__version__', 'installed')
        print(f'| {mod_name:<18} | OK       | {ver:<16} |')
    except Exception as e:
        print(f'| {mod_name:<18} | FAILED   | {str(e)[:16]} |')
        failed_core.append(mod_name)

if failed_core:
    print(f'ERROR: Missing mandatory modules: {failed_core}', file=sys.stderr)
    sys.exit(1)
sys.exit(0)
"

    local py_exec
    py_exec=$(detect_python)
    if [[ "${USE_VENV}" = true ]]; then
        if [[ -f "${VENV_PATH}/bin/python" ]]; then
            py_exec="${VENV_PATH}/bin/python"
        elif [[ -f "${VENV_PATH}/Scripts/python.exe" ]]; then
            py_exec="${VENV_PATH}/Scripts/python.exe"
        fi
    fi

    if ! "${py_exec}" -c "${verify_script}"; then
        log_critical "One or more critical core Python modules failed import verification!"
        exit ${EXIT_IMPORT_VERIFY_FAIL}
    fi

    log_success "All critical Python imports verified successfully."
}

# ------------------------------------------------------------------------------
# Step 5: Environment Variable & Secret Validation
# ------------------------------------------------------------------------------
mask_secret() {
    local secret="$1"
    local len=${#secret}
    if [[ ${len} -le 8 ]]; then
        echo "********"
    else
        echo "${secret:0:6}...${secret: -4}"
    fi
}

validate_environment() {
    log_info "Step 5/6: Validating environment variables and credentials..."

    local env_file="${TARGET_DIR}/.env"
    if [[ -f "${env_file}" ]]; then
        log_info "Reading environment variables from ${env_file}..."
        set -a
        # shellcheck disable=SC1090
        source <(grep -E '^[A-Za-z0-9_]+=' "${env_file}" | sed 's/\r$//')
        set +a
    else
        log_warn ".env file not found at ${env_file}. Checking process environment..."
    fi

    local validation_failed=false

    echo -e "${CLR_CYAN}--- Credentials Validation Matrix ---${CLR_RESET}"
    echo -e "| Variable                 | Status  | Detail                  |"
    echo -e "|--------------------------|---------|-------------------------|"

    # 1. Check HYPE_USER_ADDRESS
    local user_addr="${HYPE_USER_ADDRESS:-}"
    if [[ -z "${user_addr}" ]]; then
        echo -e "| HYPE_USER_ADDRESS        | ${CLR_RED}MISSING${CLR_RESET} | Mandatory for trading   |"
        validation_failed=true
    elif [[ ! "${user_addr}" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
        echo -e "| HYPE_USER_ADDRESS        | ${CLR_RED}INVALID${CLR_RESET} | Expected 0x + 40 hex    |"
        validation_failed=true
    else
        echo -e "| HYPE_USER_ADDRESS        | ${CLR_GREEN}VALID  ${CLR_RESET} | $(mask_secret "${user_addr}")           |"
    fi

    # 2. Check HYPE_SECRET_KEY
    local sec_key="${HYPE_SECRET_KEY:-}"
    if [[ -z "${sec_key}" ]]; then
        echo -e "| HYPE_SECRET_KEY          | ${CLR_RED}MISSING${CLR_RESET} | Mandatory for signing   |"
        validation_failed=true
    elif [[ ! "${sec_key}" =~ ^(0x)?[0-9a-fA-F]{64}$ ]]; then
        echo -e "| HYPE_SECRET_KEY          | ${CLR_RED}INVALID${CLR_RESET} | Expected 64 hex chars   |"
        validation_failed=true
    else
        echo -e "| HYPE_SECRET_KEY          | ${CLR_GREEN}VALID  ${CLR_RESET} | $(mask_secret "${sec_key}")           |"
    fi

    # 3. Check MAINTENANCE_LOCK
    local maint_lock="${MAINTENANCE_LOCK:-true}"
    if [[ "${maint_lock}" =~ ^(true|1|yes)$ ]]; then
        echo -e "| MAINTENANCE_LOCK         | ${CLR_YELLOW}LOCKED ${CLR_RESET} | Bot will halt on boot   |"
    else
        echo -e "| MAINTENANCE_LOCK         | ${CLR_GREEN}ACTIVE ${CLR_RESET} | Live trading unlocked   |"
    fi
    echo -e "${CLR_CYAN}------------------------------------------------------${CLR_RESET}"

    if [[ "${validation_failed}" = true ]]; then
        if [[ "${STRICT_ENV}" = true ]]; then
            log_critical "Mandatory trading environment variables are missing or invalid!"
            exit ${EXIT_ENV_VALIDATION_FAIL}
        else
            log_warn "Mandatory credentials incomplete. Bot will require .env configuration before live trading."
        fi
    else
        log_success "Environment variable validation completed successfully."
    fi
}

# ------------------------------------------------------------------------------
# Step 6: Code Syntax Compilation Check
# ------------------------------------------------------------------------------
verify_code_syntax() {
    log_info "Step 6/6: Verifying bot Python source code syntax..."

    local py_exec
    py_exec=$(detect_python)
    if [[ "${USE_VENV}" = true ]]; then
        if [[ -f "${VENV_PATH}/bin/python" ]]; then
            py_exec="${VENV_PATH}/bin/python"
        elif [[ -f "${VENV_PATH}/Scripts/python.exe" ]]; then
            py_exec="${VENV_PATH}/Scripts/python.exe"
        fi
    fi

    local py_files=(
        "main.py"
        "scripts/rotator.py"
        "watchdog.py"
        "exchange.py"
        "strategy.py"
        "risk_manager.py"
        "alerts.py"
        "utils.py"
    )

    for f in "${py_files[@]}"; do
        local target_f="${TARGET_DIR}/${f}"
        if [[ -f "${target_f}" ]]; then
            if ! "${py_exec}" -m py_compile "${target_f}"; then
                log_critical "Syntax compilation error in file: ${target_f}"
                exit ${EXIT_SYNTAX_CHECK_FAIL}
            fi
            log_success "Syntax valid: ${f}"
        fi
    done

    log_success "All checked source files passed syntax compilation."
}

# ------------------------------------------------------------------------------
# Main Flow
# ------------------------------------------------------------------------------
main() {
    log_info "============================================================"
    log_info "Starting Sandbox Provisioning for Target: ${TARGET_DIR}"
    log_info "============================================================"

    check_system_prerequisites
    init_directories
    setup_python_env
    verify_critical_imports
    validate_environment
    verify_code_syntax

    log_info "============================================================"
    log_success "Sandbox provisioning completed successfully!"
    log_info "Target Directory: ${TARGET_DIR}"
    if [[ "${USE_VENV}" = true ]]; then
        log_info "Virtualenv Path : ${VENV_PATH}"
        log_info "To activate run : source ${VENV_PATH}/bin/activate"
    fi
    log_info "============================================================"
    exit ${EXIT_SUCCESS}
}

main "$@"
