# pump.fun sniper

Detects a new pump.fun token launch on a push-based geyser feed, decides
whether it is the coin you are waiting for, and sends a pre-configured buy —
targeting the next block.

**Read [Ticker-only matches are dangerous](#ticker-only-matches-are-dangerous)
before you point this at a live wallet.** It is the part of this README that
will actually cost you money if you skip it.

---

## What it does

Four components, one process:

| Component | File | Job |
|---|---|---|
| **Listener** | `sniper/listener/` | Subscribes to a geyser feed and parses pump.fun create events off the wire |
| **Matcher** | `sniper/matcher.py` | Scores each candidate against the target; only fires above a confidence threshold |
| **Buyer** | `sniper/buyer.py` | Builds, signs and broadcasts the buy. Every decision it can make ahead of time, it already made |
| **Safety** | `sniper/safety.py` | Kill switch, spend cap, position limit, structured audit log |

Plus three things that are off by default and opt-in:

| | File | Job |
|---|---|---|
| **Layout verifier** | `sniper/verify.py` | Checks our encoded buy against real mainnet buys, before you spend anything |
| **Jito bundles** | `sniper/jito.py` | Tips a validator directly instead of relying on the priority-fee auction |
| **Exits** | `sniper/exit.py` | Take-profit / stop-loss / max-hold, priced off the live bonding curve |

Nothing is decided at buy time. The wallet, position size, slippage and
priority fee are fixed in config before the process starts; the only inputs at
match time are the mint and its creator.

---

## Install

**New to this?** Start with **[WINDOWS.md](WINDOWS.md)** if you are on Windows —
it walks through the whole thing on your own laptop, free, with no server. You
do not need to rent anything to get the bot running and watching live launches.

Python 3.11+ on a Linux VPS. Put the box near a validator — Frankfurt, Amsterdam
and Ashburn are the usual choices. Network distance to the leader dominates
everything this code does.

```bash
git clone <your-fork> sniper && cd sniper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create the encrypted keystore (never a plaintext key on disk):

```bash
# Generate a fresh wallet...
python -m sniper keystore create ./keystore.json --generate

# ...or import an existing one (base58 from Phantom, or a solana-keygen JSON array)
python -m sniper keystore create ./keystore.json
```

The file is written mode `0600` and holds the secret key under AES-256-GCM with
an scrypt-derived key (n=2¹⁶). It is decrypted once, at startup. The bot
refuses to load a keystore that is readable by other users.

Then configure and verify:

```bash
cp config.example.toml config.toml && chmod 600 config.toml
$EDITOR config.toml

export HELIUS_API_KEY=...
export SNIPER_KEYSTORE_PASSWORD=...

python -m sniper check          # validates config, endpoints, quote, balance
python -m sniper verify-layout  # checks our buy encoding against real mainnet buys
python -m sniper run --dry-run  # watch it work without spending anything
python -m sniper run            # live
```

Any string in the config may reference an environment variable as
`${VAR_NAME}`, so API keys and passwords stay out of the file.

---

## Choosing a feed

Set `[geyser] backend`:

| Backend | Transport | Latency | Setup |
|---|---|---|---|
| `yellowstone` | Yellowstone gRPC | Lowest | Run `./scripts/gen_proto.sh` once |
| `helius_atlas` | Helius Atlas websocket | Low | None — just the endpoint |
| `logs` | Standard `logsSubscribe` | Highest | Works against any RPC |

Run `yellowstone` in production. `helius_atlas` is the same geyser data over a
websocket and is a reasonable production choice if you would rather not deal
with protobuf codegen. `logs` goes through the node's generic pubsub fan-out and
is the first thing to fall behind under load — use it to develop against.

The gRPC stubs are generated rather than vendored because they have to match
your provider's plugin version:

```bash
pip install 'grpcio-tools>=1.60'
./scripts/gen_proto.sh                    # or: YELLOWSTONE_REF=v6.1.0 ./scripts/gen_proto.sh
```

All three backends reconnect on their own with jittered exponential backoff. A
dropped socket produces a gap in events, never a stopped bot.

---

## Verifying the buy layout before you spend anything

pump.fun is upgradeable and its `buy` account list has grown several times. The
usual way to discover it has moved again is a failed transaction — you pay the
priority fee and lose the launch.

Instead:

```bash
python -m sniper verify-layout
```

This pulls recent **successful** pump.fun buys off mainnet — ones other people
paid for — decodes them, and checks them position by position against what this
bot would encode. Two read-only RPC calls, nothing signed, nothing spent.

```
  #  role                         status    account
 --  ---------------------------- --------- --------------------------------------------
  0  global                       ok        4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
  3  bonding_curve                ok        7tyWxaNjfzasEtst6pkeV5NYRPGDt7yh89ktWtNzcPhC
  9  creator_vault                ?         A3EPUWUo6XwnUtCcmTWojzeSBXCrkrvEggpCWD9tRWib
 13  user_volume_accumulator      ok        EZfdFRVwY4dg1qjrrqwnbcggPQAbftAQMoE3iQPBHyTV
...
PASS — all 3 samples match the 'current' layout this bot encodes.
```

`ok` means we re-derived that account independently (PDA, ATA or program ID)
and it landed where we put it. `?` means it is not derivable — the fee
recipient, the mint, the buyer — so it is reported but not judged. A
`MISMATCH`, an unexpected account count, or a layout that disagrees with your
config all fail the check and print what was expected.

**Run this before every live session.** It is the cheapest possible answer to
"is the on-chain program still what this code thinks it is".

---

## Ticker-only matches are dangerous

**A ticker does not identify a coin.** Anyone can deploy `$FOO` on pump.fun,
and within seconds of a real launch there will be a dozen clones with the same
symbol, the same name and a copied image. A bot matching on ticker alone will
buy whichever one it saw first — which is very often a clone whose deployer is
watching for exactly this behaviour, because sniper bots are the intended
liquidity.

Also worth knowing before you rely on it:

- **The Twitter link is self-reported.** It lives in the creator's own metadata
  document. A clone can put the real project's Twitter URL in it. Matching the
  link proves the deployer *claimed* that handle, not that they own it.
- **Metadata is off-chain**, so verifying that link costs an HTTP round trip
  mid-decision (see [Latency](#latency)).
- **The strongest signal is `creator`** — the deployer's pubkey. If you know it
  in advance, set it. It cannot be faked.

How the signals combine: confidence is the weighted mean over whichever
criteria you configured, and the buy fires at or above
`confidence_threshold`.

| Configured | Ticker matches | Twitter matches | Confidence | Fires at 0.7? |
|---|---|---|---|---|
| ticker only | ✅ | — | **1.00** | **yes** |
| ticker only | ❌ | — | 0.00 | no |
| ticker + twitter | ✅ | ✅ | 1.00 | yes |
| ticker + twitter | ✅ | ❌ | 0.60 | no |
| ticker + twitter | ✅ | *fetch timed out* | 0.60 | no |

Two deliberate choices in that table:

1. **An unverified Twitter scores zero, not "neutral."** If a gateway is slow
   and we could not check the link, the candidate is treated as unverified.
   Scoring it neutral would mean every clone sails through whenever IPFS is
   having a bad minute.
2. **`ticker only` scores 1.00 and fires.** That is the configuration you should
   not run against a wallet you care about. Set `twitter`, or set `creator`, or
   set `require_twitter = true` for a hard gate that ignores the arithmetic
   entirely.

`prefer_earliest` (on by default) halves the confidence of every sighting of a
ticker after the first, which is what makes the bot take the original launch
and ignore the clone wave behind it.

---

## Latency

Measured on this codebase, 2000 iterations, from launch event in hand to
request body ready for the socket:

```
quote            median    6.1 µs    p95     9.1 µs
build + sign     median  173.5 µs    p95   220.0 µs
serialize        median    5.5 µs    p95     7.5 µs
─────────────────────────────────────────────────────
TOTAL            median  186.9 µs    p95   233.6 µs
```

That is ~0.2 ms of CPU inside a budget measured in hundreds of milliseconds, so
the wins are all in what *doesn't* happen on the hot path:

- **The blockhash is already cached** — refreshed on a background timer, saving
  a 20-100 ms `getLatestBlockhash` round trip.
- **Connections are pre-warmed and kept alive** — a cold TLS handshake to a
  send endpoint is two round trips, which would dwarf everything else here.
- **The fee recipient and curve parameters are read once at startup** from the
  on-chain `global` account, not per launch.
- **The `sendTransaction` body is string-concatenated**, not `json.dumps`-ed —
  only the base64 payload varies.
- **Logging is on a queue** with formatting and disk I/O on a background
  thread, so a slow disk cannot stall a buy.
- **The wallet's volume-accumulator PDA is derived at startup**; only the three
  mint- and creator-specific PDAs are derived per launch.
- **The broadcast is raced across every `send_urls` endpoint** and the first
  acceptance wins. Losers keep going — a duplicate broadcast is free and gives
  the transaction another path to the leader.
- **Candidates are handled inline**, not in a spawned task. The pre-filter is
  two string operations, cheaper than a task hand-off.

Every buy logs its own `timings_us` breakdown, including `since_event_us` — the
full pipeline from the frame arriving to the transaction going out. The numbers
above are reproducible, not aspirational.

### The one unavoidable cost

The ticker and name arrive on-chain inside the create event. **The Twitter link
does not** — it is in a JSON document on IPFS, behind the `uri` field. Matching
on it means an HTTP fetch in the middle of the decision, typically 50-300 ms.

That is bounded by `metadata_timeout_ms` (default 400), the gateways are
pre-warmed at startup, and multiple gateways are raced with the first response
winning. But it is real, and it is a genuine trade-off:

- **`twitter` set** → safer matching, ~100-300 ms slower.
- **`twitter` unset** → the whole decision stays on-chain and no HTTP happens
  at all, but see the section above about what a ticker actually proves.

If you know the deployer's pubkey, `creator` gives you a stronger signal than
Twitter for zero latency, because it is in the create event.

---

## Safety

**Dry run.** `--dry-run` builds and signs a real transaction, logs exactly what
would be bought, and sends nothing. It also *takes the position in the ledger*,
so the run stops at the same point a live run would — a rehearsal, not a
different program.

**Kill switch.** Two independent triggers:

```bash
python -m sniper kill              # touch the flag file; halts within 200 ms
python -m sniper kill --clear      # remove it
kill -TERM <pid>                   # stop trading and exit
kill -USR1 <pid>                   # stop trading, stay up to report in-flight fills
```

It halts a *quiet* feed just as fast as a busy one — the event loop races the
next event against the shutdown signal rather than parking on the stream. Once
engaged it never disengages; restart to resume. A leftover flag file at startup
is a hard stop, on the assumption that someone put it there for a reason.

**Spend cap.** `max_total_spend_sol` is a hard ceiling on everything the process
can ever spend, priority fees included. `max_positions = 1` means it buys once
and stops. Lamports are reserved *before* the transaction is broadcast and only
returned if the send definitively failed — a broadcast transaction might still
land, so its spend stays counted. Startup fails if the cap cannot cover a single
buy, rather than leaving you with a bot that can never fire.

**Structured logs.** One JSON object per line, to stdout and to
`[logging] file`:

| Event | When |
|---|---|
| `candidate_seen` | every launch on the feed (DEBUG) |
| `candidate_scored` | ticker matched — full signal breakdown and score |
| `buy_attempt` | quote, signature, endpoint, per-stage timings |
| `buy_result` | confirmation status, slot, **tokens actually received** |
| `heartbeat` | every 60 s: candidates seen, buys fired, spend used |

The fill is read from the token account balance after confirmation, so the log
records what was received rather than what was hoped for.

---

## Landing the transaction: Jito bundles

Priority fees put you in the same auction as everyone else. A Jito bundle pays
a validator directly and reaches the leader over Jito's own path, which on a
contested launch moves your fill rate more than anything in this codebase.

```toml
[jito]
enabled = true
block_engine_url = "https://frankfurt.mainnet.block-engine.jito.wtf"
tip_lamports = 1000000
also_send_rpc = true
```

The tip is a SOL transfer appended to **the same transaction** as the buy, so
if the bundle is not selected, the tip is not paid. `also_send_rpc` broadcasts
normally at the same time, so a bundle that loses still has the ordinary path
to fall back on; the two go out concurrently, not in sequence.

Costs, plainly: the tip is spent on every attempt that lands, exactly like a
priority fee, and it counts against `max_total_spend_sol`. Pick the block
engine nearest your VPS.

---

## Exits

Off by default. The bot's job as configured is to buy; selling on a schedule
you have not thought about is its own way to lose money. When you do want it:

```toml
[exit]
enabled = true
take_profit_pct = 200.0   # sell at 2x cost
stop_loss_pct = 50.0      # sell if it halves
max_hold_s = 300.0        # sell after 5 minutes regardless
```

Once a buy confirms, the position is monitored by polling its bonding curve.
Value is what the curve would **actually pay right now**, net of fees and
including the price impact of selling the whole position — not a quoted index
price. Triggers are checked stop-loss first, then take-profit, then max-hold.

Two behaviours worth knowing:

- The sell is re-quoted against the token balance the wallet actually holds,
  not the amount the buy quoted. Selling tokens you do not have reverts the
  whole transaction.
- If the curve **completes and migrates** to a DEX, pump.fun sells revert. The
  monitor detects this, logs it and stops rather than retrying against an exit
  that no longer exists. Getting out from there is a manual job on the DEX.

---

## Running it as a service

```ini
# /etc/systemd/system/sniper.service
[Unit]
Description=pump.fun sniper
After=network-online.target

[Service]
Type=simple
User=sniper
WorkingDirectory=/opt/sniper
Environment=HELIUS_API_KEY=...
EnvironmentFile=/etc/sniper/secrets.env   # SNIPER_KEYSTORE_PASSWORD, mode 0600
ExecStart=/opt/sniper/.venv/bin/python -m sniper run --config /opt/sniper/config.toml
Restart=no
Nice=-10

[Install]
WantedBy=multi-user.target
```

`Restart=no` is deliberate. A sniper that restarts itself after a crash is a
sniper that can buy again after you thought it had stopped. Decide manually.

Run it as a dedicated non-root user, keep `keystore.json` and the secrets file
mode `0600`, and fund the wallet with only what you intend to risk — the spend
cap is a software guarantee, and the wallet balance is a real one.

---

## Keeping up with program changes

pump.fun is an upgradeable program and its `buy` instruction has gained
accounts several times (`creator_vault`, then the volume accumulators, then the
fee config). This bot encodes the current 16-account layout.

If buys start failing with `AccountNotEnoughKeys`, `ConstraintSeeds` or
`InstructionDidNotDeserialize`, the layout moved. Try `layout = "legacy"` in
`[buy]` for the older 12-account form; if neither works, the account list is in
`build_buy_instruction` in `sniper/pumpfun.py`, and the discriminators and PDA
seeds it depends on are all in `sniper/constants.py`.

`python -m sniper check` prints the fee recipient and curve parameters it read
from the chain, which is the fastest way to confirm your view of the program is
still current.

---

## Deploying

See **[DEPLOY.md](DEPLOY.md)** for the full runbook: choosing a VPS and region,
getting a geyser feed, creating the burner wallet, the systemd unit, and the
dry-run soak that should happen before your first live buy.

---

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest
```

159 tests, ~7 s. Covers bonding-curve math and slippage behaviour (including
that a buy survives a small frontrun and correctly reverts on a large one),
event parsing against synthetic borsh payloads, matcher scoring and Twitter URL
normalisation, keystore round-trips and tamper detection, config validation, and
transaction structure.

`tests/test_integration.py` runs the whole bot end to end against a stubbed RPC
and a stubbed geyser feed: it asserts that a create event on the wire produces a
validly-signed pump.fun buy for the announced mint, that `--dry-run` sends
nothing, that duplicate mints are only acted on once, and that the kill switch
stops a quiet feed promptly.

---

## Limitations

- **Not verified against mainnet.** The transaction structure is asserted in
  tests and the discriminators and PDAs are checked against known mainnet
  values, but no buy from this code has been landed on-chain. Run
  `verify-layout` (which does compare against real mainnet transactions), then
  `--dry-run`, then a live run with a small `amount_sol`, and read the
  `buy_result` log before trusting it with size.
- **Landing in the next block is not guaranteed.** Priority fee, VPS location
  and RPC quality decide that, and you are competing with people running
  colocated infrastructure and staked connections. This code removes the
  software latency; it cannot remove the network.
- **Exits are best-effort.** `[exit]` handles take-profit, stop-loss and
  max-hold against the bonding curve, but it cannot save you from a curve that
  migrates to a DEX (it stops and says so) or from there being no liquidity to
  sell into. It is not a guaranteed stop.
- **Twitter matching proves a claim, not ownership** — see above.
