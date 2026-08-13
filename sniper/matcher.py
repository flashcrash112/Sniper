"""Candidate scoring.

Tickers are not unique on pump.fun. The moment a real launch is spotted,
clones of it appear with the same symbol, so "the symbol matched" is not
evidence of much on its own. This module turns each candidate into a
confidence score from the signals actually available, and the bot only fires
above `[target] confidence_threshold`.

Signals, each contributing its configured weight:

===============  ==========================================================
ticker           Required. A candidate whose symbol does not match is
                 rejected outright, before any scoring.
twitter          The socials link in the off-chain metadata. Scores 0 if it
                 does not match *or could not be fetched* — an unverified
                 link is not a verified one.
name             Substring of the coin name.
creator          Exact deployer pubkey. The strongest signal available if
                 you know the address in advance.
===============  ==========================================================

Confidence is the weighted mean over the signals you configured, so a
ticker-only config scores a matching ticker at 1.0. That is intentional and it
is also the dangerous case — see the README.

`prefer_earliest` then applies a multiplicative penalty to every sighting of a
ticker after the first, which is what makes the bot ignore the clone wave that
follows a real launch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from .config import TargetConfig
from .metadata import TokenMetadata
from .pumpfun import LaunchEvent

# Applied to the confidence of any candidate that is not the first sighting of
# its ticker in this run. Halving is enough to drop a ticker-only match (1.0)
# below the default 0.7 threshold.
LATE_SIGHTING_PENALTY = 0.5

_TWITTER_HOSTS = {"twitter.com", "x.com", "www.twitter.com", "www.x.com", "mobile.twitter.com"}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_NON_HANDLE_PATHS = {"i", "intent", "home", "search", "hashtag", "share"}


@dataclass(frozen=True, slots=True)
class TwitterRef:
    """A normalised Twitter/X reference, comparable across URL spellings."""

    handle: Optional[str] = None
    path: Optional[str] = None
    """Normalised path for non-profile links (communities, statuses)."""

    def __bool__(self) -> bool:
        return bool(self.handle or self.path)


def normalize_twitter(value: Optional[str]) -> TwitterRef:
    """Reduce any way of writing a Twitter link to something comparable.

    Handles bare handles (`foo`, `@foo`), profile URLs, status URLs and
    community links, with or without scheme, host prefix or query string.
    """
    if not value:
        return TwitterRef()
    value = value.strip()
    if not value:
        return TwitterRef()

    if value.startswith("@"):
        candidate = value[1:]
        return TwitterRef(handle=candidate.lower()) if _HANDLE_RE.match(candidate) else TwitterRef()

    if "/" not in value and _HANDLE_RE.match(value):
        return TwitterRef(handle=value.lower())

    parsed = urlparse(value if "//" in value else f"https://{value}")
    if parsed.netloc.lower() not in _TWITTER_HOSTS:
        return TwitterRef()

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return TwitterRef()

    first = segments[0]
    if first.lower() in _NON_HANDLE_PATHS:
        # e.g. /i/communities/1234 — no handle, but the path itself identifies
        # the target and is worth comparing.
        return TwitterRef(path="/".join(s.lower() for s in segments))
    if not _HANDLE_RE.match(first):
        return TwitterRef()

    handle = first.lower()
    if len(segments) >= 3 and segments[1].lower() == "status":
        return TwitterRef(handle=handle, path=f"{handle}/status/{segments[2]}")
    return TwitterRef(handle=handle)


def normalize_ticker(value: Optional[str]) -> str:
    """Case-fold a ticker and drop the `$` prefix people write inconsistently."""
    if not value:
        return ""
    return value.strip().lstrip("$").strip().upper()


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    weight: float
    score: float
    detail: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The full decision record for one candidate."""

    matched: bool
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    reason: str = ""
    late_sighting: bool = False
    metadata: Optional[TokenMetadata] = None

    def to_log(self) -> dict:
        return {
            "matched": self.matched,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "late_sighting": self.late_sighting,
            "signals": {
                s.name: {"score": s.score, "weight": s.weight, "detail": s.detail}
                for s in self.signals
            },
        }


