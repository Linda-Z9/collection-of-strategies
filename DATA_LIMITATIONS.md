# Data Limitations

## Shared Across Strategies 1-4

- Yahoo adjusted-close histories and public FRED exports may be revised after download.
- FRED macro histories are revised series, not point-in-time ALFRED vintages. Historical regime classifications therefore may contain revision look-ahead bias.
- Macro observations are lagged one month, but actual publication delays differ by series.
- `USALOLITONOSTSAM` currently ends in January 2024 in the available FRED export. Later regime calculations forward-fill the January 2024 CLI value, making the latest growth regime stale.
- Local caches permit offline reruns but can become stale when upstream services are unavailable.
- Transaction costs are modeled as fixed traded-notional basis points and exclude bid-ask variation, taxes, market impact, and fund-management fees.

## Strategy 1: Regime-Aware ETF Momentum

- The stress override uses high-yield OAS. Actual OAS observations cover 230 monthly observations; 76 otherwise-missing months use a calibrated HYG-return proxy.
- `T10Y3M` uses Yahoo `^TNX - ^IRX` when the FRED export is unavailable. `^IRX` is a 13-week bill proxy rather than the exact constant-maturity 3-month series.
- ETFs launched after the requested start date are unavailable until their actual launch dates.
- Momentum uses trading-day approximations for 6, 12, and skip-1-month lookbacks.

## Strategy 2: Dynamic Macro Factor Allocation

- Version B cannot estimate its first 36-month factor matrix until May 2010 because HYG history begins in April 2007. It remains in SHY before that date.
- Version A does not require regression and rotates from January 2007.
- Factor exposures are estimated from ETF proxy returns, not institutional factor indices.
- The five proxy factors are correlated and can make regression coefficients unstable or unintuitive.
- The optimizer uses a NumPy projected-gradient implementation instead of a full quadratic-program solver.
- The optimizer objective tracks raw regression betas against target factor numbers whose units are research assumptions.
- Missing high-yield OAS months and Treasury-curve fallback limitations are inherited from Strategy 1.

## Strategy 3: Pure Business-Cycle Asset Rotation

- Regimes intentionally use only CLI level and CLI three-month direction. UNRATE, CPI, and T10Y3M are context data and do not change allocations.
- The portfolio weights are a paper-inspired interpretation, not a direct replication of a JPM investable index.
- HYG launched in April 2007; prescribed HYG allocation before launch is moved to SHY.
- Expansion includes QQQ because the supplied allocation explicitly requires it, although QQQ was omitted from the shorter asset-universe list.

## Strategy 4: BlackRock Factor Replication Portfolio

- The first valid 36-month factor matrix is May 2010 because HYG begins in April 2007. The portfolio remains in SHY before then.
- ETF proxy factors are correlated, so rolling regression betas and optimized ETF weights can be unstable.
- The fixed target factor percentages are matched against raw ETF regression betas rather than normalized institutional factor units.
- The optimizer is a NumPy projected-gradient implementation rather than a dedicated quadratic-program solver.
- ETF factor proxies include fund-specific fees, tracking error, and launch-date limitations.

## Aggregated Risk Dashboard

- Regime probability is a model-implied heuristic from CLI level and momentum, not a calibrated recession or regime forecast. It also inherits the stale January 2024 CLI limitation.
- Non-factor strategies are mapped through the latest BlackRock ETF regression matrix to estimate common factor exposure.
- ETF risk contribution uses current weights and a trailing 252-day covariance matrix. It is not a forward-looking risk model.
- Historical stress tests use realized backtest returns over selected windows. Hypothetical shocks use a linear current-weight approximation.
- Regime accuracy is unavailable until a point-in-time outcome definition is specified.
- Upcoming CPI, FOMC, NFP, macro-surprise, and VIX monitoring remain unavailable because live calendar, consensus-estimate, and VIX data connectors are not configured.
