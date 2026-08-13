#!/usr/bin/env bash
# One-shot setup: system packages, virtualenv, dependencies, tests.
#
# Safe to re-run — it updates an existing install rather than failing, so if it
# stops partway you can just run it again.
#
#   ./scripts/setup.sh
#
# Override with environment variables if needed:
#   SNIPER_PYTHON=python3.12 ./scripts/setup.sh
set -euo pipefail

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

step() { echo; echo "${BOLD}==> $*${OFF}"; }
ok()   { echo "${GREEN}  ok${OFF} $*"; }
warn() { echo "${YELLOW}  ! ${OFF}$*"; }
die()  { echo; echo "${RED}FAILED:${OFF} $*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

# --- Refuse to run somewhere that will break later -------------------------

case "$ROOT" in
  /mnt/[a-z]/*)
    die "This is on a Windows drive ($ROOT).

Windows drives are mounted without working file permissions, so the wallet
file's safety check can never pass and the bot would refuse to start.

Move to your Linux home folder instead:
    cd ~ && git clone -b claude/pump-fun-sniper-bot-wunrae \\
        https://github.com/flashcrash112/Sniper.git sniper && cd sniper
    ./scripts/setup.sh"
    ;;
esac

# --- System packages -------------------------------------------------------

step "Checking system packages"
MISSING=()
for cmd in git curl; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
# python3-venv is a separate package on Debian/Ubuntu and is not a command.
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
  MISSING+=("python3-venv")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "  installing: ${MISSING[*]}"
    echo "  (you may be asked for your Linux password)"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip "${MISSING[@]}" \
      || die "package install failed — check the error above"
  else
    die "missing: ${MISSING[*]} — install them with your package manager first"
  fi
fi
ok "system packages present"

# --- Python ----------------------------------------------------------------

step "Checking Python"
PYTHON="${SNIPER_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  # Newest first: 3.11 is the minimum (tomllib), so prefer anything above it.
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  CURRENT="$(python3 --version 2>&1 || echo 'not found')"
  if command -v apt-get >/dev/null 2>&1; then
    warn "need Python 3.11+, found: $CURRENT — installing python3.12"
    sudo apt-get install -y -qq python3.12 python3.12-venv \
      || die "could not install python3.12.

Try:  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update
then re-run this script."
    PYTHON=python3.12
  else
    die "need Python 3.11 or newer, found: $CURRENT"
  fi
fi
ok "using $PYTHON ($("$PYTHON" --version 2>&1))"

# --- Virtualenv ------------------------------------------------------------

step "Setting up the virtual environment"
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  warn "existing .venv uses an old Python — rebuilding it"
  rm -rf .venv
fi
[ -d .venv ] || "$PYTHON" -m venv .venv || die "could not create the virtualenv"
ok ".venv ready"

step "Installing dependencies (this takes a few minutes)"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt \
  || die "dependency install failed — check the error above"
./.venv/bin/python -m pip install --quiet pytest pytest-asyncio
ok "dependencies installed"

# --- Verify ----------------------------------------------------------------

step "Running the test suite"
if ./.venv/bin/python -m pytest -q; then
  ok "all tests passed"
else
  die "tests failed — do not go further until this is resolved"
fi

# --- Done ------------------------------------------------------------------

cat <<EOF

${GREEN}${BOLD}Setup complete.${OFF}

Every new terminal, activate the environment first:

    ${BOLD}cd $ROOT && source .venv/bin/activate${OFF}

Then, in order:

  1. Create your wallet (pick a strong password and save it — there is no reset):

        export SNIPER_KEYSTORE_PASSWORD='your-password-here'
        python -m sniper keystore create ./keystore.json --generate

  2. Get a free API key from https://helius.dev and set it:

        export HELIUS_API_KEY=your-key-here

  3. Make your config (already set to the free feed):

        cp config.example.toml config.toml
        sed -i 's/^backend = .*/backend = "logs"/; s|atlas-mainnet|mainnet|' config.toml

  4. Check everything works, then watch it run without spending anything:

        python -m sniper check
        python -m sniper verify-layout
        python -m sniper run --dry-run

Full walkthrough: WINDOWS.md
EOF
