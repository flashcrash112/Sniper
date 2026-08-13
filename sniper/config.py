"""Configuration loading and validation.

One TOML file drives the whole bot. It is read once at startup and frozen —
nothing reloads config mid-run, because a config re-read on the hot path is
exactly the kind of work this bot exists to avoid.

Any string value may reference an environment variable as ``${VAR_NAME}``, so
API keys and keystore passwords stay out of the file itself.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from solders.pubkey import Pubkey

from .constants import LAMPORTS_PER_SOL, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from .pumpfun import BuyLayout

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for anything wrong with the config file, with a fixable message."""


def _expand_env(value: Any, path: str) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            env = os.environ.get(name)
            if env is None:
                raise ConfigError(
                    f"{path} references ${{{name}}} but that environment "
                    f"variable is not set"
                )
            return env

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


class _Section:
    """Typed access to one TOML table with precise error messages."""

    def __init__(self, data: dict, name: str) -> None:
        self._data = data
        self._name = name

    def _get(self, key: str, default: Any, required: bool) -> Any:
        if key in self._data:
            return self._data[key]
        if required:
            raise ConfigError(f"missing required setting [{self._name}] {key}")
        return default

    def str_(self, key: str, default: Optional[str] = None, required: bool = False) -> Any:
        value = self._get(key, default, required)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"[{self._name}] {key} must be a string")
        return value

    def int_(self, key: str, default: Optional[int] = None, required: bool = False) -> Any:
        value = self._get(key, default, required)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ConfigError(f"[{self._name}] {key} must be an integer")
        return value

    def float_(
        self, key: str, default: Optional[float] = None, required: bool = False
    ) -> Any:
        value = self._get(key, default, required)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if value is None:
                return None
            raise ConfigError(f"[{self._name}] {key} must be a number")
        return float(value)

    def bool_(self, key: str, default: bool = False) -> bool:
        value = self._get(key, default, False)
        if not isinstance(value, bool):
            raise ConfigError(f"[{self._name}] {key} must be true or false")
        return value

    def list_str(self, key: str, default: Optional[list[str]] = None) -> list[str]:
        value = self._get(key, default if default is not None else [], False)
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigError(f"[{self._name}] {key} must be a list of strings")
        return list(value)


# --- Sections --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RpcConfig:
    http_url: str
    send_urls: list[str]
    """Endpoints the signed transaction is broadcast to, in parallel.

    More endpoints means more chances one of them has a good path to the
    current leader. The first accepted signature wins; the rest are duplicates
    the cluster dedupes for free.
    """
    commitment: str = "confirmed"
    blockhash_refresh_ms: int = 400
    keepalive_interval_s: float = 15.0


@dataclass(frozen=True, slots=True)
class GeyserConfig:
    backend: str
    """One of `yellowstone`, `helius_atlas`, `logs`."""
    endpoint: str
    x_token: Optional[str] = None
    ping_interval_s: float = 20.0
    reconnect_min_s: float = 0.25
    reconnect_max_s: float = 10.0


@dataclass(frozen=True, slots=True)
class TargetConfig:
    ticker: str
    twitter: Optional[str] = None
    name_contains: Optional[str] = None
    creator: Optional[str] = None
    confidence_threshold: float = 0.7
    require_twitter: bool = False
    prefer_earliest: bool = True
    weight_ticker: float = 0.6
    weight_twitter: float = 0.4
    weight_name: float = 0.1
    weight_creator: float = 0.3
    metadata_timeout_ms: int = 400
    metadata_gateways: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WalletConfig:
    keystore_path: Path
    password_env: str = "SNIPER_KEYSTORE_PASSWORD"


@dataclass(frozen=True, slots=True)
class BuyConfig:
    amount_sol: float
    slippage_bps: int
    priority_fee_microlamports: int
    compute_unit_limit: int = 120_000
    layout: BuyLayout = BuyLayout.CURRENT
    track_volume: bool = False
    token_program: Pubkey = TOKEN_2022_PROGRAM
    """Assumed when the feed does not reveal the mint's token program.

    pump.fun now creates mints under Token-2022, so that is the default."""

    @property
    def amount_lamports(self) -> int:
        return int(round(self.amount_sol * LAMPORTS_PER_SOL))

    @property
    def priority_fee_lamports(self) -> int:
        """What the priority fee actually costs, for the spend cap."""
        return (
            self.priority_fee_microlamports * self.compute_unit_limit + 999_999
        ) // 1_000_000


