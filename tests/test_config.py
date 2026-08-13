import pytest

from sniper.config import ConfigError, load_config
from sniper.pumpfun import BuyLayout

BASE = """
[rpc]
http_url = "https://rpc.example.com"

[geyser]
backend = "logs"
endpoint = "wss://rpc.example.com"

[target]
ticker = "TEST"

[wallet]
keystore_path = "./key.json"

[buy]
amount_sol = 0.5
slippage_bps = 1000
priority_fee_microlamports = 1000000

[safety]
max_total_spend_sol = 1.0
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_minimal_config_loads(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.target.ticker == "TEST"
    assert cfg.buy.amount_lamports == 500_000_000
    assert cfg.buy.layout is BuyLayout.CURRENT
    assert cfg.rpc.send_urls == ["https://rpc.example.com"]


def test_env_var_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-token")
    text = BASE.replace(
        'endpoint = "wss://rpc.example.com"',
        'endpoint = "wss://rpc.example.com/?api-key=${MY_KEY}"',
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.geyser.endpoint == "wss://rpc.example.com/?api-key=secret-token"


def test_missing_env_var_is_an_error(tmp_path):
    text = BASE.replace('ticker = "TEST"', 'ticker = "${DEFINITELY_NOT_SET_XYZ}"')
    with pytest.raises(ConfigError, match="DEFINITELY_NOT_SET_XYZ"):
        load_config(write(tmp_path, text))


def test_priority_fee_is_included_in_the_cap_check(tmp_path):
    """A cap that cannot cover one buy means the bot could never fire."""
    text = BASE.replace("max_total_spend_sol = 1.0", "max_total_spend_sol = 0.4")
    with pytest.raises(ConfigError, match="below the cost of a single buy"):
        load_config(write(tmp_path, text))


def test_priority_fee_lamports_conversion(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    # 1e6 microlamports/CU * 120_000 CU / 1e6 = 120_000 lamports
    assert cfg.buy.priority_fee_lamports == 120_000


def test_unknown_backend_is_rejected(tmp_path):
    text = BASE.replace('backend = "logs"', 'backend = "carrier-pigeon"')
    with pytest.raises(ConfigError, match="backend must be one of"):
        load_config(write(tmp_path, text))


def test_require_twitter_without_a_twitter_is_rejected(tmp_path):
    text = BASE.replace('ticker = "TEST"', 'ticker = "TEST"\nrequire_twitter = true')
    with pytest.raises(ConfigError, match="no twitter handle"):
        load_config(write(tmp_path, text))


def test_out_of_range_values_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="confidence_threshold"):
        load_config(
            write(tmp_path, BASE.replace('ticker = "TEST"', 'ticker = "TEST"\nconfidence_threshold = 1.5'))
        )
    with pytest.raises(ConfigError, match="slippage_bps"):
        load_config(write(tmp_path, BASE.replace("slippage_bps = 1000", "slippage_bps = 20000")))
    with pytest.raises(ConfigError, match="amount_sol"):
        load_config(write(tmp_path, BASE.replace("amount_sol = 0.5", "amount_sol = 0")))


def test_missing_required_setting_names_it(tmp_path):
    text = BASE.replace('http_url = "https://rpc.example.com"', "")
    with pytest.raises(ConfigError, match=r"\[rpc\] http_url"):
        load_config(write(tmp_path, text))


def test_wrong_type_is_reported(tmp_path):
    text = BASE.replace("amount_sol = 0.5", 'amount_sol = "half"')
    with pytest.raises(ConfigError, match="must be a number"):
        load_config(write(tmp_path, text))


def test_example_config_is_valid(monkeypatch):
    """The shipped example must actually load."""
    from pathlib import Path

    monkeypatch.setenv("HELIUS_API_KEY", "test-key")
    monkeypatch.setenv("GEYSER_X_TOKEN", "test-token")
    example = Path(__file__).parent.parent / "config.example.toml"
    cfg = load_config(example)
    assert cfg.target.ticker
    assert cfg.safety.max_total_spend_sol > 0
