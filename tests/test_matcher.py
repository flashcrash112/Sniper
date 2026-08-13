import pytest
from solders.pubkey import Pubkey

from sniper.config import TargetConfig
from sniper.matcher import Matcher, normalize_ticker, normalize_twitter
from sniper.metadata import TokenMetadata
from sniper.pumpfun import LaunchEvent


def launch(symbol="TEST", name="Test Coin", creator=None, uri="ipfs://x"):
    return LaunchEvent(
        mint=Pubkey.new_unique(),
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator or Pubkey.new_unique(),
    )


def resolved(twitter):
    return TokenMetadata(twitter=twitter, source="test")


# --- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$PEPE", "PEPE"),
        ("pepe", "PEPE"),
        ("  PePe  ", "PEPE"),
        (None, ""),
    ],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://twitter.com/realcoin",
        "https://x.com/realcoin",
        "https://www.x.com/realcoin?s=20",
        "x.com/RealCoin",
        "@RealCoin",
        "realcoin",
        "https://mobile.twitter.com/realcoin/",
    ],
)
def test_twitter_spellings_all_normalise_to_the_same_handle(raw):
    assert normalize_twitter(raw).handle == "realcoin"


def test_twitter_status_urls_keep_the_status_path():
    ref = normalize_twitter("https://x.com/realcoin/status/1234567890")
    assert ref.handle == "realcoin"
    assert ref.path == "realcoin/status/1234567890"


def test_twitter_community_links_have_no_handle():
    ref = normalize_twitter("https://x.com/i/communities/1234")
    assert ref.handle is None
    assert ref.path == "i/communities/1234"


@pytest.mark.parametrize(
    "raw", ["https://facebook.com/realcoin", "", None, "not a url at all", "https://x.com/"]
)
def test_non_twitter_references_are_rejected(raw):
    assert not normalize_twitter(raw)


# --- scoring ---------------------------------------------------------------


def test_ticker_only_match_scores_full_confidence():
    matcher = Matcher(TargetConfig(ticker="TEST"))
    result = matcher.score(launch(symbol="TEST"))
    assert result.matched
    assert result.confidence == pytest.approx(1.0)


def test_wrong_ticker_is_rejected_before_scoring():
    matcher = Matcher(TargetConfig(ticker="TEST"))
    result = matcher.score(launch(symbol="OTHER"))
    assert not result.matched
    assert result.confidence == 0.0
    assert result.signals == []


def test_dollar_prefixed_ticker_still_matches():
    matcher = Matcher(TargetConfig(ticker="$TEST"))
    assert matcher.score(launch(symbol="test")).matched


def test_twitter_match_raises_confidence_to_full():
    matcher = Matcher(TargetConfig(ticker="TEST", twitter="https://x.com/realcoin"))
    result = matcher.score(launch(), resolved("https://twitter.com/RealCoin"))
    assert result.matched
    assert result.confidence == pytest.approx(1.0)


def test_twitter_mismatch_falls_below_the_default_threshold():
    """The scam-clone case: right ticker, wrong socials."""
    matcher = Matcher(TargetConfig(ticker="TEST", twitter="https://x.com/realcoin"))
    result = matcher.score(launch(), resolved("https://x.com/scamclone"))
    assert not result.matched
    assert result.confidence == pytest.approx(0.6)


def test_unresolved_metadata_scores_zero_not_neutral():
    """A Twitter we could not verify must not be treated as verified."""
    matcher = Matcher(TargetConfig(ticker="TEST", twitter="https://x.com/realcoin"))
    result = matcher.score(launch(), TokenMetadata())
    assert not result.matched
    assert result.confidence == pytest.approx(0.6)


def test_require_twitter_hard_gates_regardless_of_threshold():
    matcher = Matcher(
        TargetConfig(
            ticker="TEST",
            twitter="https://x.com/realcoin",
            require_twitter=True,
            confidence_threshold=0.1,
        )
    )
    result = matcher.score(launch(), TokenMetadata())
    assert not result.matched
    assert "require_twitter" in result.reason


def test_creator_signal():
    creator = Pubkey.new_unique()
    matcher = Matcher(TargetConfig(ticker="TEST", creator=str(creator)))
    assert matcher.score(launch(creator=creator)).matched

    matcher2 = Matcher(TargetConfig(ticker="TEST", creator=str(Pubkey.new_unique())))
    result = matcher2.score(launch(creator=creator))
    assert not result.matched


def test_name_signal_is_a_substring_check():
    matcher = Matcher(TargetConfig(ticker="TEST", name_contains="official"))
    assert matcher.score(launch(name="The Official Coin")).matched
    assert not Matcher(
        TargetConfig(ticker="TEST", name_contains="official", confidence_threshold=1.0)
    ).score(launch(name="Random Coin")).matched


def test_prefer_earliest_penalises_the_clone_wave():
    matcher = Matcher(TargetConfig(ticker="TEST", prefer_earliest=True))

    first = matcher.score(launch())
    assert first.matched and not first.late_sighting

    second = matcher.score(launch())
    assert not second.matched
    assert second.late_sighting
    assert second.confidence == pytest.approx(0.5)


def test_prefer_earliest_off_keeps_matching():
    matcher = Matcher(TargetConfig(ticker="TEST", prefer_earliest=False))
    assert matcher.score(launch()).matched
    assert matcher.score(launch()).matched


def test_needs_metadata_only_when_twitter_is_configured():
    assert not Matcher(TargetConfig(ticker="TEST")).needs_metadata()
    assert Matcher(TargetConfig(ticker="TEST", twitter="@realcoin")).needs_metadata()


def test_unparseable_target_twitter_fails_fast_at_startup():
    with pytest.raises(ValueError, match="not a recognisable"):
        Matcher(TargetConfig(ticker="TEST", twitter="https://facebook.com/nope"))
