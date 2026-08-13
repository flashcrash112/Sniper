"""Regression coverage for the "Attempt to overwrite 'name' in LogRecord" crash.

`logging.Logger.makeRecord` raises `KeyError` the instant `extra=` contains a
key that collides with a built-in `LogRecord` attribute (`name`, `module`,
`process`, ...), and it does so *before* any handler or formatter runs — so a
formatter's own filtering (see `_RESERVED` in `JsonFormatter`) does not help.
This happened for real: `LaunchEvent.summary()` included `"name"` (the coin's
on-chain name), spread straight into `extra=` at the one call site that fires
on every matched candidate — the exact moment a live run is watching hardest.

It shipped because the integration tests never called `setup_logging()`, so
the root logger stayed at its default WARNING level and the crashing
`log.info()`/`log.debug()` calls were skipped before `makeRecord()` ever ran.
These tests configure logging for real, the way `cmd_run()` does, so this
class of bug is caught here instead of in production.
"""

import logging

import pytest
from solders.pubkey import Pubkey

from sniper.config import LoggingConfig
from sniper.logging_setup import _RESERVED, safe_extra, setup_logging, shutdown_logging
from sniper.pumpfun import LaunchEvent


@pytest.fixture
def configured_logger():
    """A real logger, configured the way the CLI configures it."""
    setup_logging(LoggingConfig(level="DEBUG", json=True))
    yield logging.getLogger("sniper.test")
    shutdown_logging()


def test_reserved_keys_include_name():
    """Pin the assumption the whole bug hinges on."""
    assert "name" in _RESERVED


def test_safe_extra_renames_colliding_keys():
    assert safe_extra({"name": "Joshua", "mint": "abc"}) == {
        "name_": "Joshua",
        "mint": "abc",
    }


def test_safe_extra_leaves_ordinary_keys_alone():
    fields = {"mint": "abc", "symbol": "TEST", "confidence": 0.9}
    assert safe_extra(fields) == fields


def test_launch_event_summary_does_not_use_the_reserved_name_key():
    """The actual root cause: summary() must never emit a "name" key."""
    event = LaunchEvent(
        mint=Pubkey.new_unique(),
        name="Joshua Coin",
        symbol="JOSHUA",
        uri="",
        creator=Pubkey.new_unique(),
    )
    assert "name" not in event.summary()
    assert event.summary()["coin_name"] == "Joshua Coin"


def test_logging_a_real_candidate_does_not_raise(configured_logger):
    """The exact call shape that crashed: extra=event.summary(), for real.

    This only proves anything because `configured_logger` actually calls
    setup_logging() — with an unconfigured root logger this passes for the
    wrong reason, since log.info() would be skipped before makeRecord() runs.
    """
    event = LaunchEvent(
        mint=Pubkey.new_unique(),
        name="Joshua Coin",
        symbol="JOSHUA",
        uri="",
        creator=Pubkey.new_unique(),
    )
    # Must not raise KeyError("Attempt to overwrite 'name' in LogRecord").
    configured_logger.info("candidate_scored", extra=event.summary())
    configured_logger.debug("candidate_seen", extra=safe_extra(event.summary()))


def test_every_reserved_key_is_rejected_by_the_real_logger(configured_logger):
    """Prove _RESERVED is exhaustive: every key in it really does crash raw.

    If a future Python version adds a new LogRecord attribute, _RESERVED
    (computed from a live LogRecord instance) picks it up automatically — this
    test is what confirms that computation is trustworthy, by throwing every
    one of those names at the real logger and requiring it to explode.
    """
    for key in _RESERVED - {"message", "asctime", "taskName"}:
        with pytest.raises(KeyError):
            configured_logger.info("probe", extra={key: "collision"})
        # And safe_extra() must be the fix: renamed, it goes through clean.
        configured_logger.info("probe", extra=safe_extra({key: "collision"}))
