# Going live

A runbook for taking this from a repo to a running sniper. Follow it in order —
the last two steps exist so that the first live buy is not also the first time
the code has ever touched mainnet.

Budget roughly an hour, plus however long you let the dry run soak.

---

## 0. What this costs, before you start

Be clear-eyed about this. Sniping is not a free strategy:

| Item | Rough cost | Notes |
|---|---|---|
| VPS | $10–60/mo | A cheap box is fine; location matters far more than specs |
| Geyser feed | $50–500+/mo | **The real cost.** Free RPC will not win a contested launch |
| Priority fees | 0.01–0.15 SOL *per attempt* | Paid whether or not you win the block |
| Failed buys | Fee only | A reverted buy still costs the priority fee |

If the coin you are waiting for is a known launch that other people are also
waiting for, you are competing against people running colocated bare metal with
staked connections and Jito bundles. This code removes the software latency. It
does not remove that gap. Size your position accordingly.

---

## 1. Pick a VPS, and pick the location carefully

**Location beats specs, by a lot.** You are trying to minimise round-trip time
to your geyser provider and to the current block leader. Solana stake is heavily
concentrated in a few metros:

| Region | Good for | Typical providers |
|---|---|---|
| **Frankfurt** | Largest concentration of stake | Hetzner, Latitude.sh, OVH |
| **Amsterdam** | Close second | Hetzner, Latitude.sh |
| **Ashburn (US-East)** | US stake, most RPC providers | Latitude.sh, Vultr, AWS |

Pick the region your **geyser provider** is in first, and the metro with stake
second. A Hetzner box in Frankfurt at ~$15/mo is a completely reasonable start.
If you later want to compete seriously, move to bare metal in the same
datacentre as your RPC provider.

**Specs:** 2–4 vCPU, 4–8 GB RAM, Debian 12 or Ubuntu 24.04. This process is
single-threaded and idles at almost nothing — the hot path is 187 µs of CPU.
Do not pay for cores. Pay for network.

Avoid: anything "shared CPU burstable" at the bottom of a provider's range, and
anything more than one network hop from a major peering point.

---

## 2. Get a paid geyser feed

This is the part you cannot skip. The `logs` backend against a free public RPC
works, but it sits behind the node's generic pubsub fan-out and it is the first
thing to fall behind exactly when a launch is happening.

Two realistic options:

**Helius Atlas** (`backend = "helius_atlas"`) — geyser data over a websocket,
no protobuf codegen. Requires a paid plan that includes `transactionSubscribe`;
check their current tiers, as the plan names move around. This is the easiest
good option and what I would start with.

**Yellowstone gRPC** (`backend = "yellowstone"`) — a dedicated node or a
Yellowstone provider (Helius dedicated, Triton, QuickNode). Lowest latency,
meaningfully more expensive. Run `./scripts/gen_proto.sh` once to generate the
stubs against your provider's plugin version.

Whichever you pick, get the endpoint URL and API key before continuing.

---

## 3. Build the box

```bash
ssh root@your-vps

# Keep the clock honest — Solana is timing-sensitive and so are TLS sessions.
apt update && apt install -y python3 python3-venv python3-pip git chrony curl
systemctl enable --now chrony

# Never run this as root.
adduser --disabled-password --gecos "" sniper
install -d -o sniper -g sniper -m 750 /opt/sniper
```

```bash
su - sniper
git clone <your-fork> /opt/sniper/app && cd /opt/sniper/app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # 105 tests should pass before you go further
```

If you chose the `yellowstone` backend:

```bash
pip install 'grpcio-tools>=1.60'
./scripts/gen_proto.sh
```

---

## 4. Create a burner wallet

**Do not use your main wallet.** Generate a fresh one that holds only what you
are willing to lose:

```bash
export SNIPER_KEYSTORE_PASSWORD='<a long random password>'
python -m sniper keystore create /opt/sniper/keystore.json --generate
```

It prints the public key. Fund that address with your position size plus
headroom:

```
amount_sol  +  priority fee  +  ~0.002 SOL token-account rent  +  0.00001 base fee
```

For a 0.25 SOL position at 500000 µlam/CU, that is about **0.32 SOL**. Send a
little over, not a lot over — the wallet balance is the only limit that is not
enforced by software you are trusting.

Store the password so systemd can read it, and nobody else:

```bash
sudo install -d -m 700 -o sniper /etc/sniper
printf 'SNIPER_KEYSTORE_PASSWORD=%s\nHELIUS_API_KEY=%s\n' '<password>' '<key>' \
  | sudo tee /etc/sniper/secrets.env > /dev/null
sudo chmod 600 /etc/sniper/secrets.env
sudo chown sniper:sniper /etc/sniper/secrets.env
```

---

## 5. Configure

```bash
cp config.example.toml config.toml && chmod 600 config.toml
$EDITOR config.toml
```

The settings that actually matter on day one:

```toml
[geyser]
backend = "helius_atlas"
endpoint = "wss://atlas-mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}"

[target]
ticker  = "YOURCOIN"
twitter = "https://x.com/theproject"    # do not skip this
require_twitter = true                  # hard gate; safest setting

[buy]
amount_sol = 0.05                       # start small, raise it once you have landed one
slippage_bps = 1500                     # 15% — new launches move fast
priority_fee_microlamports = 500000     # ~0.06 SOL at the default CU limit

[safety]
max_total_spend_sol = 0.1               # hard ceiling on the whole process
max_positions = 1
```

On `require_twitter = true`: it means the bot will not buy unless it fetched the
metadata and the handle matched. That costs you 50–300 ms and it will
occasionally make you miss a launch when IPFS is slow. Take that trade until you
have a reason not to.

Then check it:

```bash
source /etc/sniper/secrets.env && export SNIPER_KEYSTORE_PASSWORD HELIUS_API_KEY
python -m sniper check
```

This reads the pump.fun `global` account off-chain, prints the fee recipient,
the live curve parameters, your balance, and what your configured SOL amount
would actually buy. **If this fails, stop and fix it** — everything downstream
depends on it.

---

## 6. Soak it in dry-run

This is the step people skip and regret.

```bash
python -m sniper run --dry-run --config config.toml
```

Leave it running for at least an hour against the live feed. You are checking
three things:

1. **`candidate_seen` events are flowing.** pump.fun sees a lot of launches; if
   you are seeing none, your subscription is wrong.
2. **The parser is not throwing.** No `listener_disconnected` storms, no
   tracebacks.
3. **Nothing matches that should not.** If your ticker is generic, watch how
   often `candidate_scored` fires and what the Twitter check does with clones.

To watch it in a readable form:

```bash
python -m sniper run --dry-run 2>&1 | jq -r \
  'select(.event=="candidate_scored") | "\(.symbol)\t\(.confidence)\t\(.matched)\t\(.metadata_twitter)"'
```

A safe way to test the whole path end to end, including the buy: temporarily set
`ticker` to something extremely common, and let dry-run fire on it. You will get
a `dry_run_buy` log with a real signed transaction, its size, and the full
timing breakdown — without spending anything. Then set your real ticker back.

---

## 7. Install as a service

```ini
# /etc/systemd/system/sniper.service
[Unit]
Description=pump.fun sniper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sniper
Group=sniper
WorkingDirectory=/opt/sniper/app
EnvironmentFile=/etc/sniper/secrets.env
ExecStart=/opt/sniper/app/.venv/bin/python -m sniper run --config /opt/sniper/app/config.toml
Restart=no
Nice=-10
LimitNOFILE=65535

# Hardening — this process needs the network and its own directory, nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/sniper
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl start sniper
sudo journalctl -u sniper -f
```

**`Restart=no` is deliberate and you should leave it.** A sniper that restarts
itself can buy again after you thought it had stopped. If it died, you want to
know why before it spends more money.

---

## 8. The first live buy

Go live with `amount_sol` at something you genuinely do not care about losing —
0.01–0.05 SOL. The goal of the first live run is **not** profit. It is to
confirm that the buy instruction layout is correct against the current on-chain
program, which is the one thing no test can prove.

After it fires, check the result:

```bash
jq 'select(.event=="buy_result")' logs/sniper.jsonl
```

- `"status":"confirmed"` with a non-null `tokens_received` — the layout is
  correct. You are live.
- `"status":"failed"` with an `err` mentioning account counts or seeds — the
  program layout has moved. Try `layout = "legacy"` in `[buy]`, and see
  "Keeping up with program changes" in the README.

Only raise `amount_sol` after you have seen one confirmed fill.

---

## 9. Operating it

```bash
# Halt immediately — works whether or not the feed is busy
python -m sniper kill --config /opt/sniper/app/config.toml
sudo systemctl stop sniper          # or this
kill -USR1 $(pgrep -f 'sniper run') # stop trading, stay up to report in-flight fills

# Clear the flag before the next run — a leftover KILL file is a hard stop at startup
python -m sniper kill --clear --config /opt/sniper/app/config.toml
```

**Practise the kill switch before you need it.** Run it once against a dry run
so you know it works and how fast it is.

Watching:

```bash
sudo journalctl -u sniper -f | jq -r 'select(.event=="heartbeat")'
jq 'select(.event=="buy_attempt") | .timings_us' logs/sniper.jsonl
```

Rotate or ship `logs/sniper.jsonl` if you leave it running for weeks; it is
already size-capped at 64 MB × 5 files, but it is also your only audit trail.

---

## Checklist

- [ ] VPS in Frankfurt / Amsterdam / Ashburn, clock synced
- [ ] Paid geyser feed, endpoint and key in hand
- [ ] `python -m pytest -q` passes on the box
- [ ] Burner wallet, funded with only what you will risk
- [ ] Keystore is mode 0600; password in `/etc/sniper/secrets.env`, mode 0600
- [ ] `python -m sniper check` succeeds and prints a sane fee recipient
- [ ] `--dry-run` soaked for an hour with candidates flowing
- [ ] Kill switch tested
- [ ] `max_total_spend_sol` set to a number you would shrug at
- [ ] First live run at 0.01–0.05 SOL, `buy_result` confirmed before scaling