@dataclass(frozen=True, slots=True)
class JitoConfig:
    """Bundle submission. Off by default — it costs a tip on every attempt."""

    enabled: bool = False
    block_engine_url: str = "https://mainnet.block-engine.jito.wtf"
    tip_lamports: int = 1_000_000
    also_send_rpc: bool = True
    """Broadcast normally as well as bundling.

    Belt and braces: if the bundle is not selected, the transaction can still
    land through the ordinary path. Costs nothing extra — the tip is only
    charged if the bundle lands.
    """


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """Take-profit / stop-loss / max-hold. Off by default: buying and holding
    is a decision, but selling on a schedule you did not think about is not."""

    enabled: bool = False
    take_profit_pct: float = 200.0
    """Sell when the position is worth this % of what it cost. 200 = 2x."""
    stop_loss_pct: float = 50.0
    """Sell when the position falls to this % of what it cost."""
    max_hold_s: float = 300.0
    poll_interval_s: float = 1.0
    slippage_bps: int = 1500
    priority_fee_microlamports: int = 200_000
    compute_unit_limit: int = 100_000


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    kill_switch_file: Path
    max_total_spend_sol: float
    max_positions: int = 1
    confirm_timeout_s: float = 30.0
    dry_run: bool = False
    """Config-level default; `--dry-run` on the command line forces it on."""

    @property
    def max_total_spend_lamports(self) -> int:
        return int(round(self.max_total_spend_sol * LAMPORTS_PER_SOL))


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: Optional[Path] = None
    json: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    rpc: RpcConfig
    geyser: GeyserConfig
    target: TargetConfig
    wallet: WalletConfig
    buy: BuyConfig
    safety: SafetyConfig
    logging: LoggingConfig
    jito: JitoConfig = field(default_factory=JitoConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    source_path: Optional[Path] = None


_VALID_BACKENDS = {"yellowstone", "helius_atlas", "logs"}
_DEFAULT_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
]


