# Collection Strategy Dashboard

Streamlit dashboard for comparing the active crypto and ETF strategy sleeves.

The Streamlit app is the primary dashboard. New strategies should be added as separate Python modules under `strategies/` and imported into `streamlit_app.py`. The older `index.html` / `app.js` browser dashboard and `server.js` routes are legacy scaffolding and should not be extended for new Streamlit work.

Active crypto strategies:

- Funding rate carry: spot long plus perpetual short when net funding is attractive.
- Cash-and-carry basis: spot plus dated futures convergence and roll management.
- Crypto Trend Rotation: daily spot trend and momentum rotation across a fixed crypto universe.

Active ETF strategy:

- Defensive Trend + Risk-Parity ETF: monthly trend/momentum filtered ETF allocation with inverse-volatility weights, BIL cash fallback, SPY 200DMA equity cap, and Yahoo daily adjusted data.
- Regime-Aware ETF Momentum: monthly OECD CLI macro regime classifier with yield-curve, high-yield spread, and unemployment stress override; regime-specific ETF momentum selection; trend and volatility controls; and Yahoo/FRED data.

Run the Streamlit app and open `http://127.0.0.1:8501/`. The dashboard uses deterministic synthetic research data for sleeves that do not have local data connectors yet, so the UI and strategy comparison workflow can be tested without API keys, exchange access, or historical data downloads.

```powershell
python -m streamlit run streamlit_app.py --server.port 8501
```

## Current Features

- Interactive strategy selector.
- Equal-allocation portfolio metrics.
- Selected strategy or portfolio equity curve.
- Scenario controls for capital, fee tier, risk mode, and lookback.
- Strategy comparison table with PnL, annualized return, volatility, drawdown, Sharpe, and risk score.
- Exportable CSV summary.
- Per-strategy signal stack, implementation checklist, risk controls, and source roadmap.
- Dedicated "What this strategy does" explanation for each strategy.

## Data Needed For Live Backtests

Funding carry:

- Funding history, mark/index price candles, spot candles, open interest, fees, and borrow or opportunity cost.
- Suggested sources: Binance futures funding and mark endpoints, Binance open interest, Coinbase or Kraken spot candles.
- Current dashboard public API frequency: 8-hour funding events plus 8-hour spot and mark candles.
- Live paper-monitor frequency: 20-second REST polling for current mark, spot, latest funding, next funding time, and open interest.
- Local storage burden: about 3 rows per day per series. A 365-day backtest is roughly 1,000 to 1,200 rows for funding and the same for each candle series.
- Caveat: Binance.com futures returned HTTP 451 from this environment. Coinbase public spot and Binance.US public spot returned HTTP 200, so public spot data is accessible, but an accessible futures/funding source is still required for this strategy.

Basis carry:

- Spot price, dated futures prices, expiry metadata, volume, open interest, fees, margin assumptions, and roll costs.
- Implemented sources: Deribit public BTC dated futures instruments and hourly TradingView candles, plus Coinbase Exchange BTC-USD hourly spot candles.
- Suggested alternate sources: CME futures data, Coinbase Derivatives if API access is available, or a market data vendor.
- Implemented frequency: hourly basis snapshots, not tick data.
- Local storage burden: small, because only a few listed expiries are active at a time.

Crypto trend rotation:

- Daily spot prices, market cap, and volume for BTC, ETH, SOL, XRP, ADA, DOGE, LINK, AVAX, LTC, and BCH.
- Implemented sources: CoinGecko first, CoinPaprika fallback, and Coinbase USD spot candles when aggregate sources are unavailable.
- Implemented frequency: daily data with weekly Monday-close rebalancing.
- Local storage burden: light CSV cache under `data/crypto_trend_rotation/`.

ETF research model:

- Defensive Trend + Risk-Parity ETF uses Yahoo daily adjusted ETF closes for SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, DBC, VNQ, and BIL. On each month-end it requires price above the long moving average and positive medium-term momentum, allocates eligible ETFs by inverse realized volatility, caps each ETF, sends unused weight to BIL, applies the SPY 200DMA equity-exposure cap, charges 10 bps per traded notional, and compares against SPY buy-and-hold, 60/40 SPY/IEF, equal-weight universe, and BIL cash.
- Regime-Aware ETF Momentum uses Yahoo adjusted closes for SPY, QQQ, IWM, EFA, EEM, HYG, TLT, IEF, GLD, and SHY plus FRED series USALOLITONOSTSAM, T10Y3M, BAMLH0A0HYM2, CPIAUCSL, and UNRATE. It lags monthly macro observations by one month, selects the strongest regime-eligible ETFs by blended 6-month and 12-month-skip-1-month momentum rank, applies a 200DMA haircut and 60-day portfolio-volatility target, charges 5 bps per traded notional, and reports regime attribution, drawdown, rolling Sharpe, benchmarks, and current holdings. FRED history is revised data; production-grade point-in-time research should use ALFRED vintages.

