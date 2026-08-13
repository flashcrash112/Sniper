"""CLI and orchestration.

    python -m sniper run       --config config.toml [--dry-run]
    python -m sniper check     --config config.toml
    python -m sniper keystore  create|show
    python -m sniper kill      --config config.toml [--clear]

The event loop deliberately handles candidates *inline* rather than spawning a
task per launch. The pre-filter is two string operations, so the cost of
staying in-line is far below the cost of a task hand-off, and the one case
where latency actually matters — our ticker just appeared — gets the shortest
possible path to `Buyer.execute`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

import aiohttp

from .buyer import Buyer, BuyOutcome
from .config import Config, ConfigError, load_config
from .constants import LAMPORTS_PER_SOL
from .exit import ExitManager, Position
from .jito import JitoClient
from .keystore import (
    KeystoreError,
    create_keystore,
    load_keypair,
    parse_secret_key,
    read_password,
)
from .listener.base import create_listener, probe_listener
from .logging_setup import get_logger, setup_logging, shutdown_logging
from .matcher import Matcher
from .metadata import MetadataFetcher, TokenMetadata
from .pumpfun import LaunchEvent
from .rpc import RpcError, RpcPool
from .verify import format_report, verify_layout
from .safety import KillSwitch, SpendLedger, clear_kill_switch

log = get_logger("sniper")

HEARTBEAT_INTERVAL_S = 60.0


class Sniper:
    """Wires the four components together and runs the event loop."""

    def __init__(self, cfg: Config, dry_run: bool) -> None:
        self.cfg = cfg
        self.dry_run = dry_run or cfg.safety.dry_run
        self.matcher = Matcher(cfg.target)
        self.ledger = SpendLedger(cfg.safety)
        self.kill = KillSwitch(cfg.safety.kill_switch_file)
        self.seen_mints: set[str] = set()
        self.candidates = 0
        self.fired = 0
        self._pending: list[asyncio.Task] = []
        self._started = time.monotonic()
        self.exit_manager: Optional[ExitManager] = None
        self.keypair = None

    async def run(self) -> int:
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        self.kill.install_signal_handlers(loop)
        await self.kill.start()
        if self.kill.engaged:
            log.error("startup_aborted", extra={"reason": self.kill.reason})
            return 1

        password = read_password(cfg.wallet.password_env)
        keypair = load_keypair(cfg.wallet.keystore_path, password)
        del password
        log.info("wallet_loaded", extra={"pubkey": str(keypair.pubkey())})

        async with RpcPool(
            cfg.rpc.http_url, cfg.rpc.send_urls, cfg.rpc.keepalive_interval_s
        ) as rpc:
            await rpc.warm()

            jito_client: Optional[JitoClient] = None
            if cfg.jito.enabled:
                jito_client = JitoClient(cfg.jito.block_engine_url)
                await jito_client.__aenter__()
                await jito_client.warm()

            buyer = Buyer(
                keypair=keypair,
                rpc=rpc,
                buy_cfg=cfg.buy,
                rpc_cfg=cfg.rpc,
                safety_cfg=cfg.safety,
                dry_run=self.dry_run,
                jito=jito_client,
                jito_cfg=cfg.jito if cfg.jito.enabled else None,
            )
            await buyer.prepare()
            await self._check_balance(rpc, buyer)
            self.keypair = keypair

            fetcher: Optional[MetadataFetcher] = None
            if self.matcher.needs_metadata():
                fetcher = MetadataFetcher(
                    cfg.target.metadata_gateways, cfg.target.metadata_timeout_ms
                )
                await fetcher.__aenter__()
                await fetcher.warm()

            log.warning(
                "sniper_armed",
                extra={
                    "dry_run": self.dry_run,
                    "backend": cfg.geyser.backend,
                    "ticker": cfg.target.ticker,
                    "twitter": cfg.target.twitter,
                    "threshold": cfg.target.confidence_threshold,
                    "buy_sol": cfg.buy.amount_sol,
                    "slippage_bps": cfg.buy.slippage_bps,
                    "priority_fee_microlamports": cfg.buy.priority_fee_microlamports,
                    "kill_switch_file": str(cfg.safety.kill_switch_file),
                    "jito": cfg.jito.enabled,
                    "jito_tip_lamports": cfg.jito.tip_lamports if cfg.jito.enabled else None,
                    "exit_enabled": cfg.exit.enabled,
                    **self.ledger.summary(),
                },
            )

            if cfg.exit.enabled:
                self.exit_manager = ExitManager(
                    keypair=keypair,
                    rpc=rpc,
                    blockhash=buyer.blockhash,
                    cfg=cfg.exit,
                    fee_recipient=buyer.fee_recipient,
                    base_curve=buyer.initial_curve,
                    layout=cfg.buy.layout,
                    dry_run=self.dry_run,
                )

            try:
                await self._consume(buyer, fetcher)
            finally:
                await self._drain(buyer)
                if fetcher is not None:
                    await fetcher.__aexit__(None, None, None)
                if jito_client is not None:
                    await jito_client.close()
                await buyer.close()
                await self.kill.stop()

        log.warning(
            "sniper_stopped",
            extra={
                "reason": self.kill.reason or "stream ended",
                "candidates_seen": self.candidates,
                "buys_fired": self.fired,
                **self.ledger.summary(),
            },
        )
        return 0

    async def _check_balance(self, rpc: RpcPool, buyer: Buyer) -> None:
        """Warn early if the wallet cannot cover what it is configured to spend."""
        try:
            balance = await rpc.get_balance(str(buyer.pubkey))
        except Exception as exc:
            log.warning("balance_check_failed", extra={"error": repr(exc)})
            return

        # Position + priority fee + base fee + rent for the token account.
        needed = (
            self.cfg.buy.amount_lamports
            + self.cfg.buy.priority_fee_lamports
            + 5_000
            + 2_040_000
        )
        log.info(
            "wallet_balance",
            extra={
                "sol": round(balance / LAMPORTS_PER_SOL, 6),
                "needed_sol": round(needed / LAMPORTS_PER_SOL, 6),
            },
        )
        if balance < needed and not self.dry_run:
            log.error(
                "insufficient_balance",
                extra={
                    "sol": round(balance / LAMPORTS_PER_SOL, 6),
                    "needed_sol": round(needed / LAMPORTS_PER_SOL, 6),
                    "note": "buy would fail; fund the wallet or lower [buy] amount_sol",
                },
            )

    async def _consume(self, buyer: Buyer, fetcher: Optional[MetadataFetcher]) -> None:
        """Pump the feed until the kill switch fires or trading is done.

        The next event and the shutdown signal are raced against each other.
        Simply iterating the stream would leave the kill switch unable to stop
        a process parked on a quiet feed — exactly the moment you are most
        likely to be reaching for it.
        """
        listener = create_listener(self.cfg.geyser)
        heartbeat = asyncio.create_task(self._heartbeat(), name="heartbeat")
        stream = listener.stream()
        shutdown = asyncio.create_task(
            self.kill.shutdown_requested.wait(), name="shutdown-wait"
        )
        try:
            while True:
                if self.kill.shutdown_requested.is_set() or self._trading_finished():
                    break
                nxt = asyncio.create_task(stream.__anext__(), name="next-event")
                done, _ = await asyncio.wait(
                    {nxt, shutdown}, return_when=asyncio.FIRST_COMPLETED
                )
                if nxt not in done:
                    # Shutdown won the race. Await the cancellation so the
                    # generator finishes unwinding before we close it below.
                    nxt.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await nxt
                    break
                try:
                    event = nxt.result()
                except StopAsyncIteration:
                    break
                # SIGUSR1 engages the kill switch without requesting shutdown:
                # keep observing and logging, but never buy again.
                await self._handle(event, buyer, fetcher)
        finally:
            shutdown.cancel()
            heartbeat.cancel()
            listener.stop()
            await stream.aclose()

    def _trading_finished(self) -> bool:
        return self.ledger.positions >= self.cfg.safety.max_positions

    async def _handle(
        self, event: LaunchEvent, buyer: Buyer, fetcher: Optional[MetadataFetcher]
    ) -> None:
        mint = str(event.mint)
        if mint in self.seen_mints:
            return
        self.seen_mints.add(mint)
        self.candidates += 1

        # Cheap gate first: everything below this line only runs for a ticker
        # we actually care about, which on a live feed is a handful of events
        # out of thousands.
        if not self.matcher.ticker_matches(event):
            log.debug("candidate_seen", extra=event.summary())
            return

        metadata: Optional[TokenMetadata] = None
        if fetcher is not None:
            metadata = await fetcher.fetch(event.uri)

        result = self.matcher.score(event, metadata)
        log.info(
            "candidate_scored",
            extra={
                **event.summary(),
                **result.to_log(),
                "metadata_fetch_ms": metadata.fetch_ms if metadata else None,
                "metadata_twitter": metadata.twitter if metadata else None,
            },
        )

        if not result.matched:
            return

        if self.kill.engaged:
            log.warning("buy_suppressed", extra={"mint": mint, "reason": "kill switch"})
            return

        cost = self.cfg.buy.amount_lamports + self.cfg.buy.priority_fee_lamports
        reservation = self.ledger.reserve(cost)
        if reservation is None:
            log.warning(
                "buy_suppressed",
                extra={
                    "mint": mint,
                    "reason": "spend cap or position limit reached",
                    **self.ledger.summary(),
                },
            )
            return

        outcome = await buyer.execute(event)
        self.fired += 1

        # The reservation is only given back if nothing went out. A dry run
        # keeps it so the rehearsal stops at the same point a live run would —
        # otherwise `--dry-run` would happily "buy" every clone in the wave and
        # tell you nothing about how the real run behaves.
        if not outcome.dry_run and not outcome.sent:
            self.ledger.release(reservation)

        log.warning(
            "buy_attempt",
            extra={**outcome.to_log(), **result.to_log(), **self.ledger.summary()},
        )

        if outcome.sent:
            self._pending.append(
                asyncio.create_task(self._track(buyer, outcome), name=f"confirm-{mint}")
            )

    async def _track(self, buyer: Buyer, outcome: BuyOutcome) -> None:
        """Confirm the buy, then hand the position to the exit manager."""
        try:
            report = await buyer.confirm(outcome)
        except Exception as exc:
            log.error(
                "confirm_failed",
                extra={"signature": outcome.signature, "error": repr(exc)},
            )
            return
        level = log.warning if report.get("status") == "confirmed" else log.error
        level("buy_result", extra={**report, "mint": str(outcome.event.mint)})

        if report.get("status") != "confirmed":
            return
        if self.exit_manager is None:
            return

        filled = report.get("tokens_received")
        if not filled:
            log.warning(
                "exit_skipped",
                extra={
                    "mint": str(outcome.event.mint),
                    "reason": "confirmed but no token balance to sell",
                },
            )
            return

        position = Position(
            mint=outcome.event.mint,
            creator=outcome.event.creator,
            associated_user=outcome.associated_user,
            token_amount=filled,
            # What the position actually cost, not what it was quoted at.
            cost_lamports=outcome.quote.expected_sol_cost,
            opened_at=time.monotonic(),
        )
        try:
            result = await self.exit_manager.monitor(position)
        except Exception as exc:
            log.error(
                "exit_failed",
                extra={"mint": str(outcome.event.mint), "error": repr(exc)},
            )
            return
        log.warning(
            "position_closed",
            extra={"mint": str(outcome.event.mint), **result.to_log()},
        )

    async def _drain(self, buyer: Buyer) -> None:
        """Let in-flight confirmations finish before shutting down."""
        pending = [t for t in self._pending if not t.done()]
        if not pending:
            return
        # With exits enabled a tracking task also holds a position open, so the
        # drain has to outlast max_hold_s or shutdown would abandon a live
        # position rather than selling it.
        timeout = self.cfg.safety.confirm_timeout_s + 5
        if self.cfg.exit.enabled:
            timeout += self.cfg.exit.max_hold_s

        log.info(
            "awaiting_confirmations",
            extra={"count": len(pending), "timeout_s": round(timeout)},
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.warning("confirmation_drain_timeout")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            log.info(
                "heartbeat",
                extra={
                    "uptime_s": round(time.monotonic() - self._started),
                    "candidates_seen": self.candidates,
                    "buys_fired": self.fired,
                    **self.ledger.summary(),
                },
            )


# --- Commands --------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    try:
        return asyncio.run(Sniper(cfg, dry_run=args.dry_run).run())
    except KeyboardInterrupt:
        return 130
    finally:
        shutdown_logging()


def _redact(url: str) -> str:
    """Hide the API key so `check` output can be pasted into an issue or a chat."""
    return re.sub(r"(api[-_]?key=)[^&\s]+", r"\1***", url, flags=re.IGNORECASE)


def cmd_check(args: argparse.Namespace) -> int:
    """Validate config and connectivity without arming anything."""
    cfg = load_config(args.config)
    setup_logging(cfg.logging)

    async def check() -> int:
        password = read_password(cfg.wallet.password_env)
        keypair = load_keypair(cfg.wallet.keystore_path, password)
        del password
        print(f"wallet          {keypair.pubkey()}")

        async with RpcPool(cfg.rpc.http_url, cfg.rpc.send_urls) as rpc:
            balance = await rpc.get_balance(str(keypair.pubkey()))
            print(f"balance         {balance / LAMPORTS_PER_SOL:.6f} SOL")

            buyer = Buyer(keypair, rpc, cfg.buy, cfg.rpc, cfg.safety, dry_run=True)
            await buyer.prepare()
            print(f"fee recipient   {buyer.fee_recipient}")
            curve = buyer.initial_curve
            print(
                f"curve           vsol={curve.virtual_sol_reserves / LAMPORTS_PER_SOL:.2f} "
                f"vtok={curve.virtual_token_reserves} fee={curve.fee_basis_points}bps"
            )

            quote = buyer.quote(
                LaunchEvent(
                    mint=keypair.pubkey(),
                    name="check",
                    symbol=cfg.target.ticker,
                    uri="",
                    creator=keypair.pubkey(),
                )
            )
            print(
                f"quote           {cfg.buy.amount_sol} SOL -> "
                f"{quote.token_amount / 1_000_000:,.2f} tokens "
                f"(max cost {quote.max_sol_cost / LAMPORTS_PER_SOL:.6f} SOL)"
            )
            print(
                f"priority fee    {cfg.buy.priority_fee_lamports / LAMPORTS_PER_SOL:.6f} SOL "
                f"({cfg.buy.priority_fee_microlamports} µlam/CU x {cfg.buy.compute_unit_limit} CU)"
            )
            await buyer.close()

        print(f"\ngeyser backend  {cfg.geyser.backend}")
        print(f"endpoint        {_redact(cfg.geyser.endpoint)}")
        if args.skip_feed:
            print("feed            (skipped)")
        else:
            print(
                f"probing the feed for up to {args.probe_seconds}s "
                f"(this is what tells you whether your plan supports it)..."
            )
            probe = await probe_listener(cfg.geyser, timeout_s=args.probe_seconds)
            print(f"feed            {'OK' if probe.ok else 'PROBLEM'} — {probe.explain()}")

        matcher = Matcher(cfg.target)
        print(f"\ntarget ticker   {cfg.target.ticker}")
        print(f"target twitter  {cfg.target.twitter or '(none)'}")
        print(f"metadata fetch  {'required' if matcher.needs_metadata() else 'not needed'}")
        print(f"threshold       {cfg.target.confidence_threshold}")
        print(f"spend cap       {cfg.safety.max_total_spend_sol} SOL")
        print(f"kill switch     {cfg.safety.kill_switch_file}")
        print("\nOK — config is valid and the endpoints answered.")
        return 0

    try:
        return asyncio.run(check())
    finally:
        shutdown_logging()


def cmd_verify_layout(args: argparse.Namespace) -> int:
    """Check our encoded buy against real, successful buys from mainnet."""
    cfg = load_config(args.config)
    setup_logging(cfg.logging)

    async def verify() -> int:
        async with RpcPool(cfg.rpc.http_url, cfg.rpc.send_urls) as rpc:
            print(
                f"Fetching recent successful pump.fun buys to compare against "
                f"the {cfg.buy.layout.value!r} layout...\n"
            )
            reports = await verify_layout(rpc, cfg.buy.layout, samples=args.samples)

        for index, report in enumerate(reports, 1):
            print(f"Sample {index}/{len(reports)}")
            print(format_report(report))
            print()

        if not reports:
            print("No comparable transactions found.")
            return 1

        good = [r for r in reports if r.ok]
        if len(good) == len(reports):
            print(
                f"PASS — all {len(reports)} samples match the {cfg.buy.layout.value!r} "
                f"layout this bot encodes."
            )
            return 0

        print(
            f"FAIL — {len(reports) - len(good)} of {len(reports)} samples disagree "
            f"with what we encode.\n"
            f"Do not run live until this is resolved. See 'Keeping up with program "
            f"changes' in the README."
        )
        return 1

    try:
        return asyncio.run(verify())
    finally:
        shutdown_logging()


def cmd_keystore_create(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if args.generate:
        from solders.keypair import Keypair

        secret = bytes(Keypair())
        print("Generated a new keypair.")
    else:
        source = (
            Path(args.import_file).expanduser().read_text()
            if args.import_file
            else input("Paste the secret key (base58 or JSON array): ")
        )
        secret = parse_secret_key(source)

    password = read_password(args.password_env, "New keystore password: ")
    if not args.password_env_set:
        confirm = read_password("__never_set__", "Confirm password: ")
        if confirm != password:
            print("Passwords do not match.", file=sys.stderr)
            return 1

    keypair = create_keystore(path, secret, password, overwrite=args.force)
    print(f"Keystore written to {path} (mode 0600)")
    print(f"Public key: {keypair.pubkey()}")
    if args.generate:
        print("\nFund this address before running the sniper.")
    return 0


def cmd_keystore_show(args: argparse.Namespace) -> int:
    import json

    path = Path(args.path).expanduser()
    document = json.loads(path.read_text())
    print(f"path    {path}")
    print(f"pubkey  {document.get('pubkey')}")
    print(f"kdf     {document.get('kdf')} {document.get('kdf_params')}")
    print(f"cipher  {document.get('cipher')}")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    flag = cfg.safety.kill_switch_file
    if args.clear:
        removed = clear_kill_switch(flag)
        print(f"{'Removed' if removed else 'No'} kill switch file at {flag}")
        return 0
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    print(f"Kill switch engaged: {flag}")
    print("A running sniper will halt within 200 ms.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sniper", description="pump.fun launch sniper"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config", "-c", default="config.toml", help="path to the TOML config"
        )

    run = sub.add_parser("run", help="arm the sniper")
    add_config(run)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build and sign, log what would be bought, send nothing",
    )
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="validate config and connectivity")
    add_config(check)
    check.add_argument(
        "--probe-seconds",
        type=float,
        default=20.0,
        help="how long to listen for live launches when probing the feed",
    )
    check.add_argument(
        "--skip-feed", action="store_true", help="skip the geyser feed probe"
    )
    check.set_defaults(func=cmd_check)

    verify = sub.add_parser(
        "verify-layout",
        help="compare our encoded buy against real mainnet buys (read-only)",
    )
    add_config(verify)
    verify.add_argument(
        "--samples",
        type=int,
        default=3,
        help="how many recent buys to check against (default 3)",
    )
    verify.set_defaults(func=cmd_verify_layout)

    kill = sub.add_parser("kill", help="engage or clear the kill switch")
    add_config(kill)
    kill.add_argument("--clear", action="store_true", help="remove the flag file")
    kill.set_defaults(func=cmd_kill)

    keystore = sub.add_parser("keystore", help="manage the encrypted keystore")
    ks_sub = keystore.add_subparsers(dest="keystore_command", required=True)

    ks_create = ks_sub.add_parser("create", help="encrypt a secret key to disk")
    ks_create.add_argument("path", help="output keystore path")
    ks_create.add_argument(
        "--generate", action="store_true", help="generate a fresh keypair"
    )
    ks_create.add_argument(
        "--import-file", help="read the secret key from this file instead of stdin"
    )
    ks_create.add_argument(
        "--password-env",
        default="SNIPER_KEYSTORE_PASSWORD",
        help="environment variable holding the password (prompts if unset)",
    )
    ks_create.add_argument("--force", action="store_true", help="overwrite an existing file")
    ks_create.set_defaults(func=cmd_keystore_create)

    ks_show = ks_sub.add_parser("show", help="print keystore metadata (no secrets)")
    ks_show.add_argument("path")
    ks_show.set_defaults(func=cmd_keystore_show)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Whether the password came from the environment decides if we need a
    # confirmation prompt when creating a keystore.
    args.password_env_set = bool(os.environ.get(getattr(args, "password_env", "") or ""))
    try:
        return args.func(args)
    except (ConfigError, KeystoreError, RpcError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except aiohttp.ClientError as exc:
        print(f"error: could not reach the RPC endpoint: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