def load_config(path: str | Path) -> Config:
    """Read, expand and validate the config file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    raw = _expand_env(raw, path.name)
    return _build(raw, path)


def _build(raw: dict, source: Optional[Path]) -> Config:
    def section(name: str) -> _Section:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table")
        return _Section(value, name)

    rpc_s = section("rpc")
    http_url = rpc_s.str_("http_url", required=True)
    send_urls = rpc_s.list_str("send_urls") or [http_url]
    rpc = RpcConfig(
        http_url=http_url,
        send_urls=send_urls,
        commitment=rpc_s.str_("commitment", "confirmed"),
        blockhash_refresh_ms=rpc_s.int_("blockhash_refresh_ms", 400),
        keepalive_interval_s=rpc_s.float_("keepalive_interval_s", 15.0),
    )

    geyser_s = section("geyser")
    backend = geyser_s.str_("backend", "logs")
    if backend not in _VALID_BACKENDS:
        raise ConfigError(
            f"[geyser] backend must be one of {sorted(_VALID_BACKENDS)}, got {backend!r}"
        )
    geyser = GeyserConfig(
        backend=backend,
        endpoint=geyser_s.str_("endpoint", required=True),
        x_token=geyser_s.str_("x_token"),
        ping_interval_s=geyser_s.float_("ping_interval_s", 20.0),
        reconnect_min_s=geyser_s.float_("reconnect_min_s", 0.25),
        reconnect_max_s=geyser_s.float_("reconnect_max_s", 10.0),
    )

    target_s = section("target")
    ticker = target_s.str_("ticker", required=True).strip()
    if not ticker:
        raise ConfigError("[target] ticker must not be empty")
    threshold = target_s.float_("confidence_threshold", 0.7)
    if not 0.0 < threshold <= 1.0:
        raise ConfigError("[target] confidence_threshold must be in (0, 1]")
    target = TargetConfig(
        ticker=ticker,
        twitter=target_s.str_("twitter"),
        name_contains=target_s.str_("name_contains"),
        creator=target_s.str_("creator"),
        confidence_threshold=threshold,
        require_twitter=target_s.bool_("require_twitter", False),
        prefer_earliest=target_s.bool_("prefer_earliest", True),
        weight_ticker=target_s.float_("weight_ticker", 0.6),
        weight_twitter=target_s.float_("weight_twitter", 0.4),
        weight_name=target_s.float_("weight_name", 0.1),
        weight_creator=target_s.float_("weight_creator", 0.3),
        metadata_timeout_ms=target_s.int_("metadata_timeout_ms", 400),
        metadata_gateways=target_s.list_str("metadata_gateways")
        or list(_DEFAULT_GATEWAYS),
    )
    if target.require_twitter and not target.twitter:
        raise ConfigError(
            "[target] require_twitter is set but no twitter handle was given"
        )

    wallet_s = section("wallet")
    wallet = WalletConfig(
        keystore_path=Path(wallet_s.str_("keystore_path", required=True)).expanduser(),
        password_env=wallet_s.str_("password_env", "SNIPER_KEYSTORE_PASSWORD"),
    )

    buy_s = section("buy")
    amount_sol = buy_s.float_("amount_sol", required=True)
    if amount_sol <= 0:
        raise ConfigError("[buy] amount_sol must be positive")
    slippage_bps = buy_s.int_("slippage_bps", 1000)
    if not 0 <= slippage_bps <= 10_000:
        raise ConfigError("[buy] slippage_bps must be between 0 and 10000")
    token_program_raw = buy_s.str_("token_program", "token2022")
    token_programs = {"token2022": TOKEN_2022_PROGRAM, "token": TOKEN_PROGRAM}
    if token_program_raw not in token_programs:
        raise ConfigError(
            f"[buy] token_program must be one of {sorted(token_programs)}, "
            f"got {token_program_raw!r}"
        )
    layout_raw = buy_s.str_("layout", BuyLayout.CURRENT.value)
    try:
        layout = BuyLayout(layout_raw)
    except ValueError:
        raise ConfigError(
            f"[buy] layout must be one of {[m.value for m in BuyLayout]}, "
            f"got {layout_raw!r}"
        ) from None
    buy = BuyConfig(
        amount_sol=amount_sol,
        slippage_bps=slippage_bps,
        priority_fee_microlamports=buy_s.int_("priority_fee_microlamports", 1_000_000),
        compute_unit_limit=buy_s.int_("compute_unit_limit", 120_000),
        layout=layout,
        track_volume=buy_s.bool_("track_volume", False),
        token_program=token_programs[token_program_raw],
    )

    safety_s = section("safety")
    max_spend = safety_s.float_("max_total_spend_sol", required=True)
    safety = SafetyConfig(
        kill_switch_file=Path(
            safety_s.str_("kill_switch_file", "./KILL")
        ).expanduser(),
        max_total_spend_sol=max_spend,
        max_positions=safety_s.int_("max_positions", 1),
        confirm_timeout_s=safety_s.float_("confirm_timeout_s", 30.0),
        dry_run=safety_s.bool_("dry_run", False),
    )
    if safety.max_positions < 1:
        raise ConfigError("[safety] max_positions must be at least 1")

    # The cap has to cover the position plus the fee that buys it, otherwise a
    # buy that is nominally within budget trips the cap at send time.
    per_buy_cost = buy.amount_lamports + buy.priority_fee_lamports
    if safety.max_total_spend_lamports < per_buy_cost:
        raise ConfigError(
            f"[safety] max_total_spend_sol ({max_spend}) is below the cost of a "
            f"single buy ({per_buy_cost / LAMPORTS_PER_SOL:.6f} SOL including "
            f"the priority fee) — the bot could never fire"
        )

    jito_s = section("jito")
    jito = JitoConfig(
        enabled=jito_s.bool_("enabled", False),
        block_engine_url=jito_s.str_(
            "block_engine_url", "https://mainnet.block-engine.jito.wtf"
        ),
        tip_lamports=jito_s.int_("tip_lamports", 1_000_000),
        also_send_rpc=jito_s.bool_("also_send_rpc", True),
    )
    if jito.enabled and jito.tip_lamports < 1_000:
        raise ConfigError("[jito] tip_lamports must be at least 1000 (Jito's minimum)")

    exit_s = section("exit")
    exit_cfg = ExitConfig(
        enabled=exit_s.bool_("enabled", False),
        take_profit_pct=exit_s.float_("take_profit_pct", 200.0),
        stop_loss_pct=exit_s.float_("stop_loss_pct", 50.0),
        max_hold_s=exit_s.float_("max_hold_s", 300.0),
        poll_interval_s=exit_s.float_("poll_interval_s", 1.0),
        slippage_bps=exit_s.int_("slippage_bps", 1500),
        priority_fee_microlamports=exit_s.int_("priority_fee_microlamports", 200_000),
        compute_unit_limit=exit_s.int_("compute_unit_limit", 100_000),
    )
    if exit_cfg.enabled:
        if exit_cfg.take_profit_pct <= 100:
            raise ConfigError(
                "[exit] take_profit_pct must be above 100 (it is a percentage of "
                "cost, so 200 means 2x)"
            )
        if not 0 < exit_cfg.stop_loss_pct < 100:
            raise ConfigError("[exit] stop_loss_pct must be between 0 and 100")
        if exit_cfg.poll_interval_s < 0.2:
            raise ConfigError("[exit] poll_interval_s below 0.2 will hit RPC rate limits")

    # The Jito tip is spent per attempt, so the cap has to cover it too.
    if jito.enabled:
        per_buy_cost += jito.tip_lamports
        if safety.max_total_spend_lamports < per_buy_cost:
            raise ConfigError(
                f"[safety] max_total_spend_sol ({max_spend}) is below the cost of a "
                f"single buy once the Jito tip is included "
                f"({per_buy_cost / LAMPORTS_PER_SOL:.6f} SOL)"
            )

    log_s = section("logging")
    log_file = log_s.str_("file")
    logging_cfg = LoggingConfig(
        level=log_s.str_("level", "INFO").upper(),
        file=Path(log_file).expanduser() if log_file else None,
        json=log_s.bool_("json", True),
    )

    return Config(
        rpc=rpc,
        geyser=geyser,
        target=target,
        wallet=wallet,
        buy=buy,
        safety=safety,
        logging=logging_cfg,
        jito=jito,
        exit=exit_cfg,
        source_path=source,
    )