## Public API Coverage

Public market data is enough for research and paper monitoring, but not for real account reconciliation or real execution.

- Enough with public data: historical backtests, signal calculation, hypothetical fills, paper PnL, funding/basis dashboards, and live market monitoring.
- Requires private API keys: real balances, actual fills, funding fees received, margin state, liquidation risk, and reconciliation against exchange account history.
- Requires execution API keys: placing, canceling, or modifying orders.

The active public endpoints used by the dashboard are:

- `GET /api/funding-carry/backtest?symbol=BTCUSDT&days=365&capital=100000`
- `GET /api/funding-carry/live?symbol=BTCUSDT`
- `GET /api/futures-basis/backtest?days=30&capital=100000`
- `GET /api/futures-basis/live`
- Crypto Trend Rotation runs directly in Streamlit through `strategies/crypto_trend_rotation.py`.
- Defensive Trend + Risk-Parity ETF now runs directly in Streamlit through `strategies/defensive_trend_risk_parity.py`; it no longer needs the Node API route.
- Regime-Aware ETF Momentum runs directly in Streamlit through `strategies/regime_aware_etf_momentum.py`.

Refresh and validate all locally stored data for Regime-Aware ETF Momentum:

```powershell
python scripts/download_regime_aware_etf_data.py
```

The downloader keeps durable ETF and macro caches, uses stale local data when upstream services are temporarily unavailable, constructs the Treasury curve from Yahoo `^TNX - ^IRX` when the FRED `T10Y3M` export is unavailable, and merges the currently available BofA high-yield OAS export with a public historical FRED snapshot. Months still missing from the licensed OAS history use a HYG three-month-return proxy calibrated against overlapping actual OAS observations. Coverage and source provenance are stored in `data/regime_aware_etf_momentum/data_manifest.json`.

Refresh the shared Yahoo/FRED caches, run all four ETF strategies, persist their
analytics, and write a combined data coverage manifest:

```powershell
python scripts/download_all_etf_strategy_data.py
```

The combined manifest is stored in `data/all_etf_strategies_data_manifest.json`.
Committed local caches let the dashboard and backtests continue to work when an
upstream public endpoint is unavailable.

Dynamic Macro Factor Allocation is implemented separately in `strategies/dynamic_macro_factor_allocation.py`. It runs both the rule-based and NumPy projected-gradient optimizer versions, uses rolling 36-month ETF factor regressions, applies the requested risk controls, and exports all requested analytics under `data/dynamic_macro_factor_allocation/`.

```powershell
python strategies/dynamic_macro_factor_allocation.py
```

Pure Business-Cycle Asset Rotation is implemented separately in `strategies/pure_business_cycle_asset_rotation.py`. It uses CLI level and three-month CLI direction only, rotates monthly through the supplied paper-inspired allocations, and compares against SPY, 60/40 SPY/IEF, and risk parity.

```powershell
python strategies/pure_business_cycle_asset_rotation.py
```

Known data and modeling limitations for Strategies 1-3 are documented in `DATA_LIMITATIONS.md`.

BlackRock Factor Replication Portfolio is implemented separately in `strategies/blackrock_factor_replication.py`. It targets a static growth, duration, inflation, credit, and commodity factor mix using rolling 36-month ETF regressions and a long-only, fully invested, 30%-capped optimizer.

```powershell
python strategies/blackrock_factor_replication.py
```

Strategies 1-4 are available from the main dashboard selector and from the dedicated **Strategies 1-4 Research** tab. Known limitations for all four strategies are documented in `DATA_LIMITATIONS.md`.

The funding and futures-basis routes run through the local `server.js` proxy, which the Streamlit app can start or reuse.

## Missing Details Before Production Use

- Which venues you can legally and operationally trade from your jurisdiction.
- Actual fee tier, borrow rates, collateral rules, and leverage limits.
- Desired backtest frequency: 8 hour, hourly, daily, or monthly.
- Whether the dashboard should stay static or become a Python/Node service that stores historical data.
- Whether strategies should be research-only, paper-trading, or connected to execution.