class Matcher:
    """Scores launches against the configured target.

    Stateful in one respect: it remembers which tickers it has already seen so
    `prefer_earliest` can penalise clones.
    """

    def __init__(self, cfg: TargetConfig) -> None:
        self.cfg = cfg
        self._target_ticker = normalize_ticker(cfg.ticker)
        self._target_twitter = normalize_twitter(cfg.twitter)
        self._target_name = (cfg.name_contains or "").strip().lower() or None
        self._target_creator = (cfg.creator or "").strip() or None
        self._seen_tickers: set[str] = set()

        if cfg.twitter and not self._target_twitter:
            raise ValueError(
                f"[target] twitter is not a recognisable Twitter/X reference: "
                f"{cfg.twitter!r}"
            )

    def needs_metadata(self) -> bool:
        """Whether a metadata fetch can change the outcome.

        If it cannot, the whole decision stays on-chain and the hot path never
        touches the network before sending.
        """
        return bool(self._target_twitter)

    def ticker_matches(self, event: LaunchEvent) -> bool:
        """The cheap pre-filter, evaluated before any metadata fetch."""
        return normalize_ticker(event.symbol) == self._target_ticker

    def score(
        self, event: LaunchEvent, metadata: Optional[TokenMetadata] = None
    ) -> MatchResult:
        """Score one candidate. Pure apart from the seen-ticker bookkeeping."""
        symbol = normalize_ticker(event.symbol)
        if symbol != self._target_ticker:
            return MatchResult(
                matched=False,
                confidence=0.0,
                reason=f"ticker {symbol!r} != {self._target_ticker!r}",
                metadata=metadata,
            )

        first_sighting = symbol not in self._seen_tickers
        self._seen_tickers.add(symbol)

        signals = [
            Signal("ticker", self.cfg.weight_ticker, 1.0, f"{symbol} == {self._target_ticker}")
        ]

        if self._target_twitter:
            signals.append(self._score_twitter(metadata))

        if self._target_name:
            hit = self._target_name in (event.name or "").lower()
            signals.append(
                Signal(
                    "name",
                    self.cfg.weight_name,
                    1.0 if hit else 0.0,
                    f"{self._target_name!r} {'in' if hit else 'not in'} {event.name!r}",
                )
            )

        if self._target_creator:
            hit = str(event.creator) == self._target_creator
            signals.append(
                Signal(
                    "creator",
                    self.cfg.weight_creator,
                    1.0 if hit else 0.0,
                    f"{event.creator} vs {self._target_creator}",
                )
            )

        total_weight = sum(s.weight for s in signals)
        if total_weight <= 0:
            raise ValueError("all match weights are zero; nothing can ever match")
        confidence = sum(s.weight * s.score for s in signals) / total_weight

        late = self.cfg.prefer_earliest and not first_sighting
        if late:
            confidence *= LATE_SIGHTING_PENALTY

        if self.cfg.require_twitter:
            twitter_signal = next((s for s in signals if s.name == "twitter"), None)
            if twitter_signal is None or twitter_signal.score < 1.0:
                return MatchResult(
                    matched=False,
                    confidence=confidence,
                    signals=signals,
                    reason="require_twitter is set and the link was not verified",
                    late_sighting=late,
                    metadata=metadata,
                )

        matched = confidence >= self.cfg.confidence_threshold
        reason = (
            f"confidence {confidence:.3f} >= threshold {self.cfg.confidence_threshold}"
            if matched
            else f"confidence {confidence:.3f} < threshold {self.cfg.confidence_threshold}"
        )
        if late:
            reason += f" (after x{LATE_SIGHTING_PENALTY} late-sighting penalty)"

        return MatchResult(
            matched=matched,
            confidence=confidence,
            signals=signals,
            reason=reason,
            late_sighting=late,
            metadata=metadata,
        )

    def _score_twitter(self, metadata: Optional[TokenMetadata]) -> Signal:
        if metadata is None or not metadata.resolved:
            # Deliberately 0, not "skip": we asked for a Twitter match and did
            # not get one. Treating unknown as neutral would let any clone
            # through whenever a gateway is slow.
            return Signal("twitter", self.cfg.weight_twitter, 0.0, "metadata unavailable")

        candidate = normalize_twitter(metadata.twitter)
        if not candidate:
            return Signal(
                "twitter",
                self.cfg.weight_twitter,
                0.0,
                f"no usable twitter link (raw={metadata.twitter!r})",
            )

        target = self._target_twitter
        if target.path and candidate.path:
            hit = target.path == candidate.path
        elif target.handle and candidate.handle:
            hit = target.handle == candidate.handle
        else:
            hit = False

        return Signal(
            "twitter",
            self.cfg.weight_twitter,
            1.0 if hit else 0.0,
            f"{candidate.handle or candidate.path} vs {target.handle or target.path}",
        )
