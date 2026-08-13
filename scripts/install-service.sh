#!/usr/bin/env bash
# Install the sniper as a systemd service on a fresh Debian/Ubuntu VPS.
#
# Run as root on the server:
#
#   curl -fsSL https://raw.githubusercontent.com/flashcrash112/Sniper/claude/pump-fun-sniper-bot-wunrae/scripts/install-service.sh | bash
#
# or, if you have already cloned the repo:
#
#   sudo ./scripts/install-service.sh
#
# Creates a non-root `sniper` user, installs into /opt/sniper/app, and writes a
# hardened systemd unit — but deliberately does NOT start it. Starting is a
# decision you make after `check`, `verify-layout` and `simulate` all pass.
set -euo pipefail

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
step() { echo; echo "${BOLD}==> $*${OFF}"; }
ok()   { echo "${GREEN}  ok${OFF} $*"; }
warn() { echo "${YELLOW}  ! ${OFF}$*"; }
die()  { echo; echo "${RED}FAILED:${OFF} $*" >&2; exit 1; }

REPO="${SNIPER_REPO:-https://github.com/flashcrash112/Sniper.git}"
BRANCH="${SNIPER_BRANCH:-claude/pump-fun-sniper-bot-wunrae}"
HOME_DIR=/opt/sniper
APP_DIR="$HOME_DIR/app"
SECRETS=/etc/sniper/secrets.env
USER_NAME=sniper

[ "$(id -u)" -eq 0 ] || die "run this as root: sudo $0"
command -v apt-get >/dev/null 2>&1 || die "this expects a Debian/Ubuntu server"

# --- Packages --------------------------------------------------------------

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# chrony because Solana is timing-sensitive and so are TLS sessions.
apt-get install -y -qq python3 python3-venv python3-pip git curl chrony jq \
  || die "package install failed"
systemctl enable --now chrony >/dev/null 2>&1 || warn "could not start chrony"
ok "packages installed"

# --- User and directories --------------------------------------------------

step "Creating the $USER_NAME user"
if id "$USER_NAME" >/dev/null 2>&1; then
  ok "$USER_NAME already exists"
else
  # No login shell and no password: this account exists to run one process.
  adduser --system --group --home "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
  ok "created"
fi
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 "$HOME_DIR"

# --- Code ------------------------------------------------------------------

step "Fetching the bot"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$USER_NAME" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP_DIR" checkout --quiet "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
  ok "updated to latest $BRANCH"
else
  sudo -u "$USER_NAME" git clone --quiet -b "$BRANCH" "$REPO" "$APP_DIR" \
    || die "clone failed — if the repo is private, clone it manually first"
  ok "cloned into $APP_DIR"
fi

step "Running the setup script"
sudo -u "$USER_NAME" bash "$APP_DIR/scripts/setup.sh" \
  || die "setup failed — see the error above"

# --- Secrets ---------------------------------------------------------------

step "Preparing the secrets file"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" /etc/sniper
if [ ! -f "$SECRETS" ]; then
  cat > "$SECRETS" <<'ENVEOF'
# Read by systemd, never by an interactive shell — so it does not end up in
# bash history. Keep this file 0600.
SNIPER_KEYSTORE_PASSWORD=
HELIUS_API_KEY=
ENVEOF
  ok "created $SECRETS (empty — fill it in)"
else
  ok "$SECRETS already exists, leaving it alone"
fi
chmod 600 "$SECRETS"
chown "$USER_NAME:$USER_NAME" "$SECRETS"

# --- systemd unit ----------------------------------------------------------

step "Writing the systemd unit"
cat > /etc/systemd/system/sniper.service <<UNITEOF
[Unit]
Description=pump.fun sniper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$APP_DIR
EnvironmentFile=$SECRETS
ExecStart=$APP_DIR/.venv/bin/python -m sniper run --config $APP_DIR/config.toml

# Deliberately no restart. A sniper that restarts itself can buy again after
# you thought it had stopped; if it died, you want to know why first.
Restart=no
Nice=-10
LimitNOFILE=65535

# It needs the network and its own directory. Nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$HOME_DIR
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNITEOF
if command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null; then
  ok "installed (not started)"
else
  warn "unit written to /etc/systemd/system/sniper.service but systemd did not
    reload it. On a container or a non-systemd host you will need to run the
    bot manually instead of via systemctl."
fi

# --- Next steps ------------------------------------------------------------

cat <<EOF

${GREEN}${BOLD}Server ready.${OFF}  The service is installed but ${BOLD}not running${OFF}.

Three things left, in order:

  ${BOLD}1. Put your wallet on the box.${OFF}
     From your laptop (the keystore is encrypted, so this is safe to copy):

         scp ~/sniper/keystore.json root@THIS_SERVER:$HOME_DIR/keystore.json

     Then back here:

         chown $USER_NAME:$USER_NAME $HOME_DIR/keystore.json
         chmod 600 $HOME_DIR/keystore.json

     Or make a fresh one and fund it:

         sudo -u $USER_NAME $APP_DIR/.venv/bin/python -m sniper \\
             keystore create $HOME_DIR/keystore.json --generate

  ${BOLD}2. Fill in the secrets and the config.${OFF}

         nano $SECRETS          # keystore password + Helius key
         cp $APP_DIR/config.example.toml $APP_DIR/config.toml
         nano $APP_DIR/config.toml
         chown $USER_NAME:$USER_NAME $APP_DIR/config.toml
         chmod 600 $APP_DIR/config.toml

     Point [wallet] keystore_path at $HOME_DIR/keystore.json.

  ${BOLD}3. Verify before starting anything.${OFF}

         cd $APP_DIR
         sudo -u $USER_NAME env \$(grep -v '^#' $SECRETS | xargs) \\
             .venv/bin/python -m sniper check
         sudo -u $USER_NAME env \$(grep -v '^#' $SECRETS | xargs) \\
             .venv/bin/python -m sniper verify-layout
         sudo -u $USER_NAME env \$(grep -v '^#' $SECRETS | xargs) \\
             .venv/bin/python -m sniper simulate

     Only once ${BOLD}simulate${OFF} passes:

         systemctl start sniper
         journalctl -u sniper -f

Full runbook: $APP_DIR/DEPLOY.md
EOF
