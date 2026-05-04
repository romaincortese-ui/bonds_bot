from __future__ import annotations

import time
from datetime import datetime, timezone

from bondsbot.backtest.data import FixtureRatesDataProvider
from bondsbot.config import BondsConfig
from bondsbot.oanda_client import OandaClient
from bondsbot.risk import country_dv01, max_country_dv01, max_portfolio_dv01, max_tenor_dv01, tenor_dv01
from bondsbot.state import StateStore
from bondsbot.strategies import generate_signal
from bondsbot.telegram import TelegramNotifier


def run_scan(config: BondsConfig, client: OandaClient, state_store: StateStore, notifier: TelegramNotifier) -> dict[str, object]:
    state = state_store.load()
    tradeable: set[str] = set()
    if config.has_oanda_credentials:
        try:
            tradeable = set(client.tradeable_instruments())
        except RuntimeError:
            tradeable = set()

    provider = FixtureRatesDataProvider(days=max(90, config.backtest_days + 60))
    data_provider_counts = {"oanda": 0, "fixture": 0}
    mapped_instruments: dict[str, str] = {}
    failures: list[str] = []
    signals = []
    for canonical in config.universe:
        oanda_instrument = config.oanda_instrument_for(canonical, tradeable) if tradeable else ""
        candles = []
        data_provider = "fixture"
        if oanda_instrument:
            try:
                candles = client.candles(oanda_instrument, count=max(90, config.backtest_days + 60), granularity="D")
            except RuntimeError as exc:
                failures.append(f"{canonical}:{exc}")
            if len(candles) >= 45:
                data_provider = "oanda"
                mapped_instruments[canonical] = oanda_instrument
            else:
                candles = []
        if not candles:
            candles = provider.history(canonical)
        data_provider_counts[data_provider] += 1
        signal = generate_signal(canonical, candles, config)
        if signal is not None:
            signal.metadata["data_provider"] = data_provider
            if oanda_instrument:
                signal.metadata["oanda_instrument"] = oanda_instrument
            signals.append(signal)

    signals.sort(key=lambda item: item.score, reverse=True)
    equity = float(state.get("equity", config.backtest_initial_balance))
    research_only = config.has_oanda_credentials and len(mapped_instruments) < config.min_oanda_rates_products
    snapshot = {
        "time": datetime.now(timezone.utc).isoformat(),
        "paper_trade": config.paper_trade or research_only,
        "research_only": research_only,
        "configured_symbols": list(config.universe),
        "mapped_oanda_instruments": mapped_instruments,
        "tradeable_instrument_count": len(tradeable),
        "data_provider_counts": data_provider_counts,
        "failures": failures[:5],
        "risk_caps": {
            "portfolio_dv01": round(max_portfolio_dv01(equity, config), 4),
            "country_dv01": round(max_country_dv01(equity, config), 4),
            "tenor_dv01": round(max_tenor_dv01(equity, config), 4),
        },
        "open_dv01_by_country": country_dv01([]),
        "open_dv01_by_tenor": tenor_dv01([]),
        "top_signals": [
            {
                "canonical": signal.canonical,
                "side": signal.side,
                "score": round(signal.score, 2),
                "strategy": signal.strategy,
                "country": signal.country,
                "tenor": signal.tenor_bucket,
                "data_provider": signal.metadata.get("data_provider", "fixture"),
            }
            for signal in signals[:5]
        ],
    }
    state["last_snapshot"] = snapshot
    state_store.save(state)

    message = "Bonds bot scan complete\n" + "\n".join(
        f"{row['canonical']} {row['side']} score={row['score']} {row['strategy']} {row['data_provider']}" for row in snapshot["top_signals"]
    )
    print(message, flush=True)
    notifier.send(message)
    return snapshot


def run_bot() -> None:
    config = BondsConfig.from_env()
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
    state_store = StateStore(config.state_file)
    client = OandaClient(config)

    if not config.paper_trade and not config.has_oanda_credentials:
        raise RuntimeError("Live trading requested but OANDA_ACCOUNT_ID/OANDA_API_TOKEN are missing")

    while True:
        run_scan(config, client, state_store, notifier)
        if config.run_once:
            return
        time.sleep(max(30, config.scan_interval_seconds))
