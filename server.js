const http = require("http");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");

const root = __dirname;
const port = Number(process.env.PORT || 8000);
const host = "127.0.0.1";
const futuresBase = "https://fapi.binance.com";
const spotBase = "https://api.binance.com";
const deribitBase = "https://www.deribit.com/api/v2";
const coinbaseExchangeBase = "https://api.exchange.coinbase.com";
const memoryCache = new Map();
const deribitMonths = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

const types = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".csv": "text/csv; charset=utf-8"
};

function sendFile(response, filePath) {
  fs.readFile(filePath, (error, content) => {
    if (error) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not found");
      return;
    }

    response.writeHead(200, {
      "Content-Type": types[path.extname(filePath)] || "application/octet-stream",
      "Cache-Control": "no-store"
    });
    response.end(content);
  });
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}

function latestMicroFile() {
  const directory = path.join(root, "data", "microstructure");
  if (!fs.existsSync(directory)) {
    return null;
  }
  const files = fs
    .readdirSync(directory)
    .filter(
      (name) =>
        (name.startsWith("coinbase_l2_features_") || name.startsWith("kraken_l2_features_")) &&
        name.endsWith(".csv")
    )
    .map((name) => path.join(directory, name))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  return files[0] || null;
}

function handleMicroBacktest(url, response) {
  const file = latestMicroFile();
  if (!file) {
    sendJson(response, 404, { error: "No captured Kraken/Coinbase L2 feature file found yet." });
    return;
  }

  const args = [
    path.join(root, "scripts", "micro_backtest.py"),
    "--file",
    file,
    "--capital",
    url.searchParams.get("capital") || "100000",
    "--horizon-seconds",
    url.searchParams.get("horizonSeconds") || "10",
    "--threshold",
    url.searchParams.get("threshold") || "2.5",
    "--cost-bps",
    url.searchParams.get("costBps") || "2",
    "--max-spread-bps",
    url.searchParams.get("maxSpreadBps") || "1",
    "--rolling-window",
    url.searchParams.get("rollingWindow") || "120"
  ];

  execFile("python", args, { cwd: root, maxBuffer: 20 * 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      sendJson(response, 502, { error: stderr || error.message });
      return;
    }
    try {
      sendJson(response, 200, JSON.parse(stdout));
    } catch (parseError) {
      sendJson(response, 502, { error: parseError.message, stdout });
    }
  });
}

function handleIntradayPerformance(url, response) {
  const file = latestMicroFile();
  if (!file) {
    sendJson(response, 404, { error: "No captured Kraken/Coinbase L2 feature file found yet." });
    return;
  }

  const args = [
    path.join(root, "scripts", "micro_backtest.py"),
    "--file",
    file,
    "--capital",
    url.searchParams.get("capital") || "100000",
    "--horizon-seconds",
    url.searchParams.get("horizonSeconds") || "10",
    "--threshold",
    url.searchParams.get("threshold") || "2.5",
    "--cost-bps",
    url.searchParams.get("costBps") || "2",
    "--max-spread-bps",
    url.searchParams.get("maxSpreadBps") || "1",
    "--rolling-window",
    url.searchParams.get("rollingWindow") || "120"
  ];

  execFile("python", args, { cwd: root, maxBuffer: 20 * 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      sendJson(response, 502, { error: stderr || error.message });
      return;
    }

    try {
      const payload = JSON.parse(stdout);
      const points = payload.points || [];
      const lastPoint = points[points.length - 1] || null;
      const firstTime = points[0]?.time || null;
      const lastTime = lastPoint?.time || null;
      sendJson(response, 200, {
        mode: "live-intraday-paper",
        strategy: "Kraken L2 Microstructure Alpha",
        source: path.basename(file),
        file,
        rows: payload.rows || 0,
        refreshedAt: Date.now(),
        startedAt: firstTime,
        lastSignalAt: lastTime,
        assumptions: payload.assumptions || {},
        metrics: payload.metrics || {},
        latest: lastPoint,
        equitySparkline: points.slice(-240).map((point) => ({
          time: point.time,
          equity: point.equity,
          signal: point.signal,
          position: point.position
        }))
      });
    } catch (parseError) {
      sendJson(response, 502, { error: parseError.message, stdout });
    }
  });
}

async function fetchJson(url, cacheMs = 0) {
  const cached = memoryCache.get(url);
  if (cached && Date.now() - cached.time < cacheMs) {
    return cached.value;
  }

  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${url}`);
  }

  const value = await response.json();
  if (cacheMs > 0) {
    memoryCache.set(url, { time: Date.now(), value });
  }
  return value;
}

async function fetchText(url, cacheMs = 0) {
  const cached = memoryCache.get(url);
  if (cached && Date.now() - cached.time < cacheMs) {
    return cached.value;
  }

  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${url}`);
  }

  const value = await response.text();
  if (cacheMs > 0) {
    memoryCache.set(url, { time: Date.now(), value });
  }
  return value;
}

async function fetchPaged(endpoint, params, limit, cacheMs) {
  const rows = [];
  let startTime = Number(params.startTime);
  const endTime = Number(params.endTime);

  while (startTime < endTime) {
    const query = new URLSearchParams({
      ...params,
      startTime: String(startTime),
      endTime: String(endTime),
      limit: String(limit)
    });
    const batch = await fetchJson(`${endpoint}?${query.toString()}`, cacheMs);
    if (!Array.isArray(batch) || batch.length === 0) {
      break;
    }

    rows.push(...batch);
    const last = batch[batch.length - 1];
    const lastTime = Array.isArray(last) ? Number(last[0]) : Number(last.fundingTime);
    const nextTime = lastTime + 1;
    if (nextTime <= startTime) {
      break;
    }
    startTime = nextTime;

    if (batch.length < limit) {
      break;
    }
  }

  return rows;
}

function nearestClose(candles, eventTime) {
  let best = null;
  for (const candle of candles) {
    const openTime = Number(candle[0]);
    if (openTime <= eventTime) {
      best = Number(candle[4]);
    } else {
      break;
    }
  }
  return best;
}

function nearestCoinbaseClose(candles, eventTime) {
  let best = null;
  for (const candle of candles) {
    const time = Number(candle[0]) * 1000;
    if (time <= eventTime) {
      best = Number(candle[4]);
    } else {
      break;
    }
  }
  return best;
}

async function fetchCoinbaseCandles(productId, startTime, endTime, granularity, cacheMs) {
  const rows = [];
  const maxWindowMs = granularity * 1000 * 300;
  let cursor = startTime;

  while (cursor < endTime) {
    const windowEnd = Math.min(endTime, cursor + maxWindowMs);
    const query = new URLSearchParams({
      start: new Date(cursor).toISOString(),
      end: new Date(windowEnd).toISOString(),
      granularity: String(granularity)
    });
    const batch = await fetchJson(
      `${coinbaseExchangeBase}/products/${productId}/candles?${query.toString()}`,
      cacheMs
    );
    if (Array.isArray(batch)) {
      rows.push(...batch);
    }
    cursor = windowEnd;
  }

  const deduped = new Map();
  rows.forEach((row) => deduped.set(Number(row[0]), row));
  return [...deduped.values()].sort((a, b) => Number(a[0]) - Number(b[0]));
}

function deribitChartToCandles(chart) {
  const result = chart.result || {};
  const ticks = result.ticks || [];
  const close = result.close || [];
  const open = result.open || [];
  const high = result.high || [];
  const low = result.low || [];
  const volume = result.volume || [];
  return ticks
    .map((time, index) => ({
      time: Number(time),
      open: Number(open[index]),
      high: Number(high[index]),
      low: Number(low[index]),
      close: Number(close[index]),
      volume: Number(volume[index] || 0)
    }))
    .filter((row) => Number.isFinite(row.close))
    .sort((a, b) => a.time - b.time);
}

function parseDeribitFutureName(name) {
  const match = name.match(/^BTC-(\d{1,2})([A-Z]{3})(\d{2})$/);
  if (!match) {
    return null;
  }
  const day = Number(match[1]);
  const month = deribitMonths.indexOf(match[2]);
  const year = 2000 + Number(match[3]);
  if (month < 0) {
    return null;
  }
  return Date.UTC(year, month, day, 8, 0, 0);
}

function lastFriday(year, monthIndex) {
  const date = new Date(Date.UTC(year, monthIndex + 1, 0, 8, 0, 0));
  while (date.getUTCDay() !== 5) {
    date.setUTCDate(date.getUTCDate() - 1);
  }
  return date;
}

function isLastFridayExpiry(expiryTime) {
  const date = new Date(expiryTime);
  return lastFriday(date.getUTCFullYear(), date.getUTCMonth()).getTime() === expiryTime;
}

function isQuarterlyExpiry(expiryTime) {
  const month = new Date(expiryTime).getUTCMonth();
  return [2, 5, 8, 11].includes(month) && isLastFridayExpiry(expiryTime);
}

function deribitDeliveryFeeBps(expiryTime, nonWeeklyDeliveryFeeBps) {
  return isLastFridayExpiry(expiryTime) ? nonWeeklyDeliveryFeeBps : 0;
}

function formatDeribitFutureName(date) {
  const day = String(date.getUTCDate());
  const month = deribitMonths[date.getUTCMonth()];
  const year = String(date.getUTCFullYear()).slice(-2);
  return `BTC-${day}${month}${year}`;
}

function generatedDeribitFutureCandidates(startTime, endTime, minDte, maxDte, contractSet = "weekly-quarterly") {
  const start = new Date(startTime - maxDte * 86400000);
  const end = new Date(endTime + maxDte * 86400000);
  const names = new Set();
  for (let year = start.getUTCFullYear(); year <= end.getUTCFullYear() + 1; year += 1) {
    for (let month = 0; month < 12; month += 1) {
      const expiry = lastFriday(year, month);
      const expiryTime = expiry.getTime();
      if (
        contractSet !== "weekly-quarterly" &&
        expiryTime >= startTime + minDte * 86400000 &&
        expiryTime <= endTime + maxDte * 86400000
      ) {
        names.add(formatDeribitFutureName(expiry));
      }
    }
  }

  if (contractSet === "weekly-quarterly") {
    const cursor = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate(), 8, 0, 0));
    while (cursor.getUTCDay() !== 5) {
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    while (cursor <= end) {
      const expiryTime = cursor.getTime();
      const eligibleTime =
        expiryTime >= startTime + minDte * 86400000 &&
        expiryTime <= endTime + maxDte * 86400000;
      if (eligibleTime && (!isLastFridayExpiry(expiryTime) || isQuarterlyExpiry(expiryTime))) {
        names.add(formatDeribitFutureName(cursor));
      }
      cursor.setUTCDate(cursor.getUTCDate() + 7);
    }
  }
  return [...names].sort((a, b) => parseDeribitFutureName(a) - parseDeribitFutureName(b));
}

function chooseDeribitFuture(instruments, minDte, maxDte) {
  const now = Date.now();
  return instruments
    .filter((instrument) => instrument.instrument_name !== "BTC-PERPETUAL")
    .map((instrument) => ({
      ...instrument,
      dte: (Number(instrument.expiration_timestamp) - now) / 86400000
    }))
    .filter((instrument) => instrument.dte >= minDte && instrument.dte <= maxDte)
    .sort((a, b) => a.dte - b.dte)[0];
}

async function chooseBacktestFuture(instruments, requestedInstrument, minDte, maxDte, startTime, endTime, cacheMs) {
  const requested = instruments.find((instrument) => instrument.instrument_name === requestedInstrument);
  if (requested) {
    const chart = await fetchJson(
      `${deribitBase}/public/get_tradingview_chart_data?instrument_name=${requested.instrument_name}&start_timestamp=${startTime}&end_timestamp=${endTime}&resolution=60`,
      cacheMs
    );
    return { chosen: requested, futureCandles: deribitChartToCandles(chart) };
  }

  const now = Date.now();
  const candidates = instruments
    .filter((instrument) => instrument.instrument_name !== "BTC-PERPETUAL")
    .map((instrument) => ({
      ...instrument,
      dte: (Number(instrument.expiration_timestamp) - now) / 86400000
    }))
    .filter((instrument) => instrument.dte >= minDte && instrument.dte <= maxDte)
    .sort((a, b) => a.dte - b.dte);

  const targetRows = Math.max(24, ((endTime - startTime) / 3600000) * 0.8);
  let best = null;
  for (const candidate of candidates.slice(0, 6)) {
    const chart = await fetchJson(
      `${deribitBase}/public/get_tradingview_chart_data?instrument_name=${candidate.instrument_name}&start_timestamp=${startTime}&end_timestamp=${endTime}&resolution=60`,
      cacheMs
    );
    const futureCandles = deribitChartToCandles(chart);
    const choice = { chosen: candidate, futureCandles };
    if (!best || futureCandles.length > best.futureCandles.length) {
      best = choice;
    }
    if (futureCandles.length >= targetRows) {
      return choice;
    }
  }
  return best || { chosen: null, futureCandles: [] };
}

function parseDeribitOptionName(name) {
  const parts = name.split("-");
  if (parts.length !== 4) {
    return null;
  }
  const expiry = Date.parse(`${parts[1].replace(/(\d+)([A-Z]+)(\d+)/, "$1 $2 20$3")} UTC`);
  return {
    expiry,
    strike: Number(parts[2]),
    type: parts[3]
  };
}

function realizedVolFromCoinbase(candles) {
  const closes = candles.map((row) => Number(row[4])).filter(Number.isFinite);
  const returns = [];
  for (let i = 1; i < closes.length; i += 1) {
    returns.push(Math.log(closes[i] / closes[i - 1]));
  }
  if (returns.length < 2) {
    return 0;
  }
  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) /
    Math.max(1, returns.length - 1);
  return Math.sqrt(variance) * Math.sqrt(365 * 24);
}

async function handleOptionsSnapshot(url, response) {
  const currency = (url.searchParams.get("currency") || "BTC").toUpperCase();
  const minDte = Number(url.searchParams.get("minDte") || 10);
  const maxDte = Number(url.searchParams.get("maxDte") || 90);
  const moneynessPct = Number(url.searchParams.get("moneynessPct") || 15) / 100;
  const endTime = Date.now();
  const startTime = endTime - 14 * 86400000;
  const cacheMs = 30 * 1000;

  const [summaryPayload, spotCandles] = await Promise.all([
    fetchJson(`${deribitBase}/public/get_book_summary_by_currency?currency=${currency}&kind=option`, cacheMs),
    fetchCoinbaseCandles("BTC-USD", startTime, endTime, 3600, cacheMs)
  ]);

  const rows = (summaryPayload.result || [])
    .map((row) => {
      const parsed = parseDeribitOptionName(row.instrument_name);
      if (!parsed) {
        return null;
      }
      const dte = (parsed.expiry - Date.now()) / 86400000;
      const underlying = Number(row.underlying_price || row.estimated_delivery_price);
      const moneyness = underlying ? parsed.strike / underlying - 1 : null;
      return {
        instrument: row.instrument_name,
        type: parsed.type,
        expiry: parsed.expiry,
        dte,
        strike: parsed.strike,
        underlying,
        moneyness,
        markIv: Number(row.mark_iv) / 100,
        markPrice: Number(row.mark_price),
        bidPrice: row.bid_price === null ? null : Number(row.bid_price),
        askPrice: row.ask_price === null ? null : Number(row.ask_price),
        openInterest: Number(row.open_interest || 0),
        volumeUsd: Number(row.volume_usd || 0)
      };
    })
    .filter(
      (row) =>
        row &&
        row.dte >= minDte &&
        row.dte <= maxDte &&
        Math.abs(row.moneyness) <= moneynessPct &&
        Number.isFinite(row.markIv)
    );

  const atm = rows.sort((a, b) => Math.abs(a.moneyness) - Math.abs(b.moneyness)).slice(0, 40);
  const avgIv = atm.reduce((sum, row) => sum + row.markIv, 0) / Math.max(1, atm.length);
  const rv14d = realizedVolFromCoinbase(spotCandles);
  const term = new Map();
  atm.forEach((row) => {
    const bucket = row.dte < 30 ? "near" : row.dte < 60 ? "mid" : "far";
    const existing = term.get(bucket) || { count: 0, iv: 0 };
    existing.count += 1;
    existing.iv += row.markIv;
    term.set(bucket, existing);
  });

  sendJson(response, 200, {
    mode: "public-deribit-options-snapshot",
    currency,
    time: Date.now(),
    frequency: "live option-chain snapshot, suitable for forward collection",
    rows: { options: rows.length, atm: atm.length, spotCandles: spotCandles.length },
    filters: { minDte, maxDte, moneynessPct },
    metrics: {
      avgIv,
      rv14d,
      ivMinusRv: avgIv - rv14d,
      nearIv: term.has("near") ? term.get("near").iv / term.get("near").count : null,
      midIv: term.has("mid") ? term.get("mid").iv / term.get("mid").count : null,
      farIv: term.has("far") ? term.get("far").iv / term.get("far").count : null
    },
    options: atm
  });
}

async function fetchDeribitVolIndex(currency, startTime, endTime, resolution, cacheMs) {
  const rows = [];
  let cursorEnd = endTime;
  while (cursorEnd > startTime) {
    const payload = await fetchJson(
      `${deribitBase}/public/get_volatility_index_data?currency=${currency}&start_timestamp=${startTime}&end_timestamp=${cursorEnd}&resolution=${resolution}`,
      cacheMs
    );
    const data = payload.result?.data || [];
    rows.push(...data);
    const continuation = payload.result?.continuation;
    if (!continuation || !data.length || continuation >= cursorEnd) {
      break;
    }
    cursorEnd = continuation;
  }
  const deduped = new Map();
  rows.forEach((row) => deduped.set(Number(row[0]), row));
  return [...deduped.values()].sort((a, b) => Number(a[0]) - Number(b[0]));
}

function rollingRealizedVol(candles, time, windowHours) {
  const closes = candles
    .filter((row) => Number(row[0]) * 1000 <= time)
    .slice(-windowHours - 1)
    .map((row) => Number(row[4]));
  if (closes.length < windowHours / 2) {
    return null;
  }
  const returns = [];
  for (let i = 1; i < closes.length; i += 1) {
    returns.push(Math.log(closes[i] / closes[i - 1]));
  }
  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) / Math.max(1, returns.length - 1);
  return Math.sqrt(variance) * Math.sqrt(365 * 24);
}

async function handleOptionsBacktest(url, response) {
  const currency = (url.searchParams.get("currency") || "BTC").toUpperCase();
  const days = Math.min(365, Math.max(7, Number(url.searchParams.get("days") || 180)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const notionalFraction = Math.min(1, Math.max(0.1, Number(url.searchParams.get("notionalFraction") || 0.25)));
  const entrySpread = Number(url.searchParams.get("entrySpread") || 0.05);
  const exitSpread = Number(url.searchParams.get("exitSpread") || 0.01);
  const feeDragAnnual = Number(url.searchParams.get("feeDragAnnual") || 0.03);
  const rvWindowHours = Number(url.searchParams.get("rvWindowHours") || 24 * 7);
  const endTime = Date.now();
  const startTime = endTime - days * 86400000;
  const cacheMs = 15 * 60 * 1000;

  const [volRows, spotCandles] = await Promise.all([
    fetchDeribitVolIndex(currency, startTime, endTime, 3600, cacheMs),
    fetchCoinbaseCandles("BTC-USD", startTime, endTime, 3600, cacheMs)
  ]);

  let equity = capital;
  let inPosition = false;
  let trades = 0;
  const points = [];
  for (const row of volRows) {
    const time = Number(row[0]);
    const iv = Number(row[4]) / 100;
    const rv = rollingRealizedVol(spotCandles, time, rvWindowHours);
    if (!Number.isFinite(iv) || !Number.isFinite(rv)) {
      continue;
    }
    const spread = iv - rv;
    if (!inPosition && spread >= entrySpread) {
      inPosition = true;
      trades += 1;
    } else if (inPosition && spread <= exitSpread) {
      inPosition = false;
      trades += 1;
    }
    if (inPosition) {
      const hourlyCarry = ((iv * iv - rv * rv) / (365 * 24) - feeDragAnnual / (365 * 24)) * notionalFraction;
      equity *= 1 + hourlyCarry;
    }
    points.push({ time, equity, iv, rv, spread, inPosition });
  }

  sendJson(response, 200, {
    mode: "public-deribit-dvol-coinbase-rv-proxy",
    currency,
    days,
    frequency: "1h Deribit BTC volatility index with 1h Coinbase realized volatility",
    rows: { volatilityIndex: volRows.length, spotCandles: spotCandles.length, points: points.length },
    assumptions: { notionalFraction, entrySpread, exitSpread, feeDragAnnual, rvWindowHours },
    metrics: { ...summarizeReturns(points, capital), trades },
    points
  });
}

function summarizeReturns(points, capital) {
  if (points.length < 2) {
    return { pnl: 0, annReturn: 0, annVol: 0, maxDrawdown: 0, sharpe: 0 };
  }

  let peak = capital;
  let maxDrawdown = 0;
  const returns = [];
  points.forEach((point, index) => {
    peak = Math.max(peak, point.equity);
    maxDrawdown = Math.min(maxDrawdown, point.equity / peak - 1);
    if (index > 0) {
      returns.push(point.equity / points[index - 1].equity - 1);
    }
  });

  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) /
    Math.max(1, returns.length - 1);
  const days = Math.max(1, (points[points.length - 1].time - points[0].time) / 86400000);
  const annReturn = Math.pow(points[points.length - 1].equity / capital, 365 / days) - 1;
  const avgIntervalMs =
    (points[points.length - 1].time - points[0].time) / Math.max(1, points.length - 1);
  const periodsPerYear = (365 * 86400000) / Math.max(1, avgIntervalMs);
  const annVol = Math.sqrt(variance) * Math.sqrt(periodsPerYear);
  return {
    pnl: points[points.length - 1].equity - capital,
    annReturn,
    annVol,
    maxDrawdown,
    sharpe: annVol === 0 ? 0 : annReturn / annVol
  };
}

function summarizeStrategyPoints(points, capital) {
  const base = summarizeReturns(points, capital);
  if (points.length < 2) {
    return {
      ...base,
      cagr: base.annReturn,
      sortino: 0,
      calmar: 0,
      winRateByMonth: 0,
      worstMonths: [],
      performanceByYear: []
    };
  }

  const monthly = new Map();
  const yearly = new Map();
  points.forEach((point, index) => {
    if (index === 0) return;
    const previous = points[index - 1];
    const ret = point.equity / previous.equity - 1;
    const date = point.date || new Date(point.time).toISOString().slice(0, 10);
    const month = date.slice(0, 7);
    const year = date.slice(0, 4);
    monthly.set(month, (1 + (monthly.get(month) || 0)) * (1 + ret) - 1);
    yearly.set(year, (1 + (yearly.get(year) || 0)) * (1 + ret) - 1);
  });

  const dailyReturns = points.slice(1).map((point, index) => point.equity / points[index].equity - 1);
  const downside = dailyReturns.filter((value) => value < 0);
  const downsideVariance =
    downside.reduce((sum, value) => sum + value * value, 0) / Math.max(1, downside.length - 1);
  const downsideVol = Math.sqrt(downsideVariance) * Math.sqrt(252);
  const monthlyReturns = [...monthly.entries()].map(([month, ret]) => ({ month, ret }));
  const yearlyReturns = [...yearly.entries()].map(([year, ret]) => ({ year, ret }));

  return {
    ...base,
    cagr: base.annReturn,
    sortino: downsideVol === 0 ? 0 : base.annReturn / downsideVol,
    calmar: base.maxDrawdown === 0 ? 0 : base.annReturn / Math.abs(base.maxDrawdown),
    winRateByMonth: monthlyReturns.filter((row) => row.ret > 0).length / Math.max(1, monthlyReturns.length),
    worstMonths: monthlyReturns.slice().sort((a, b) => a.ret - b.ret).slice(0, 10),
    performanceByYear: yearlyReturns
  };
}

function yahooRangeForDays(days) {
  if (days <= 730) return "2y";
  if (days <= 1825) return "5y";
  if (days <= 3650) return "10y";
  return "max";
}

function movingAverage(prices, index, window) {
  if (index < window - 1) return null;
  let total = 0;
  for (let cursor = index - window + 1; cursor <= index; cursor += 1) {
    total += prices[cursor];
  }
  return total / window;
}

function simpleReturn(prices, index, window) {
  if (index < window || prices[index - window] <= 0) return null;
  return prices[index] / prices[index - window] - 1;
}

function realizedVolFromPrices(prices, index, window) {
  if (index < window) return null;
  const returns = [];
  for (let cursor = index - window + 1; cursor <= index; cursor += 1) {
    if (prices[cursor - 1] > 0 && prices[cursor] > 0) {
      returns.push(prices[cursor] / prices[cursor - 1] - 1);
    }
  }
  if (returns.length < 2) return null;
  const avg = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + Math.pow(value - avg, 2), 0) / Math.max(1, returns.length - 1);
  return Math.sqrt(variance) * Math.sqrt(252);
}

function monthlyLastTradingIndexes(rows, startIndex) {
  const indexes = [];
  for (let index = startIndex; index < rows.length; index += 1) {
    const month = rows[index].date.slice(0, 7);
    const nextMonth = rows[index + 1]?.date.slice(0, 7);
    if (month !== nextMonth) {
      indexes.push(index);
    }
  }
  return new Set(indexes);
}

function capWeightsToBil(weights, symbols, maxWeight, cashSymbol) {
  const capped = {};
  let invested = 0;
  symbols.forEach((symbol) => {
    const weight = Math.min(maxWeight, Math.max(0, weights[symbol] || 0));
    capped[symbol] = weight;
    invested += weight;
  });
  capped[cashSymbol] = Math.max(0, 1 - invested);
  return capped;
}

function normalizeObjectWeights(weights, symbols) {
  const total = symbols.reduce((sum, symbol) => sum + Math.max(0, weights[symbol] || 0), 0);
  if (total <= 0) {
    return {};
  }
  return Object.fromEntries(symbols.map((symbol) => [symbol, Math.max(0, weights[symbol] || 0) / total]));
}

function calculateDefensiveTrendWeights(aligned, symbols, riskySymbols, cashSymbol, equitySymbols, index, config) {
  const pricesBySymbol = Object.fromEntries(symbols.map((symbol) => [symbol, aligned.map((row) => row.prices[symbol])]));
  const eligible = [];
  riskySymbols.forEach((symbol) => {
    const prices = pricesBySymbol[symbol];
    const price = prices[index];
    const trendMa = movingAverage(prices, index, config.trendMaWindow);
    const momentum = simpleReturn(prices, index, config.momentumWindow);
    const vol = realizedVolFromPrices(prices, index, config.volWindow);
    const trendOk = !config.requirePriceAboveMa || (trendMa !== null && price > trendMa);
    const momentumOk = !config.requirePositiveMomentum || (momentum !== null && momentum > 0);
    if (trendOk && momentumOk && vol !== null && vol > 0) {
      eligible.push({ symbol, raw: 1 / vol, price, trendMa, momentum, vol });
    }
  });

  const rawTotal = eligible.reduce((sum, item) => sum + item.raw, 0);
  const rawWeights = {};
  eligible.forEach((item) => {
    rawWeights[item.symbol] = rawTotal > 0 ? item.raw / rawTotal : 0;
  });
  let weights = capWeightsToBil(rawWeights, riskySymbols, config.maxSingleEtfWeight, cashSymbol);

  const spyPrices = pricesBySymbol.SPY;
  const spyMa = movingAverage(spyPrices, index, config.trendMaWindow);
  const spyRiskOn = spyMa !== null && spyPrices[index] > spyMa;
  const equityCap = spyRiskOn ? config.equityCapRiskOn : config.equityCapRiskOff;
  const equityExposure = equitySymbols.reduce((sum, symbol) => sum + (weights[symbol] || 0), 0);
  if (equityExposure > equityCap) {
    const scale = equityCap / equityExposure;
    equitySymbols.forEach((symbol) => {
      weights[symbol] = (weights[symbol] || 0) * scale;
    });
    const total = symbols.reduce((sum, symbol) => sum + (symbol === cashSymbol ? 0 : weights[symbol] || 0), 0);
    weights[cashSymbol] = Math.max(0, 1 - total);
  }

  return {
    weights: Object.fromEntries(symbols.map((symbol) => [symbol, weights[symbol] || 0])),
    eligible: eligible.map((item) => item.symbol),
    regime: spyRiskOn ? "risk_on" : "risk_off",
    spyPrice: spyPrices[index],
    spyMa,
    equityCap
  };
}

function oneWayTurnover(previousWeights, targetWeights, symbols) {
  return symbols.reduce((sum, symbol) => sum + Math.abs((targetWeights[symbol] || 0) - (previousWeights[symbol] || 0)), 0) / 2;
}

function buildBenchmark(name, aligned, symbols, capital, targetWeightsByDate, costBps = 0) {
  let equity = capital;
  let weights = normalizeObjectWeights(targetWeightsByDate(aligned[0], 0), symbols);
  let costs = 0;
  let turnover = 0;
  let trades = 0;
  const rebalanceIndexes = monthlyLastTradingIndexes(aligned, 1);
  const points = [{ time: aligned[0].time, date: aligned[0].date, equity, weights }];
  for (let index = 1; index < aligned.length; index += 1) {
    const previous = aligned[index - 1];
    const current = aligned[index];
    const dailyReturn = symbols.reduce(
      (sum, symbol) => sum + (weights[symbol] || 0) * (current.prices[symbol] / previous.prices[symbol] - 1),
      0
    );
    equity *= 1 + dailyReturn;
    if (rebalanceIndexes.has(index)) {
      const targetWeights = normalizeObjectWeights(targetWeightsByDate(current, index), symbols);
      const rebalanceTurnover = oneWayTurnover(weights, targetWeights, symbols);
      const cost = equity * rebalanceTurnover * (costBps / 10000);
      equity -= cost;
      costs += cost;
      turnover += rebalanceTurnover;
      trades += 1;
      weights = targetWeights;
    }
    points.push({ time: current.time, date: current.date, equity, weights });
  }
  return { name, metrics: { ...summarizeStrategyPoints(points, capital), costs, turnover, trades }, points };
}

async function fetchYahooDaily(symbol, startTime, endTime, cacheMs, days) {
  const range = yahooRangeForDays(days);
  const payload = await fetchJson(
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
      symbol
    )}?range=${range}&interval=1d&events=history&includeAdjustedClose=true`,
    cacheMs
  );
  const result = payload.chart?.result?.[0];
  if (!result) {
    return [];
  }

  const timestamps = result.timestamp || [];
  const quote = result.indicators?.quote?.[0] || {};
  const adjclose = result.indicators?.adjclose?.[0]?.adjclose || [];
  const close = quote.close || [];
  const open = quote.open || [];
  const high = quote.high || [];
  const low = quote.low || [];
  const volume = quote.volume || [];

  return timestamps
    .map((timestamp, index) => {
      const time = Number(timestamp) * 1000;
      const adjustedClose = Number(adjclose[index]);
      return {
        time,
        date: new Date(time).toISOString().slice(0, 10),
        open: Number(open[index]),
        high: Number(high[index]),
        low: Number(low[index]),
        close: Number.isFinite(adjustedClose) ? adjustedClose : Number(close[index]),
        rawClose: Number(close[index]),
        volume: Number(volume[index] || 0)
      };
    })
    .filter((row) => row.time >= startTime && row.time <= endTime && Number.isFinite(row.close) && row.close > 0)
    .sort((a, b) => a.time - b.time);
}

function alignedEtfPriceRows(seriesBySymbol, symbols) {
  const maps = new Map();
  symbols.forEach((symbol) => {
    maps.set(
      symbol,
      new Map(seriesBySymbol[symbol].map((row) => [row.date, row]))
    );
  });

  return seriesBySymbol[symbols[0]]
    .map((row) => {
      const prices = {};
      const volumes = {};
      for (const symbol of symbols) {
        const match = maps.get(symbol).get(row.date);
        if (!match) {
          return null;
        }
        prices[symbol] = match.close;
        volumes[symbol] = match.volume;
      }
      return { time: row.time, date: row.date, prices, volumes };
    })
    .filter(Boolean);
}

function covarianceMatrix(returnRows, symbols, startIndex, endIndex) {
  const n = symbols.length;
  const means = Array(n).fill(0);
  const count = Math.max(1, endIndex - startIndex);
  for (let rowIndex = startIndex; rowIndex < endIndex; rowIndex += 1) {
    symbols.forEach((symbol, index) => {
      means[index] += returnRows[rowIndex].returns[symbol] / count;
    });
  }

  const matrix = Array.from({ length: n }, () => Array(n).fill(0));
  for (let rowIndex = startIndex; rowIndex < endIndex; rowIndex += 1) {
    for (let i = 0; i < n; i += 1) {
      for (let j = 0; j < n; j += 1) {
        matrix[i][j] +=
          ((returnRows[rowIndex].returns[symbols[i]] - means[i]) *
            (returnRows[rowIndex].returns[symbols[j]] - means[j])) /
          Math.max(1, count - 1);
      }
    }
  }
  return matrix;
}

function normalizeWeights(weights) {
  const clipped = weights.map((value) => Math.max(0, Number.isFinite(value) ? value : 0));
  const total = clipped.reduce((sum, value) => sum + value, 0);
  if (total <= 0) {
    return weights.map(() => 1 / weights.length);
  }
  return clipped.map((value) => value / total);
}

function inverseVolWeights(covariance) {
  const raw = covariance.map((row, index) => 1 / Math.sqrt(Math.max(1e-10, row[index])));
  return normalizeWeights(raw);
}

function equalRiskContributionWeights(covariance, maxIterations = 200) {
  const n = covariance.length;
  let weights = inverseVolWeights(covariance);

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    const marginal = covariance.map((row) => row.reduce((sum, value, index) => sum + value * weights[index], 0));
    const portfolioVariance = Math.max(
      1e-12,
      weights.reduce((sum, weight, index) => sum + weight * marginal[index], 0)
    );
    const riskContributions = weights.map((weight, index) => (weight * marginal[index]) / portfolioVariance);
    const adjusted = weights.map((weight, index) => {
      const ratio = (1 / n) / Math.max(1e-6, riskContributions[index]);
      return weight * Math.pow(ratio, 0.35);
    });
    weights = normalizeWeights(adjusted);
  }

  return weights;
}

function constrainedWeights(weights, maxWeight) {
  let constrained = weights.slice();
  for (let iteration = 0; iteration < 20; iteration += 1) {
    let excess = 0;
    constrained = constrained.map((weight) => {
      if (weight > maxWeight) {
        excess += weight - maxWeight;
        return maxWeight;
      }
      return weight;
    });
    const openIndexes = constrained
      .map((weight, index) => ({ weight, index }))
      .filter((item) => item.weight < maxWeight - 1e-9)
      .map((item) => item.index);
    if (excess <= 1e-9 || !openIndexes.length) {
      break;
    }
    const openTotal = openIndexes.reduce((sum, index) => sum + constrained[index], 0);
    openIndexes.forEach((index) => {
      constrained[index] += excess * (openTotal > 0 ? constrained[index] / openTotal : 1 / openIndexes.length);
    });
  }
  return normalizeWeights(constrained);
}

function parseSimpleCsv(text) {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return [];
  const headers = lines[0].split(",").map((header) => header.trim());
  return lines.slice(1).map((line) => {
    const values = [];
    let current = "";
    let quoted = false;
    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      if (char === '"') {
        quoted = !quoted;
      } else if (char === "," && !quoted) {
        values.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    values.push(current.trim());
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
}

function readCsvIfExists(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return parseSimpleCsv(fs.readFileSync(filePath, "utf8"));
}

function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length);
}

function variance(values) {
  const avg = mean(values);
  return values.reduce((sum, value) => sum + Math.pow(value - avg, 2), 0) / Math.max(1, values.length - 1);
}

function linearRegressionBeta(x, y) {
  const avgX = mean(x);
  const avgY = mean(y);
  const covariance = x.reduce((sum, value, index) => sum + (value - avgX) * (y[index] - avgY), 0);
  const varX = x.reduce((sum, value) => sum + Math.pow(value - avgX, 2), 0);
  return varX <= 1e-12 ? 1 : covariance / varX;
}

function standardDeviation(values) {
  return Math.sqrt(Math.max(0, variance(values)));
}

function nearestPriceRow(rows, time) {
  let best = null;
  for (const row of rows) {
    if (row.time <= time) best = row;
    else break;
  }
  return best;
}

function quantile(values, probability) {
  const sorted = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
  if (!sorted.length) {
    return 0;
  }
  const index = (sorted.length - 1) * probability;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) {
    return sorted[lower];
  }
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower);
}

function logReturn(prices, index, lag) {
  if (index < lag || prices[index - lag] <= 0 || prices[index] <= 0) {
    return null;
  }
  return Math.log(prices[index] / prices[index - lag]);
}

function rollingRealizedVol(prices, index, window) {
  if (index < window) {
    return null;
  }
  const returns = [];
  for (let offset = index - window + 1; offset <= index; offset += 1) {
    const value = logReturn(prices, offset, 1);
    if (value !== null) {
      returns.push(value);
    }
  }
  if (returns.length < 2) {
    return null;
  }
  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance =
    returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) /
    Math.max(1, returns.length - 1);
  return Math.sqrt(variance) * Math.sqrt(252);
}

function rollingDrawdown(prices, index, window) {
  if (index < window || prices[index] <= 0) {
    return null;
  }
  let peak = 0;
  for (let cursor = index - window + 1; cursor <= index; cursor += 1) {
    peak = Math.max(peak, prices[cursor]);
  }
  return peak > 0 ? prices[index] / peak - 1 : null;
}

function makeHmmFeatureRows(alignedRows) {
  const spy = alignedRows.map((row) => row.prices.SPY);
  const tlt = alignedRows.map((row) => row.prices.TLT);
  const features = [];
  for (let index = 63; index < alignedRows.length; index += 1) {
    const eqRet5 = logReturn(spy, index, 5);
    const eqVol20 = rollingRealizedVol(spy, index, 20);
    const eqDd63 = rollingDrawdown(spy, index, 63);
    const bondRet5 = logReturn(tlt, index, 5);
    if ([eqRet5, eqVol20, eqDd63, bondRet5].every((value) => value !== null && Number.isFinite(value))) {
      features.push({
        time: alignedRows[index].time,
        date: alignedRows[index].date,
        values: [eqRet5, Math.log(Math.max(1e-8, eqVol20)), eqDd63, bondRet5]
      });
    }
  }
  return features;
}

function standardizeTrainingFeatures(rows) {
  const dimensions = rows[0].values.length;
  const lower = [];
  const upper = [];
  const means = [];
  const stds = [];
  for (let dimension = 0; dimension < dimensions; dimension += 1) {
    const values = rows.map((row) => row.values[dimension]);
    lower[dimension] = quantile(values, 0.01);
    upper[dimension] = quantile(values, 0.99);
    const clipped = values.map((value) => Math.min(upper[dimension], Math.max(lower[dimension], value)));
    means[dimension] = clipped.reduce((sum, value) => sum + value, 0) / clipped.length;
    const variance =
      clipped.reduce((sum, value) => sum + Math.pow(value - means[dimension], 2), 0) /
      Math.max(1, clipped.length - 1);
    stds[dimension] = Math.sqrt(Math.max(1e-8, variance));
  }

  const transform = (values) =>
    values.map((value, dimension) => {
      const clipped = Math.min(upper[dimension], Math.max(lower[dimension], value));
      return (clipped - means[dimension]) / stds[dimension];
    });

  return {
    x: rows.map((row) => transform(row.values)),
    transform
  };
}

function seededRandom(seed) {
  let state = Math.max(1, seed + 1);
  return () => {
    state = (state * 48271) % 0x7fffffff;
    return state / 0x7fffffff;
  };
}

function gaussianLogDensityDiag(vector, mean, variance) {
  let value = 0;
  for (let dimension = 0; dimension < vector.length; dimension += 1) {
    const varValue = Math.max(1e-6, variance[dimension]);
    const diff = vector[dimension] - mean[dimension];
    value += -0.5 * (Math.log(2 * Math.PI * varValue) + (diff * diff) / varValue);
  }
  return value;
}

function logSumExp(values) {
  const max = Math.max(...values);
  if (!Number.isFinite(max)) {
    return max;
  }
  return max + Math.log(values.reduce((sum, value) => sum + Math.exp(value - max), 0));
}

function initialHmmParams(x, states, seed) {
  const random = seededRandom(seed + 17);
  const dimensions = x[0].length;
  const means = [];
  for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
    means.push(x[Math.floor(random() * x.length)].slice());
  }
  const variances = Array.from({ length: states }, () => Array(dimensions).fill(1));
  const transition = Array.from({ length: states }, (_, row) =>
    Array.from({ length: states }, (_, col) => (row === col ? 0.9 : 0.1 / Math.max(1, states - 1)))
  );
  const start = Array(states).fill(1 / states);
  return { start, transition, means, variances };
}

function forwardBackwardHmm(x, params) {
  const n = x.length;
  const states = params.start.length;
  const logEmission = Array.from({ length: n }, () => Array(states).fill(0));
  for (let time = 0; time < n; time += 1) {
    for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
      logEmission[time][stateIndex] = gaussianLogDensityDiag(
        x[time],
        params.means[stateIndex],
        params.variances[stateIndex]
      );
    }
  }

  const alpha = Array.from({ length: n }, () => Array(states).fill(-Infinity));
  for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
    alpha[0][stateIndex] = Math.log(Math.max(1e-12, params.start[stateIndex])) + logEmission[0][stateIndex];
  }
  for (let time = 1; time < n; time += 1) {
    for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
      alpha[time][stateIndex] =
        logEmission[time][stateIndex] +
        logSumExp(
          alpha[time - 1].map(
            (value, previousState) => value + Math.log(Math.max(1e-12, params.transition[previousState][stateIndex]))
          )
        );
    }
  }

  const beta = Array.from({ length: n }, () => Array(states).fill(0));
  for (let time = n - 2; time >= 0; time -= 1) {
    for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
      beta[time][stateIndex] = logSumExp(
        beta[time + 1].map(
          (value, nextState) =>
            Math.log(Math.max(1e-12, params.transition[stateIndex][nextState])) +
            logEmission[time + 1][nextState] +
            value
        )
      );
    }
  }

  const logLikelihood = logSumExp(alpha[n - 1]);
  const gamma = Array.from({ length: n }, () => Array(states).fill(0));
  for (let time = 0; time < n; time += 1) {
    const row = alpha[time].map((value, stateIndex) => value + beta[time][stateIndex] - logLikelihood);
    const total = row.reduce((sum, value) => sum + Math.exp(value), 0);
    for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
      gamma[time][stateIndex] = Math.exp(row[stateIndex]) / Math.max(1e-12, total);
    }
  }

  const xiSums = Array.from({ length: states }, () => Array(states).fill(0));
  for (let time = 0; time < n - 1; time += 1) {
    const terms = [];
    for (let from = 0; from < states; from += 1) {
      for (let to = 0; to < states; to += 1) {
        terms.push(
          alpha[time][from] +
            Math.log(Math.max(1e-12, params.transition[from][to])) +
            logEmission[time + 1][to] +
            beta[time + 1][to]
        );
      }
    }
    const normalizer = logSumExp(terms);
    for (let from = 0; from < states; from += 1) {
      for (let to = 0; to < states; to += 1) {
        xiSums[from][to] += Math.exp(
          alpha[time][from] +
            Math.log(Math.max(1e-12, params.transition[from][to])) +
            logEmission[time + 1][to] +
            beta[time + 1][to] -
            normalizer
        );
      }
    }
  }

  return { gamma, xiSums, logLikelihood };
}

function fitGaussianHmmDiag(x, states, seed, maxIterations = 80, tolerance = 1e-4) {
  let params = initialHmmParams(x, states, seed);
  let previousLogLikelihood = -Infinity;
  let current = null;
  const dimensions = x[0].length;

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    current = forwardBackwardHmm(x, params);
    const { gamma, xiSums, logLikelihood } = current;
    const start = normalizeWeights(gamma[0]);
    const transition = Array.from({ length: states }, (_, from) => {
      const row = Array(states)
        .fill(0)
        .map((_, to) => xiSums[from][to] + (from === to ? 9 : 1));
      return normalizeWeights(row);
    });

    const means = Array.from({ length: states }, () => Array(dimensions).fill(0));
    const variances = Array.from({ length: states }, () => Array(dimensions).fill(0));
    for (let stateIndex = 0; stateIndex < states; stateIndex += 1) {
      const weightSum = Math.max(1e-8, gamma.reduce((sum, row) => sum + row[stateIndex], 0));
      for (let dimension = 0; dimension < dimensions; dimension += 1) {
        means[stateIndex][dimension] =
          x.reduce((sum, row, rowIndex) => sum + gamma[rowIndex][stateIndex] * row[dimension], 0) / weightSum;
      }
      for (let dimension = 0; dimension < dimensions; dimension += 1) {
        variances[stateIndex][dimension] =
          x.reduce(
            (sum, row, rowIndex) =>
              sum + gamma[rowIndex][stateIndex] * Math.pow(row[dimension] - means[stateIndex][dimension], 2),
            0
          ) /
            weightSum +
          1e-5;
      }
    }

    params = { start, transition, means, variances };
    if (Math.abs(logLikelihood - previousLogLikelihood) < tolerance) {
      break;
    }
    previousLogLikelihood = logLikelihood;
  }

  current = forwardBackwardHmm(x, params);
  const parameterCount = states - 1 + states * (states - 1) + states * dimensions * 2;
  const bic = parameterCount * Math.log(x.length) - 2 * current.logLikelihood;
  return { ...params, logLikelihood: current.logLikelihood, bic, gamma: current.gamma };
}

function fitBestHmm(x, states = 2, restarts = 8) {
  let best = null;
  for (let seed = 0; seed < restarts; seed += 1) {
    const candidate = fitGaussianHmmDiag(x, states, seed);
    if (!best || candidate.bic < best.bic) {
      best = candidate;
    }
  }
  return best;
}

function labelHmmStates(model, trainRows) {
  const stateStats = model.means.map((mean, stateIndex) => {
    const weightSum = Math.max(1e-8, model.gamma.reduce((sum, row) => sum + row[stateIndex], 0));
    const eqReturn = trainRows.reduce((sum, row, index) => sum + model.gamma[index][stateIndex] * row.values[0], 0) / weightSum;
    const drawdown = trainRows.reduce((sum, row, index) => sum + model.gamma[index][stateIndex] * row.values[2], 0) / weightSum;
    const vol = mean[1];
    return { stateIndex, score: eqReturn + drawdown - 0.35 * vol, eqReturn, drawdown, vol };
  });
  stateStats.sort((a, b) => b.score - a.score);
  return {
    riskOnState: stateStats[0].stateIndex,
    defensiveState: stateStats[stateStats.length - 1].stateIndex,
    stateStats
  };
}

function latestHmmRegime(model, labels) {
  const latest = model.gamma[model.gamma.length - 1];
  const rawState = latest.indexOf(Math.max(...latest));
  return {
    rawState,
    regime: rawState === labels.riskOnState ? "risk_on" : "defensive",
    confidence: latest[rawState],
    probabilities: latest
  };
}

function regimeRiskBudgets(symbols, regime) {
  const riskOn = { SPY: 0.35, TLT: 0.2, IEF: 0.1, TIP: 0.1, IAU: 0.1, DBC: 0.15, SHY: 0 };
  const defensive = { SPY: 0.1, TLT: 0.3, IEF: 0.2, TIP: 0.15, IAU: 0.2, DBC: 0.05, SHY: 0 };
  const template = regime === "risk_on" ? riskOn : defensive;
  const raw = symbols.map((symbol) => template[symbol] ?? 0);
  return normalizeWeights(raw);
}

function overlayRiskParityWeights(covariance, symbols, regime, maxWeight) {
  const baseWeights = equalRiskContributionWeights(covariance);
  const budgets = regimeRiskBudgets(symbols, regime);
  const tilted = normalizeWeights(baseWeights.map((weight, index) => weight * Math.sqrt(Math.max(0.01, budgets[index]))));
  return constrainedWeights(tilted, maxWeight);
}

async function handleRiskParityEtfBacktest(url, response) {
  const riskySymbols = (url.searchParams.get("risky") || "SPY,QQQ,IWM,EFA,EEM,TLT,IEF,GLD,DBC,VNQ")
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean)
    .slice(0, 12);
  const cashSymbol = (url.searchParams.get("cash") || "BIL").trim().toUpperCase();
  const symbols = [...new Set(riskySymbols.concat([cashSymbol]))];
  const equitySymbols = (url.searchParams.get("equity") || "SPY,QQQ,IWM,EFA,EEM,VNQ")
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter((symbol) => symbols.includes(symbol));
  const defensiveSymbols = (url.searchParams.get("defensive") || "TLT,IEF,GLD,BIL")
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter((symbol) => symbols.includes(symbol));
  const days = Math.min(7000, Math.max(252, Number(url.searchParams.get("days") || 5000)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const config = {
    trendMaWindow: Math.min(300, Math.max(40, Number(url.searchParams.get("trendMaWindow") || 200))),
    momentumWindow: Math.min(252, Math.max(20, Number(url.searchParams.get("momentumWindow") || 126))),
    volWindow: Math.min(252, Math.max(20, Number(url.searchParams.get("volWindow") || 63))),
    requirePriceAboveMa: url.searchParams.get("requirePriceAboveMa") !== "false",
    requirePositiveMomentum: url.searchParams.get("requirePositiveMomentum") !== "false",
    maxSingleEtfWeight: Math.min(0.8, Math.max(0.05, Number(url.searchParams.get("maxSingleEtfWeight") || 0.25))),
    equityCapRiskOn: Math.min(1, Math.max(0, Number(url.searchParams.get("equityCapRiskOn") || 0.8))),
    equityCapRiskOff: Math.min(1, Math.max(0, Number(url.searchParams.get("equityCapRiskOff") || 0.3))),
    transactionCostBps: Math.max(0, Number(url.searchParams.get("transactionCostBps") || 10))
  };
  const lookback = Math.max(config.trendMaWindow, config.momentumWindow, config.volWindow);
  const endTime = Date.now();
  const startTime = Date.parse(url.searchParams.get("startDate") || "2007-01-01");
  const cacheMs = 6 * 60 * 60 * 1000;

  if (!symbols.includes("SPY") || !symbols.includes(cashSymbol)) {
    sendJson(response, 400, { error: "Defensive Trend + Risk-Parity ETF requires SPY and the cash ETF." });
    return;
  }

  const seriesBySymbol = {};
  await Promise.all(
    symbols.map(async (symbol) => {
      seriesBySymbol[symbol] = await fetchYahooDaily(symbol, startTime, endTime, cacheMs, days + lookback + 365);
    })
  );

  const missing = symbols.filter((symbol) => seriesBySymbol[symbol].length < lookback + 30);
  if (missing.length) {
    sendJson(response, 502, {
      error: `Not enough Yahoo daily adjusted history for: ${missing.join(", ")}`,
      rows: Object.fromEntries(symbols.map((symbol) => [symbol, seriesBySymbol[symbol].length]))
    });
    return;
  }

  const aligned = alignedEtfPriceRows(seriesBySymbol, symbols).slice(-(days + lookback + 1));
  if (aligned.length < lookback + 30) {
    sendJson(response, 502, { error: "Not enough overlapping ETF history after date alignment." });
    return;
  }

  let equity = capital;
  let weights = Object.fromEntries(symbols.map((symbol) => [symbol, symbol === cashSymbol ? 1 : 0]));
  let trades = 0;
  let costs = 0;
  let turnover = 0;
  const points = [];
  const weightHistory = [];
  const signalHistory = [];
  const startIndex = Math.min(aligned.length - 2, lookback);
  const rebalanceIndexes = monthlyLastTradingIndexes(aligned, startIndex);
  const initialSignal = calculateDefensiveTrendWeights(
    aligned,
    symbols,
    riskySymbols,
    cashSymbol,
    equitySymbols,
    startIndex,
    config
  );
  weights = initialSignal.weights;
  points.push({
    time: aligned[startIndex].time,
    date: aligned[startIndex].date,
    equity,
    weights,
    regime: initialSignal.regime,
    equityExposure: equitySymbols.reduce((sum, symbol) => sum + (weights[symbol] || 0), 0),
    cashAllocation: weights[cashSymbol] || 0,
    prices: aligned[startIndex].prices
  });

  for (let index = startIndex + 1; index < aligned.length; index += 1) {
    const previous = aligned[index - 1];
    const row = aligned[index];
    const investedReturn = symbols.reduce(
      (sum, symbol) => sum + (weights[symbol] || 0) * (row.prices[symbol] / previous.prices[symbol] - 1),
      0
    );
    equity *= 1 + investedReturn;
    points.push({
      time: row.time,
      date: row.date,
      equity,
      weights,
      regime: points[points.length - 1]?.regime || initialSignal.regime,
      equityExposure: equitySymbols.reduce((sum, symbol) => sum + (weights[symbol] || 0), 0),
      cashAllocation: weights[cashSymbol] || 0,
      prices: row.prices
    });

    if (rebalanceIndexes.has(index)) {
      const signal = calculateDefensiveTrendWeights(
        aligned,
        symbols,
        riskySymbols,
        cashSymbol,
        equitySymbols,
        index,
        config
      );
      const rebalanceTurnover = oneWayTurnover(weights, signal.weights, symbols);
      const tradeCost = equity * rebalanceTurnover * (config.transactionCostBps / 10000);
      equity -= tradeCost;
      costs += tradeCost;
      turnover += rebalanceTurnover;
      trades += 1;
      weights = signal.weights;
      const historyRow = {
        time: row.time,
        date: row.date,
        weights,
        turnover: rebalanceTurnover,
        eligible: signal.eligible,
        regime: signal.regime,
        spyPrice: signal.spyPrice,
        spyMa: signal.spyMa,
        equityCap: signal.equityCap,
        equityExposure: equitySymbols.reduce((sum, symbol) => sum + (weights[symbol] || 0), 0),
        cashAllocation: weights[cashSymbol] || 0
      };
      weightHistory.push(historyRow);
      signalHistory.push(historyRow);
      points[points.length - 1] = { ...points[points.length - 1], equity, weights, regime: signal.regime };
    }
  }

  const testAligned = aligned.slice(startIndex);
  const spyBenchmark = buildBenchmark("SPY Buy & Hold", testAligned, symbols, capital, () => ({ SPY: 1 }));
  const sixtyFortyBenchmark = buildBenchmark("60/40 SPY/IEF", testAligned, symbols, capital, () => ({ SPY: 0.6, IEF: 0.4 }));
  const equalWeightBenchmark = buildBenchmark(
    "Equal-Weight Universe Monthly",
    testAligned,
    symbols,
    capital,
    () => Object.fromEntries(symbols.map((symbol) => [symbol, 1 / symbols.length])),
    config.transactionCostBps
  );
  const bilBenchmark = buildBenchmark("BIL Cash", testAligned, symbols, capital, () => ({ [cashSymbol]: 1 }));
  const metrics = {
    ...summarizeStrategyPoints(points, capital),
    trades,
    costs,
    turnover,
    avgTurnover: trades ? turnover / trades : 0,
    numberOfTrades: trades,
    averageCashAllocation: points.reduce((sum, point) => sum + (point.cashAllocation || 0), 0) / Math.max(1, points.length),
    averageEquityExposure: points.reduce((sum, point) => sum + (point.equityExposure || 0), 0) / Math.max(1, points.length),
    spyBenchmarkMaxDrawdown: spyBenchmark.metrics.maxDrawdown,
    drawdownImprovedVsSpy: summarizeStrategyPoints(points, capital).maxDrawdown > spyBenchmark.metrics.maxDrawdown
  };

  sendJson(response, 200, {
    mode: "public-yahoo-defensive-trend-risk-parity-etf",
    strategy: "Defensive Trend + Risk-Parity ETF Allocation",
    symbols,
    riskySymbols,
    cashSymbol,
    equitySymbols,
    defensiveSymbols,
    days,
    frequency: "daily Yahoo adjusted ETF closes, last-trading-day monthly rebalance",
    rows: {
      aligned: aligned.length,
      bySymbol: Object.fromEntries(symbols.map((symbol) => [symbol, seriesBySymbol[symbol].length]))
    },
    assumptions: { capital, startDate: "2007-01-01", rebalance: "M", ...config },
    metrics,
    benchmark: spyBenchmark,
    benchmarks: [spyBenchmark, sixtyFortyBenchmark, equalWeightBenchmark, bilBenchmark],
    latestRegime: weightHistory.length ? weightHistory[weightHistory.length - 1] : null,
    latestWeights: points.length ? points[points.length - 1].weights : {},
    weightHistory,
    signalHistory,
    points
  });
}

async function handleHmmEtfBacktest(url, response) {
  const symbols = (url.searchParams.get("symbols") || "SPY,TLT,IEF,TIP,IAU,DBC,SHY")
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean)
    .slice(0, 10);
  const required = ["SPY", "TLT"];
  const missingRequired = required.filter((symbol) => !symbols.includes(symbol));
  if (missingRequired.length) {
    sendJson(response, 400, { error: `HMM MVP requires ${missingRequired.join(", ")} in the universe.` });
    return;
  }

  const days = Math.min(1825, Math.max(252, Number(url.searchParams.get("days") || 730)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const trainWindow = Math.min(1500, Math.max(504, Number(url.searchParams.get("trainWindow") || 1000)));
  const covLookback = Math.min(252, Math.max(40, Number(url.searchParams.get("covLookback") || 126)));
  const rebalanceEvery = Math.min(63, Math.max(10, Number(url.searchParams.get("rebalanceEvery") || 21)));
  const maxWeight = Math.min(0.8, Math.max(0.2, Number(url.searchParams.get("maxWeight") || 0.45)));
  const feeBps = Math.max(0, Number(url.searchParams.get("feeBps") || 1));
  const slippageBps = Math.max(0, Number(url.searchParams.get("slippageBps") || 2));
  const restarts = Math.min(20, Math.max(2, Number(url.searchParams.get("restarts") || 8)));
  const endTime = Date.now();
  const startTime = endTime - (days + trainWindow + 90) * 86400000;
  const cacheMs = 6 * 60 * 60 * 1000;

  const seriesBySymbol = {};
  await Promise.all(
    symbols.map(async (symbol) => {
      seriesBySymbol[symbol] = await fetchYahooDaily(symbol, startTime, endTime, cacheMs, days + trainWindow + 90);
    })
  );

  const missing = symbols.filter((symbol) => seriesBySymbol[symbol].length < Math.min(trainWindow, 756));
  if (missing.length) {
    sendJson(response, 502, {
      error: `Not enough Yahoo daily adjusted history for: ${missing.join(", ")}`,
      rows: Object.fromEntries(symbols.map((symbol) => [symbol, seriesBySymbol[symbol].length]))
    });
    return;
  }

  const aligned = alignedEtfPriceRows(seriesBySymbol, symbols);
  const returnRows = [];
  for (let index = 1; index < aligned.length; index += 1) {
    const returns = {};
    symbols.forEach((symbol) => {
      returns[symbol] = aligned[index].prices[symbol] / aligned[index - 1].prices[symbol] - 1;
    });
    returnRows.push({ time: aligned[index].time, date: aligned[index].date, returns, prices: aligned[index].prices });
  }

  const featureRows = makeHmmFeatureRows(aligned);
  const featureIndexByDate = new Map(featureRows.map((row, index) => [row.date, index]));
  let equity = capital;
  let weights = symbols.map(() => 1 / symbols.length);
  let trades = 0;
  let costs = 0;
  let turnover = 0;
  let regimeSwitches = 0;
  let previousRegime = null;
  let benchmarkEquity = capital;
  const benchmarkWeights = symbols.map(() => 1 / symbols.length);
  const benchmarkPoints = [];
  let spyBenchmarkEquity = capital;
  const spyBenchmarkPoints = [];
  const points = [];
  const regimeHistory = [];
  const weightHistory = [];
  const startReturnIndex = Math.max(covLookback, trainWindow + 63, returnRows.length - days);

  for (let index = startReturnIndex; index < returnRows.length; index += 1) {
    const row = returnRows[index];
    const featureIndex = featureIndexByDate.get(row.date);
    if (featureIndex === undefined) {
      continue;
    }

    if ((index - startReturnIndex) % rebalanceEvery === 0 && featureIndex >= trainWindow) {
      const trainRows = featureRows.slice(featureIndex - trainWindow, featureIndex);
      const standardized = standardizeTrainingFeatures(trainRows);
      const model = fitBestHmm(standardized.x, 2, restarts);
      const labels = labelHmmStates(model, trainRows);
      const regimeInfo = latestHmmRegime(model, labels);
      const covariance = covarianceMatrix(returnRows, symbols, index - covLookback, index);
      const targetWeights = overlayRiskParityWeights(covariance, symbols, regimeInfo.regime, maxWeight);
      const oneWayTurnover =
        targetWeights.reduce((sum, weight, weightIndex) => sum + Math.abs(weight - weights[weightIndex]), 0) / 2;
      const tradeCost = equity * oneWayTurnover * ((feeBps + slippageBps) / 10000);
      equity -= tradeCost;
      costs += tradeCost;
      turnover += oneWayTurnover;
      trades += 1;
      if (previousRegime && previousRegime !== regimeInfo.regime) {
        regimeSwitches += 1;
      }
      previousRegime = regimeInfo.regime;
      weights = targetWeights;

      const historyRow = {
        time: row.time,
        date: row.date,
        regime: regimeInfo.regime,
        confidence: regimeInfo.confidence,
        rawState: regimeInfo.rawState,
        probabilities: regimeInfo.probabilities,
        bic: model.bic,
        stateStats: labels.stateStats,
        weights: Object.fromEntries(symbols.map((symbol, weightIndex) => [symbol, weights[weightIndex]])),
        turnover: oneWayTurnover
      };
      regimeHistory.push(historyRow);
      weightHistory.push(historyRow);
    }

    const portfolioReturn = symbols.reduce(
      (sum, symbol, weightIndex) => sum + weights[weightIndex] * row.returns[symbol],
      0
    );
    equity *= 1 + portfolioReturn;
    const benchmarkReturn = symbols.reduce(
      (sum, symbol, weightIndex) => sum + benchmarkWeights[weightIndex] * row.returns[symbol],
      0
    );
    benchmarkEquity *= 1 + benchmarkReturn;
    const spyReturn = row.returns.SPY ?? 0;
    spyBenchmarkEquity *= 1 + spyReturn;
    points.push({
      time: row.time,
      date: row.date,
      equity,
      regime: previousRegime,
      weights: Object.fromEntries(symbols.map((symbol, weightIndex) => [symbol, weights[weightIndex]])),
      prices: row.prices
    });
    benchmarkPoints.push({
      time: row.time,
      date: row.date,
      equity: benchmarkEquity,
      weights: Object.fromEntries(symbols.map((symbol, weightIndex) => [symbol, benchmarkWeights[weightIndex]]))
    });
    spyBenchmarkPoints.push({
      time: row.time,
      date: row.date,
      equity: spyBenchmarkEquity,
      weights: { SPY: 1 }
    });
  }

  const strategyMetrics = {
    ...summarizeReturns(points, capital),
    trades,
    costs,
    turnover,
    avgTurnover: trades ? turnover / trades : 0,
    regimeSwitches
  };
  const benchmarkMetrics = summarizeReturns(benchmarkPoints, capital);
  const spyBenchmarkMetrics = summarizeReturns(spyBenchmarkPoints, capital);

  sendJson(response, 200, {
    mode: "public-yahoo-hmm-etf-regime",
    strategy: "HMM Regime ETF Allocation",
    symbols,
    days,
    frequency: "daily Yahoo adjusted ETF closes, monthly 2-state Gaussian diag HMM overlay",
    rows: {
      aligned: aligned.length,
      returns: returnRows.length,
      features: featureRows.length,
      bySymbol: Object.fromEntries(symbols.map((symbol) => [symbol, seriesBySymbol[symbol].length]))
    },
    assumptions: {
      capital,
      states: 2,
      emission: "Gaussian diagonal",
      trainWindow,
      covLookback,
      rebalanceEvery,
      restarts,
      maxWeight,
      feeBps,
      slippageBps,
      features: ["SPY 5d log return", "log SPY 20d realized vol", "SPY 63d drawdown", "TLT 5d log return"],
      note: "Treasury curve features from the guidance are omitted in this MVP because this dashboard already has reliable ETF history but no Treasury curve connector yet."
    },
    metrics: {
      ...strategyMetrics,
      benchmarkPnl: benchmarkMetrics.pnl,
      benchmarkAnnReturn: benchmarkMetrics.annReturn,
      benchmarkAnnVol: benchmarkMetrics.annVol,
      benchmarkMaxDrawdown: benchmarkMetrics.maxDrawdown,
      benchmarkSharpe: benchmarkMetrics.sharpe,
      spyBenchmarkPnl: spyBenchmarkMetrics.pnl,
      spyBenchmarkAnnReturn: spyBenchmarkMetrics.annReturn,
      spyBenchmarkAnnVol: spyBenchmarkMetrics.annVol,
      spyBenchmarkMaxDrawdown: spyBenchmarkMetrics.maxDrawdown,
      spyBenchmarkSharpe: spyBenchmarkMetrics.sharpe,
      excessPnl: strategyMetrics.pnl - benchmarkMetrics.pnl,
      excessAnnReturn: strategyMetrics.annReturn - benchmarkMetrics.annReturn,
      excessPnlVsSpy: strategyMetrics.pnl - spyBenchmarkMetrics.pnl,
      excessAnnReturnVsSpy: strategyMetrics.annReturn - spyBenchmarkMetrics.annReturn
    },
    benchmark: {
      name: "Equal Weight Buy & Hold",
      description: "Equal-weight buy-and-hold benchmark using the same ETF universe and same test window.",
      weights: Object.fromEntries(symbols.map((symbol, weightIndex) => [symbol, benchmarkWeights[weightIndex]])),
      metrics: benchmarkMetrics,
      points: benchmarkPoints
    },
    benchmarks: [
      {
        name: "Equal Weight Buy & Hold",
        description: "Equal-weight buy-and-hold benchmark using the same ETF universe and same test window.",
        weights: Object.fromEntries(symbols.map((symbol, weightIndex) => [symbol, benchmarkWeights[weightIndex]])),
        metrics: benchmarkMetrics,
        points: benchmarkPoints
      },
      {
        name: "SPY Buy & Hold",
        description: "100% SPY buy-and-hold benchmark using the same test window.",
        weights: { SPY: 1 },
        metrics: spyBenchmarkMetrics,
        points: spyBenchmarkPoints
      }
    ],
    latestRegime: regimeHistory.length ? regimeHistory[regimeHistory.length - 1] : null,
    latestWeights: points.length ? points[points.length - 1].weights : {},
    regimeHistory,
    weightHistory,
    points
  });
}

async function handleEtfOverlapStatArbBacktest(url, response) {
  const pairText =
    url.searchParams.get("pairs") || "QQQ:VGT,QQQ:XLK,VGT:XLK,XLF:VFH,XLF:IYF,VFH:IYF,IVV:VOO,SPY:IVV";
  const pairs = pairText
    .split(",")
    .map((item) => item.split(":").map((part) => part.trim().toUpperCase()))
    .filter((pair) => pair.length === 2 && pair[0] && pair[1]);
  const symbols = [...new Set(pairs.flat().concat(["SPY"]))];
  const days = Math.min(3650, Math.max(252, Number(url.searchParams.get("days") || 1095)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const lookback = Math.min(252, Math.max(40, Number(url.searchParams.get("lookback") || 90)));
  const zEntry = Math.max(0.5, Number(url.searchParams.get("zEntry") || 2));
  const zExit = Math.max(0.1, Number(url.searchParams.get("zExit") || 0.5));
  const maxPairs = Math.min(10, Math.max(1, Number(url.searchParams.get("maxPairs") || 5)));
  const grossPerPair = Math.min(0.4, Math.max(0.02, Number(url.searchParams.get("grossPerPair") || 0.12)));
  const costBps = Math.max(0, Number(url.searchParams.get("costBps") || 4));
  const endTime = Date.now();
  const startTime = endTime - (days + lookback + 10) * 86400000;
  const cacheMs = 6 * 60 * 60 * 1000;

  const seriesBySymbol = {};
  await Promise.all(
    symbols.map(async (symbol) => {
      seriesBySymbol[symbol] = await fetchYahooDaily(symbol, startTime, endTime, cacheMs, days + lookback + 10);
    })
  );
  const missing = symbols.filter((symbol) => seriesBySymbol[symbol].length < lookback + 30);
  if (missing.length) {
    sendJson(response, 502, {
      error: `Not enough Yahoo daily adjusted history for: ${missing.join(", ")}`,
      rows: Object.fromEntries(symbols.map((symbol) => [symbol, seriesBySymbol[symbol].length]))
    });
    return;
  }

  const aligned = alignedEtfPriceRows(seriesBySymbol, symbols).slice(-(days + lookback + 1));
  const pairStates = new Map();
  pairs.forEach(([a, b]) => pairStates.set(`${a}/${b}`, { side: 0, beta: 1, z: 0, entryZ: null, trades: 0 }));
  let equity = capital;
  let benchmarkEquity = capital;
  let costs = 0;
  let trades = 0;
  const points = [];
  const benchmarkPoints = [];
  const tradeLog = [];

  for (let index = lookback; index < aligned.length - 1; index += 1) {
    const row = aligned[index];
    const next = aligned[index + 1];
    const signals = [];
    for (const [a, b] of pairs) {
      const spreadWindow = [];
      const aWindow = [];
      const bWindow = [];
      for (let cursor = index - lookback; cursor < index; cursor += 1) {
        aWindow.push(Math.log(aligned[cursor].prices[a]));
        bWindow.push(Math.log(aligned[cursor].prices[b]));
      }
      const beta = linearRegressionBeta(bWindow, aWindow);
      for (let cursor = index - lookback; cursor < index; cursor += 1) {
        spreadWindow.push(Math.log(aligned[cursor].prices[a]) - beta * Math.log(aligned[cursor].prices[b]));
      }
      const currentSpread = Math.log(row.prices[a]) - beta * Math.log(row.prices[b]);
      const z = (currentSpread - mean(spreadWindow)) / Math.max(1e-8, standardDeviation(spreadWindow));
      signals.push({ a, b, key: `${a}/${b}`, beta, z, absZ: Math.abs(z), spread: currentSpread });
    }

    signals.sort((left, right) => right.absZ - left.absZ);
    const activeKeys = new Set([...pairStates.entries()].filter(([, state]) => state.side !== 0).map(([key]) => key));
    const targetActive = new Set(activeKeys);
    for (const signal of signals) {
      const state = pairStates.get(signal.key);
      if (state.side !== 0 && Math.abs(signal.z) < zExit) {
        targetActive.delete(signal.key);
      } else if (state.side === 0 && targetActive.size < maxPairs && Math.abs(signal.z) > zEntry) {
        targetActive.add(signal.key);
      }
    }

    let dailyReturn = 0;
    const openPositions = [];
    for (const signal of signals) {
      const state = pairStates.get(signal.key);
      const shouldBeActive = targetActive.has(signal.key);
      const desiredSide = shouldBeActive ? (signal.z < 0 ? 1 : -1) : 0;
      if (desiredSide !== state.side) {
        const turnoverCost = equity * grossPerPair * (costBps / 10000);
        equity -= turnoverCost;
        costs += turnoverCost;
        trades += 1;
        state.trades += 1;
        tradeLog.push({
          time: row.time,
          date: row.date,
          pair: signal.key,
          action: desiredSide === 0 ? "exit" : desiredSide > 0 ? "long_spread" : "short_spread",
          z: signal.z,
          beta: signal.beta,
          cost: turnoverCost
        });
        state.side = desiredSide;
        state.entryZ = desiredSide === 0 ? null : signal.z;
      }
      state.beta = signal.beta;
      state.z = signal.z;
      if (state.side !== 0) {
        const aReturn = next.prices[signal.a] / row.prices[signal.a] - 1;
        const bReturn = next.prices[signal.b] / row.prices[signal.b] - 1;
        dailyReturn += grossPerPair * state.side * (aReturn - signal.beta * bReturn);
        openPositions.push({
          pair: signal.key,
          side: state.side > 0 ? "long spread" : "short spread",
          z: signal.z,
          beta: signal.beta
        });
      }
    }

    equity *= 1 + dailyReturn;
    benchmarkEquity *= 1 + (next.prices.SPY / row.prices.SPY - 1);
    points.push({ time: next.time, date: next.date, equity, openPositions: openPositions.length, dailyReturn });
    benchmarkPoints.push({ time: next.time, date: next.date, equity: benchmarkEquity });
  }

  const latestSignals = [...pairStates.entries()]
    .map(([pair, state]) => ({ pair, z: state.z, beta: state.beta, side: state.side }))
    .sort((a, b) => Math.abs(b.z) - Math.abs(a.z));
  const openPositions = latestSignals.filter((signal) => signal.side !== 0);
  const metrics = { ...summarizeReturns(points, capital), trades, costs, openPositions: openPositions.length };
  const benchmarkMetrics = summarizeReturns(benchmarkPoints, capital);
  sendJson(response, 200, {
    mode: "public-yahoo-etf-overlap-stat-arb",
    strategy: "ETF Holdings Overlap Stat Arb",
    days,
    frequency: "Daily Yahoo adjusted ETF closes with predefined high-overlap ETF pairs",
    assumptions: {
      pairs,
      lookback,
      zEntry,
      zExit,
      maxPairs,
      grossPerPair,
      costBps,
      note: "Holdings overlap is represented by curated issuer-style ETF pair candidates in this MVP; issuer holdings downloads can replace this list later."
    },
    rows: { aligned: aligned.length, points: points.length, pairs: pairs.length },
    metrics: {
      ...metrics,
      spyBenchmarkPnl: benchmarkMetrics.pnl,
      spyBenchmarkAnnReturn: benchmarkMetrics.annReturn,
      spyBenchmarkMaxDrawdown: benchmarkMetrics.maxDrawdown,
      excessPnlVsSpy: metrics.pnl - benchmarkMetrics.pnl,
      excessAnnReturnVsSpy: metrics.annReturn - benchmarkMetrics.annReturn
    },
    latestSignals,
    openPositions,
    trades: tradeLog.slice(-200),
    benchmarks: [
      {
        name: "SPY Buy & Hold",
        description: "100% SPY buy-and-hold benchmark over the same test window.",
        metrics: benchmarkMetrics,
        points: benchmarkPoints
      }
    ],
    points
  });
}

async function handleCongress13fBacktest(url, response) {
  const congressFile = path.join(root, "data", "alt", "congress_trades.csv");
  const holdingsFile = path.join(root, "data", "alt", "13f_changes.csv");
  const congressRows = readCsvIfExists(congressFile);
  const holdingRows = readCsvIfExists(holdingsFile);
  if (!congressRows || !holdingRows) {
    sendJson(response, 404, {
      error:
        "Congressional + 13F strategy needs local data files first: data/alt/congress_trades.csv and data/alt/13f_changes.csv.",
      requiredFiles: [
        {
          path: "data/alt/congress_trades.csv",
          columns: ["date", "ticker", "side", "amount_low", "amount_high"]
        },
        {
          path: "data/alt/13f_changes.csv",
          columns: ["quarter", "ticker", "funds_prev", "funds_current", "value_prev", "value_current"]
        }
      ],
      sources: ["Capitol Trades or Quiver Congressional Trading export", "SEC EDGAR 13F parser or WhaleWisdom export"]
    });
    return;
  }

  const days = Math.min(3650, Math.max(252, Number(url.searchParams.get("days") || 1460)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const topN = Math.min(50, Math.max(5, Number(url.searchParams.get("topN") || 20)));
  const tickers = [
    ...new Set(
      congressRows
        .map((row) => String(row.ticker || "").toUpperCase())
        .concat(holdingRows.map((row) => String(row.ticker || "").toUpperCase()))
        .filter(Boolean)
    )
  ].slice(0, 250);
  const symbols = [...new Set(tickers.concat(["SPY"]))];
  const endTime = Date.now();
  const startTime = endTime - (days + 260) * 86400000;
  const cacheMs = 6 * 60 * 60 * 1000;
  const seriesBySymbol = {};
  await Promise.all(
    symbols.map(async (symbol) => {
      seriesBySymbol[symbol] = await fetchYahooDaily(symbol, startTime, endTime, cacheMs, days + 260);
    })
  );
  const availableTickers = tickers.filter((ticker) => (seriesBySymbol[ticker] || []).length > 260);
  if (availableTickers.length < Math.min(5, topN)) {
    sendJson(response, 502, { error: "Not enough Yahoo price history for tickers in the local alt-data files." });
    return;
  }

  const aligned = alignedEtfPriceRows(seriesBySymbol, availableTickers.concat(["SPY"])).slice(-(days + 1));
  const congressByTicker = new Map();
  congressRows.forEach((row) => {
    const ticker = String(row.ticker || "").toUpperCase();
    const time = Date.parse(`${row.date}T00:00:00Z`);
    const side = String(row.side || row.type || "").toLowerCase();
    const amount =
      (Number(row.amount_low || row.low || 0) + Number(row.amount_high || row.high || row.amount || 0)) / 2 || 1;
    if (!congressByTicker.has(ticker)) congressByTicker.set(ticker, []);
    congressByTicker.get(ticker).push({ time, side, amount });
  });
  const latest13f = new Map();
  holdingRows.forEach((row) => {
    const ticker = String(row.ticker || "").toUpperCase();
    const fundsPrev = Number(row.funds_prev || row.prev_funds || 0);
    const fundsCurrent = Number(row.funds_current || row.current_funds || 0);
    const valuePrev = Number(row.value_prev || row.prev_value || 0);
    const valueCurrent = Number(row.value_current || row.current_value || 0);
    latest13f.set(ticker, { fundsDelta: fundsCurrent - fundsPrev, valueDelta: valueCurrent - valuePrev });
  });

  let equity = capital;
  let benchmarkEquity = capital;
  let currentWeights = new Map();
  let trades = 0;
  let turnover = 0;
  const points = [];
  const benchmarkPoints = [];
  const rankings = [];
  for (let index = 200; index < aligned.length - 1; index += 1) {
    const row = aligned[index];
    const next = aligned[index + 1];
    const isRebalance = index === 200 || new Date(row.time).getUTCMonth() !== new Date(aligned[index - 1].time).getUTCMonth();
    if (isRebalance) {
      const scored = availableTickers.map((ticker) => {
        const priceRows = seriesBySymbol[ticker];
        const priceRow = nearestPriceRow(priceRows, row.time);
        const pricePast = nearestPriceRow(priceRows, row.time - 126 * 86400000);
        const trades60 = (congressByTicker.get(ticker) || []).filter((trade) => trade.time <= row.time && trade.time >= row.time - 60 * 86400000);
        const netBuy = trades60.reduce((sum, trade) => sum + (trade.side.includes("sell") ? -trade.amount : trade.amount), 0);
        const inst = latest13f.get(ticker) || { fundsDelta: 0, valueDelta: 0 };
        const trend = priceRow && pricePast ? priceRow.close / pricePast.close - 1 : 0;
        const congressionalScore = Math.tanh(netBuy / 1000000);
        const institutionalScore = Math.tanh((inst.fundsDelta + inst.valueDelta / 10000000) / 10);
        const trendScore = trend > 0 ? Math.min(1, trend) : trend;
        return {
          ticker,
          congressionalScore,
          institutionalScore,
          trendScore,
          score: 0.4 * congressionalScore + 0.4 * institutionalScore + 0.2 * trendScore,
          netCongressionalBuying: netBuy,
          fundsDelta: inst.fundsDelta,
          valueDelta: inst.valueDelta,
          trend126d: trend
        };
      });
      scored.sort((a, b) => b.score - a.score);
      const selected = scored.slice(0, topN).filter((item) => item.score > 0);
      const targetWeights = new Map(selected.map((item) => [item.ticker, 1 / Math.max(1, selected.length)]));
      const allTickers = new Set([...currentWeights.keys(), ...targetWeights.keys()]);
      const oneWayTurnover =
        [...allTickers].reduce((sum, ticker) => sum + Math.abs((targetWeights.get(ticker) || 0) - (currentWeights.get(ticker) || 0)), 0) / 2;
      trades += selected.length;
      turnover += oneWayTurnover;
      currentWeights = targetWeights;
      rankings.push({ time: row.time, date: row.date, selected });
    }
    const portfolioReturn = [...currentWeights.entries()].reduce(
      (sum, [ticker, weight]) => sum + weight * (next.prices[ticker] / row.prices[ticker] - 1),
      0
    );
    equity *= 1 + portfolioReturn;
    benchmarkEquity *= 1 + (next.prices.SPY / row.prices.SPY - 1);
    points.push({ time: next.time, date: next.date, equity });
    benchmarkPoints.push({ time: next.time, date: next.date, equity: benchmarkEquity });
  }

  const metrics = { ...summarizeReturns(points, capital), trades, turnover };
  const benchmarkMetrics = summarizeReturns(benchmarkPoints, capital);
  const latestRanking = rankings.length ? rankings[rankings.length - 1].selected : [];
  sendJson(response, 200, {
    mode: "local-congress-13f-accumulation",
    strategy: "Congressional + 13F Institutional Accumulation",
    frequency: "Monthly rebalance from local congressional and 13F CSV data plus Yahoo prices",
    rows: { congress: congressRows.length, holdings: holdingRows.length, aligned: aligned.length, tickers: availableTickers.length },
    assumptions: { topN, weights: { congressional: 0.4, institutional: 0.4, trend: 0.2 } },
    metrics: {
      ...metrics,
      spyBenchmarkPnl: benchmarkMetrics.pnl,
      spyBenchmarkAnnReturn: benchmarkMetrics.annReturn,
      excessPnlVsSpy: metrics.pnl - benchmarkMetrics.pnl,
      excessAnnReturnVsSpy: metrics.annReturn - benchmarkMetrics.annReturn
    },
    latestSignals: latestRanking,
    portfolio: [...currentWeights.entries()].map(([ticker, weight]) => ({ ticker, weight })),
    benchmarks: [{ name: "SPY Buy & Hold", metrics: benchmarkMetrics, points: benchmarkPoints }],
    points
  });
}

async function handleBasisBacktest(url, response) {
  const days = Math.min(730, Math.max(3, Number(url.searchParams.get("days") || 365)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const requestedInstrument = (url.searchParams.get("instrument") || "").toUpperCase();
  const spotProduct = (url.searchParams.get("spotProduct") || "BTC-USD").toUpperCase();
  const model = (url.searchParams.get("model") || "hold").toLowerCase();
  const feeModel = (url.searchParams.get("feeModel") || "realistic").toLowerCase();
  const contractSet = (url.searchParams.get("contractSet") || "weekly-quarterly").toLowerCase();
  const entryAnnual = Number(url.searchParams.get("entryAnnual") || 0.12);
  const exitAnnual = Number(url.searchParams.get("exitAnnual") || 0.05);
  const minimumNetEdgeAnnual = Number(url.searchParams.get("minimumNetEdgeAnnual") || 0.03);
  const percentileEntry = Number(url.searchParams.get("percentileEntry") || 0.9);
  const minDte = Number(url.searchParams.get("minDte") || 14);
  const maxDte = Number(url.searchParams.get("maxDte") || 90);
  const rollDte = Number(url.searchParams.get("rollDte") || 0.25);
  const minHourlyVolume = Number(url.searchParams.get("minHourlyVolume") || 1);
  const minCashBuffer = Number(url.searchParams.get("minCashBuffer") || 0.25);
  const maxMarginDrawdown = Number(url.searchParams.get("maxMarginDrawdown") || 0.2);
  const notionalFraction = Math.min(0.85, Math.max(0.1, Number(url.searchParams.get("notionalFraction") || 0.5)));
  const fundingCostAnnual = Number(url.searchParams.get("fundingCostAnnual") || 0.04);
  const spotCashCostAnnual = Number(url.searchParams.get("spotCashCostAnnual") || fundingCostAnnual);
  const futuresMarginCostAnnual = Number(url.searchParams.get("futuresMarginCostAnnual") || 0.01);
  const safetyBufferAnnual = Number(url.searchParams.get("safetyBufferAnnual") || 0.02);
  const rollImprovementAnnual = Number(url.searchParams.get("rollImprovementAnnual") || 0.03);
  const initialMarginRate = Number(url.searchParams.get("initialMarginRate") || 0.1);
  const nonWeeklyDeliveryFeeBps = Number(url.searchParams.get("nonWeeklyDeliveryFeeBps") || 2.5);
  const feeProfiles = {
    conservative: { spotFeeBps: 40, futureFeeBps: 5, spotSlippageBps: 10, futureSlippageBps: 10 },
    realistic: { spotFeeBps: 40, futureFeeBps: 0, spotSlippageBps: 5, futureSlippageBps: 5 },
    activeMaker: { spotFeeBps: 10, futureFeeBps: 0, spotSlippageBps: 3, futureSlippageBps: 3 },
    optimistic: { spotFeeBps: 3, futureFeeBps: 0, spotSlippageBps: 2, futureSlippageBps: 2 }
  };
  const fees = feeProfiles[feeModel] || feeProfiles.realistic;
  const endTime = Date.now();
  const startTime = endTime - days * 86400000;
  const cacheMs = 15 * 60 * 1000;

  const candidateNames = requestedInstrument
    ? [requestedInstrument]
    : generatedDeribitFutureCandidates(startTime, endTime, minDte, maxDte, contractSet);
  const candleSets = [];
  for (const instrumentName of candidateNames) {
    const expiration = parseDeribitFutureName(instrumentName);
    if (!expiration) {
      continue;
    }
    try {
      const chart = await fetchJson(
        `${deribitBase}/public/get_tradingview_chart_data?instrument_name=${instrumentName}&start_timestamp=${startTime}&end_timestamp=${endTime}&resolution=60`,
        cacheMs
      );
      const candles = deribitChartToCandles(chart).filter((row) => row.volume >= minHourlyVolume);
      if (candles.length) {
        candleSets.push({ instrumentName, expiration, candles });
      }
    } catch (error) {
      // Some historical weekly names are not listed by Deribit for every date; skip missing contracts.
    }
  }

  if (!candleSets.length) {
    sendJson(response, 404, { error: "No Deribit BTC dated futures candles were available for this window." });
    return;
  }

  const spotCandles = await fetchCoinbaseCandles(spotProduct, startTime, endTime, 3600, cacheMs);
  const curveByTime = new Map();
  candleSets.forEach((set) => {
    set.candles.forEach((candle) => {
      const daysToExpiry = (set.expiration - candle.time) / 86400000;
      if (daysToExpiry < minDte || daysToExpiry > maxDte) {
        return;
      }
      const rows = curveByTime.get(candle.time) || [];
      rows.push({ ...candle, instrumentName: set.instrumentName, expiration: set.expiration, daysToExpiry });
      curveByTime.set(candle.time, rows);
    });
  });

  const openingRate =
    (fees.spotFeeBps + Math.abs(fees.futureFeeBps) + fees.spotSlippageBps + fees.futureSlippageBps) / 10000;
  const closingRate = openingRate;
  const futuresLegRate = (Math.abs(fees.futureFeeBps) + fees.futureSlippageBps) / 10000;
  const spotRoundTripRate = (fees.spotFeeBps * 2 + fees.spotSlippageBps * 2) / 10000;
  const futuresRoundTripRate = (Math.abs(fees.futureFeeBps) * 2 + fees.futureSlippageBps * 2) / 10000;
  const basisRows = [];
  [...curveByTime.entries()]
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .forEach(([time, futures]) => {
      const spot = nearestCoinbaseClose(spotCandles, Number(time));
      if (!spot) {
        return;
      }
      futures
        .sort((a, b) => a.daysToExpiry - b.daysToExpiry)
        .slice(0, 2)
        .forEach((future) => {
          const basis = future.close / spot - 1;
          const annualized = basis * (365 / future.daysToExpiry);
          const deliveryFeeBps = deribitDeliveryFeeBps(future.expiration, nonWeeklyDeliveryFeeBps);
          const deliveryRate = deliveryFeeBps / 10000;
          const amortizedSpotFee = spotRoundTripRate * (365 / future.daysToExpiry);
          const amortizedFuturesFee = futuresRoundTripRate * (365 / future.daysToExpiry);
          const amortizedDelivery = deliveryRate * (365 / future.daysToExpiry);
          const amortizedExecution = amortizedSpotFee + amortizedFuturesFee + amortizedDelivery;
          const netAnnualized =
            annualized -
            spotCashCostAnnual -
            futuresMarginCostAnnual -
            safetyBufferAnnual -
            amortizedExecution;
          basisRows.push({
            ...future,
            spot,
            basis,
            annualized,
            netAnnualized,
            amortizedExecution,
            amortizedSpotFee,
            amortizedFuturesFee,
            amortizedDelivery,
            deliveryFeeBps,
            isWeekly: !isLastFridayExpiry(future.expiration),
            isQuarterly: isQuarterlyExpiry(future.expiration)
          });
        });
    });

  const sortedNet = basisRows.map((row) => row.netAnnualized).filter(Number.isFinite).sort((a, b) => a - b);
  const percentileIndex = Math.max(0, Math.min(sortedNet.length - 1, Math.floor(sortedNet.length * percentileEntry)));
  const percentileThreshold = sortedNet.length ? sortedNet[percentileIndex] : entryAnnual;
  const entryThreshold = Math.max(entryAnnual, percentileThreshold);
  const rowsByTime = new Map();
  basisRows.forEach((row) => {
    const rows = rowsByTime.get(row.time) || [];
    rows.push(row);
    rowsByTime.set(row.time, rows);
  });
  const timeEntries = [...rowsByTime.entries()].sort((a, b) => Number(a[0]) - Number(b[0]));
  const futureCandles = timeEntries.map(([, rows]) =>
    [...rows].sort((a, b) => b.netAnnualized - a.netAnnualized || a.daysToExpiry - b.daysToExpiry)[0]
  );

  let equity = capital;
  let inPosition = false;
  let notional = 0;
  let trades = 0;
  let spotPositionOpen = false;
  let spotPnl = 0;
  let futuresPnl = 0;
  let basisCompressionPnl = 0;
  let feesPaid = 0;
  let slippagePaid = 0;
  let deliveryFeesPaid = 0;
  let rollCost = 0;
  let fundingCost = 0;
  let previousSpot = null;
  let previousFuture = null;
  let currentInstrument = null;
  let entryEquity = capital;
  let reason = "none";
  const points = [];

  for (const [, rows] of timeEntries) {
    const sortedRows = [...rows].sort((a, b) => b.netAnnualized - a.netAnnualized || a.daysToExpiry - b.daysToExpiry);
    const heldFuture = inPosition ? sortedRows.find((row) => row.instrumentName === currentInstrument) : null;
    const bestFuture = sortedRows[0];
    const rollCandidateIsBetter =
      inPosition &&
      heldFuture &&
      bestFuture.instrumentName !== heldFuture.instrumentName &&
      bestFuture.netAnnualized >= heldFuture.netAnnualized + rollImprovementAnnual &&
      bestFuture.netAnnualized >= minimumNetEdgeAnnual;
    let future = heldFuture || bestFuture;
    const spot = future.spot;

    if (inPosition && previousSpot && previousFuture) {
      const spotLegPnl = notional * (spot / previousSpot - 1);
      const futureLegPnl = -notional * (future.close / previousFuture - 1);
      const carryCost = (notional * (spotCashCostAnnual + futuresMarginCostAnnual)) / (365 * 24);
      equity += spotLegPnl + futureLegPnl - carryCost;
      spotPnl += spotLegPnl;
      futuresPnl += futureLegPnl;
      basisCompressionPnl += spotLegPnl + futureLegPnl;
      fundingCost += carryCost;
    }

    let rolledThisStep = false;
    if (rollCandidateIsBetter) {
      const rollFee = notional * futuresLegRate * 2;
      equity -= rollFee;
      rollCost += rollFee;
      currentInstrument = bestFuture.instrumentName;
      previousFuture = bestFuture.close;
      trades += 1;
      future = bestFuture;
      rolledThisStep = true;
    }

    const daysToExpiry = Math.max(0.01, future.daysToExpiry);
    const netAnnualized = future.netAnnualized;
    const marginUsed = inPosition ? notional * initialMarginRate : 0;
    const cashBuffer = inPosition ? Math.max(0, equity - notional - marginUsed) / Math.max(1, equity) : 1;
    const tradeDrawdown = inPosition ? equity / entryEquity - 1 : 0;
    const shouldEnter =
      !inPosition &&
      future.annualized >= entryThreshold &&
      netAnnualized >= minimumNetEdgeAnnual &&
      daysToExpiry > rollDte;
    const shouldExitDynamic = model !== "hold" && inPosition && netAnnualized < exitAnnual;
    const shouldExitExpiry = inPosition && daysToExpiry <= rollDte;
    const shouldExitLiquidity = inPosition && future.volume < minHourlyVolume;
    const shouldExitMargin = inPosition && (tradeDrawdown < -maxMarginDrawdown || cashBuffer < minCashBuffer);

    if (shouldEnter) {
      notional = equity * notionalFraction;
      const feeCost = notional * (fees.spotFeeBps + Math.abs(fees.futureFeeBps)) / 10000;
      const slipCost = notional * (fees.spotSlippageBps + fees.futureSlippageBps) / 10000;
      equity -= feeCost + slipCost;
      feesPaid += feeCost;
      slippagePaid += slipCost;
      inPosition = true;
      spotPositionOpen = true;
      currentInstrument = future.instrumentName;
      entryEquity = equity;
      reason = "enter";
      trades += 1;
    } else if (shouldExitDynamic || shouldExitExpiry || shouldExitLiquidity || shouldExitMargin) {
      const feeCost = notional * (fees.spotFeeBps + Math.abs(fees.futureFeeBps)) / 10000;
      const slipCost = notional * (fees.spotSlippageBps + fees.futureSlippageBps) / 10000;
      const deliveryFee = shouldExitExpiry ? notional * (future.deliveryFeeBps / 10000) : 0;
      equity -= feeCost + slipCost + deliveryFee;
      feesPaid += feeCost;
      slippagePaid += slipCost;
      deliveryFeesPaid += deliveryFee;
      inPosition = false;
      spotPositionOpen = false;
      currentInstrument = null;
      notional = 0;
      reason = shouldExitDynamic
        ? "exit_net_basis"
        : shouldExitExpiry
          ? "exit_expiry"
          : shouldExitLiquidity
            ? "exit_liquidity"
            : "exit_margin";
      trades += 1;
    } else {
      reason = rolledThisStep ? "roll_better_net_basis" : "hold";
    }

    previousSpot = spot;
    previousFuture = future.close;
    points.push({
      time: future.time,
      equity,
      spot,
      future: future.close,
      basis: future.basis,
      annualized: future.annualized,
      netAnnualized,
      amortizedExecution: future.amortizedExecution,
      amortizedSpotFee: future.amortizedSpotFee,
      amortizedFuturesFee: future.amortizedFuturesFee,
      amortizedDelivery: future.amortizedDelivery,
      deliveryFeeBps: future.deliveryFeeBps,
      daysToExpiry,
      instrument: future.instrumentName,
      volume: future.volume,
      marginUsed,
      cashBuffer,
      reason,
      inPosition
    });
  }

  if (inPosition && notional > 0) {
    const feeCost = notional * (fees.spotFeeBps + Math.abs(fees.futureFeeBps)) / 10000;
    const slipCost = notional * (fees.spotSlippageBps + fees.futureSlippageBps) / 10000;
    equity -= feeCost + slipCost;
    feesPaid += feeCost;
    slippagePaid += slipCost;
    trades += 1;
    if (points.length) {
      points[points.length - 1].equity = equity;
      points[points.length - 1].inPosition = false;
      points[points.length - 1].reason = "final_close";
    }
  }

  const buyHoldStart = spotCandles[0] ? Number(spotCandles[0][4]) : null;
  const buyHoldEnd = spotCandles[spotCandles.length - 1] ? Number(spotCandles[spotCandles.length - 1][4]) : null;
  const buyHoldPnl = buyHoldStart && buyHoldEnd ? capital * (buyHoldEnd / buyHoldStart - 1) : null;
  const alwaysCarryEligible = futureCandles.filter((row) => row.daysToExpiry >= minDte && row.daysToExpiry <= maxDte);
  const avgNetBasis =
    alwaysCarryEligible.reduce((sum, row) => sum + row.netAnnualized, 0) / Math.max(1, alwaysCarryEligible.length);

  sendJson(response, 200, {
    mode: "public-deribit-coinbase-basis-v3-high-threshold-carry",
    instrument: requestedInstrument || contractSet,
    instruments: candleSets.map((set) => set.instrumentName),
    spotProduct,
    days,
    frequency: "1h Deribit futures candles with 1h Coinbase spot candles; high-threshold net-basis carry",
    rows: { futureCandles: futureCandles.length, spotCandles: spotCandles.length, instruments: candleSets.length },
    coverage: {
      requestedHours: Math.round((endTime - startTime) / 3600000),
      futureCoverageRatio: futureCandles.length / Math.max(1, Math.round((endTime - startTime) / 3600000))
    },
    assumptions: {
      model,
      feeModel,
      contractSet,
      entryAnnual,
      exitAnnual,
      minimumNetEdgeAnnual,
      percentileEntry,
      entryThreshold,
      percentileThreshold,
      fees,
      fundingCostAnnual,
      spotCashCostAnnual,
      futuresMarginCostAnnual,
      safetyBufferAnnual,
      rollImprovementAnnual,
      nonWeeklyDeliveryFeeBps,
      capital,
      notionalFraction,
      minDte,
      maxDte,
      rollDte,
      minHourlyVolume,
      minCashBuffer,
      maxMarginDrawdown,
      initialMarginRate,
      historicalLimitations: "Deribit historical candles provide price and volume; historical bid/ask spread and open interest are not available in this public candle endpoint."
    },
    metrics: {
      ...summarizeReturns(points, capital),
      trades,
      spotPnl,
      futuresPnl,
      basisCompressionPnl,
      fees: feesPaid,
      slippage: slippagePaid,
      deliveryFees: deliveryFeesPaid,
      rollCost,
      fundingCost,
      totalCosts: feesPaid + slippagePaid + deliveryFeesPaid + rollCost + fundingCost
    },
    benchmarks: {
      buyHoldBtcPnl: buyHoldPnl,
      flatCashPnl: 0,
      avgEligibleNetBasis: avgNetBasis,
      topQuartileNetBasis: sortedNet.length ? sortedNet[Math.floor(sortedNet.length * 0.75)] : null
    },
    points
  });
}

async function handleBasisLive(url, response) {
  const requestedInstrument = (url.searchParams.get("instrument") || "").toUpperCase();
  const spotProduct = (url.searchParams.get("spotProduct") || "BTC-USD").toUpperCase();
  const minDte = Number(url.searchParams.get("minDte") || 14);
  const maxDte = Number(url.searchParams.get("maxDte") || 90);
  const feeModel = (url.searchParams.get("feeModel") || "realistic").toLowerCase();
  const fundingCostAnnual = Number(url.searchParams.get("fundingCostAnnual") || 0.04);
  const futuresMarginCostAnnual = Number(url.searchParams.get("futuresMarginCostAnnual") || 0.01);
  const safetyBufferAnnual = Number(url.searchParams.get("safetyBufferAnnual") || 0.02);
  const nonWeeklyDeliveryFeeBps = Number(url.searchParams.get("nonWeeklyDeliveryFeeBps") || 2.5);
  const feeProfiles = {
    conservative: { spotFeeBps: 40, futureFeeBps: 5, spotSlippageBps: 10, futureSlippageBps: 10 },
    realistic: { spotFeeBps: 40, futureFeeBps: 0, spotSlippageBps: 5, futureSlippageBps: 5 },
    activeMaker: { spotFeeBps: 10, futureFeeBps: 0, spotSlippageBps: 3, futureSlippageBps: 3 },
    optimistic: { spotFeeBps: 3, futureFeeBps: 0, spotSlippageBps: 2, futureSlippageBps: 2 }
  };
  const fees = feeProfiles[feeModel] || feeProfiles.realistic;
  const cacheMs = 20 * 1000;

  const [instrumentsPayload, summariesPayload, spotTicker] = await Promise.all([
    fetchJson(`${deribitBase}/public/get_instruments?currency=BTC&kind=future&expired=false`, cacheMs),
    fetchJson(`${deribitBase}/public/get_book_summary_by_currency?currency=BTC&kind=future`, cacheMs),
    fetchJson(`${coinbaseExchangeBase}/products/${spotProduct}/ticker`, cacheMs)
  ]);
  const instruments = instrumentsPayload.result || [];
  const spot = Number(spotTicker.price);
  const instrumentByName = new Map(instruments.map((instrument) => [instrument.instrument_name, instrument]));
  const spotRoundTripRate = (fees.spotFeeBps * 2 + fees.spotSlippageBps * 2) / 10000;
  const futuresRoundTripRate = (Math.abs(fees.futureFeeBps) * 2 + fees.futureSlippageBps * 2) / 10000;
  const liveRows = (summariesPayload.result || [])
    .map((summary) => {
      const instrument = instrumentByName.get(summary.instrument_name);
      if (!instrument || instrument.instrument_name === "BTC-PERPETUAL") return null;
      const daysToExpiry = Math.max(0.01, (Number(instrument.expiration_timestamp) - Date.now()) / 86400000);
      if (daysToExpiry < minDte || daysToExpiry > maxDte) return null;
      const future =
        Number(summary.mid_price) ||
        Number(summary.mark_price) ||
        Number(summary.last) ||
        Number(summary.estimated_delivery_price);
      if (!Number.isFinite(spot) || !Number.isFinite(future) || spot <= 0 || future <= 0) return null;
      const basis = future / spot - 1;
      const annualizedBasis = basis * (365 / daysToExpiry);
      const deliveryFeeBps = deribitDeliveryFeeBps(Number(instrument.expiration_timestamp), nonWeeklyDeliveryFeeBps);
      const amortizedSpotFee = spotRoundTripRate * (365 / daysToExpiry);
      const amortizedFuturesFee = futuresRoundTripRate * (365 / daysToExpiry);
      const amortizedDelivery = (deliveryFeeBps / 10000) * (365 / daysToExpiry);
      const netAnnualizedBasis =
        annualizedBasis -
        fundingCostAnnual -
        futuresMarginCostAnnual -
        safetyBufferAnnual -
        amortizedSpotFee -
        amortizedFuturesFee -
        amortizedDelivery;
      return {
        instrument: instrument.instrument_name,
        spotPrice: spot,
        futurePrice: future,
        basis,
        annualizedBasis,
        netAnnualizedBasis,
        daysToExpiry,
        openInterest: Number(summary.open_interest || 0),
        volumeUsd: Number(summary.volume_usd || 0),
        bidPrice: summary.bid_price === null ? null : Number(summary.bid_price),
        askPrice: summary.ask_price === null ? null : Number(summary.ask_price),
        deliveryFeeBps,
        amortizedSpotFee,
        amortizedFuturesFee,
        amortizedDelivery,
        isWeekly: !isLastFridayExpiry(Number(instrument.expiration_timestamp)),
        isQuarterly: isQuarterlyExpiry(Number(instrument.expiration_timestamp))
      };
    })
    .filter(Boolean);
  const requestedRow = liveRows.find((row) => row.instrument === requestedInstrument);
  const bestRow = requestedRow || liveRows.sort((a, b) => b.netAnnualizedBasis - a.netAnnualizedBasis)[0];

  if (!bestRow) {
    sendJson(response, 404, { error: "No Deribit BTC dated future matched the requested DTE window." });
    return;
  }

  sendJson(response, 200, {
    mode: "public-deribit-coinbase-live",
    time: Date.now(),
    frequency: "20s REST polling for live net-basis monitor",
    instrument: bestRow.instrument,
    spotProduct,
    feeModel,
    assumptions: { minDte, maxDte, fees, fundingCostAnnual, futuresMarginCostAnnual, safetyBufferAnnual, nonWeeklyDeliveryFeeBps },
    rows: { candidates: liveRows.length },
    ...bestRow
  });
}

async function fetchDeribitFundingHistory(instrumentName, startTime, endTime, cacheMs) {
  const rows = [];
  const chunkMs = 30 * 86400000;
  let cursor = startTime;
  while (cursor < endTime) {
    const chunkEnd = Math.min(endTime, cursor + chunkMs);
    const payload = await fetchJson(
      `${deribitBase}/public/get_funding_rate_history?instrument_name=${instrumentName}&start_timestamp=${cursor}&end_timestamp=${chunkEnd}`,
      cacheMs
    );
    if (Array.isArray(payload.result)) {
      rows.push(...payload.result);
    }
    cursor = chunkEnd + 1;
  }
  const deduped = new Map();
  rows.forEach((row) => deduped.set(Number(row.timestamp), row));
  return [...deduped.values()].sort((a, b) => Number(a.timestamp) - Number(b.timestamp));
}

async function handleDeribitFundingBacktest(url, response, binanceError = null) {
  const instrumentName = (url.searchParams.get("instrument") || "BTC-PERPETUAL").toUpperCase();
  const days = Math.min(730, Math.max(30, Number(url.searchParams.get("days") || 365)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const entryAnnual = Number(url.searchParams.get("entryAnnual") || 0.25);
  const exitAnnual = Number(url.searchParams.get("exitAnnual") || -0.01);
  const minHoldHours = Number(url.searchParams.get("minHoldHours") || 72);
  const feeBps = Number(url.searchParams.get("feeBps") || 2);
  const slippageBps = Number(url.searchParams.get("slippageBps") || 1);
  const notionalFraction = Math.min(1, Math.max(0.1, Number(url.searchParams.get("notionalFraction") || 0.5)));
  const feeRate = (feeBps + slippageBps) / 10000;
  const endTime = Date.now();
  const startTime = endTime - days * 86400000;
  const cacheMs = 15 * 60 * 1000;
  const funding = await fetchDeribitFundingHistory(instrumentName, startTime, endTime, cacheMs);

  let equity = capital;
  let inPosition = false;
  let trades = 0;
  let fundingIncome = 0;
  let costs = 0;
  let notional = 0;
  let openedAt = null;
  const points = [];

  for (const row of funding) {
    const time = Number(row.timestamp);
    const fundingRate = Number(row.interest_1h || 0);
    const annualized = Number(row.interest_8h || 0) * 3 * 365;
    const indexPrice = Number(row.index_price || row.prev_index_price || 0);

    if (inPosition) {
      const fundingCash = notional * fundingRate;
      equity += fundingCash;
      fundingIncome += fundingCash;
    }

    if (!inPosition && annualized >= entryAnnual) {
      notional = equity * notionalFraction;
      const openCost = notional * feeRate * 2;
      equity -= openCost;
      costs += openCost;
      inPosition = true;
      openedAt = time;
      trades += 1;
    } else if (inPosition && annualized < exitAnnual && openedAt && time - openedAt >= minHoldHours * 3600000) {
      const closeCost = notional * feeRate * 2;
      equity -= closeCost;
      costs += closeCost;
      inPosition = false;
      openedAt = null;
      notional = 0;
      trades += 1;
    }

    points.push({ time, equity, fundingRate, annualized, inPosition, spot: indexPrice, mark: indexPrice });
  }

  sendJson(response, 200, {
    mode: "public-deribit-funding",
    instrument: instrumentName,
    days,
    frequency: "1h Deribit BTC perpetual funding history",
    rows: { funding: funding.length },
    assumptions: { entryAnnual, exitAnnual, feeBps, slippageBps, capital, notionalFraction, minHoldHours },
    fallbackFromBinance: binanceError,
    metrics: { ...summarizeReturns(points, capital), trades, fundingIncome, hedgePnl: 0, costs },
    points
  });
}

async function handleFundingBacktest(url, response) {
  const symbol = (url.searchParams.get("symbol") || "BTCUSDT").toUpperCase();
  const days = Math.min(730, Math.max(30, Number(url.searchParams.get("days") || 365)));
  const capital = Math.max(1000, Number(url.searchParams.get("capital") || 100000));
  const entryAnnual = Number(url.searchParams.get("entryAnnual") || 0.15);
  const exitAnnual = Number(url.searchParams.get("exitAnnual") || 0.03);
  const feeBps = Number(url.searchParams.get("feeBps") || 8);
  const slippageBps = Number(url.searchParams.get("slippageBps") || 4);
  const notionalFraction = Math.min(1, Math.max(0.1, Number(url.searchParams.get("notionalFraction") || 0.5)));
  const feeRate = (feeBps + slippageBps) / 10000;
  const endTime = Date.now();
  const startTime = endTime - days * 86400000;
  const cacheMs = 15 * 60 * 1000;

  let funding;
  let markCandles;
  let spotCandles;
  try {
    [funding, markCandles, spotCandles] = await Promise.all([
    fetchPaged(`${futuresBase}/fapi/v1/fundingRate`, { symbol, startTime, endTime }, 1000, cacheMs),
    fetchPaged(
      `${futuresBase}/fapi/v1/markPriceKlines`,
      { symbol, interval: "8h", startTime, endTime },
      1000,
      cacheMs
    ),
    fetchPaged(
      `${spotBase}/api/v3/klines`,
      { symbol, interval: "8h", startTime, endTime },
      1000,
      cacheMs
    )
    ]);
  } catch (error) {
    await handleDeribitFundingBacktest(url, response, error.message);
    return;
  }

  let equity = capital;
  let inPosition = false;
  let trades = 0;
  let fundingIncome = 0;
  let hedgePnl = 0;
  let costs = 0;
  let previousSpot = null;
  let previousMark = null;
  let notional = 0;
  const points = [];

  for (const event of funding) {
    const time = Number(event.fundingTime);
    const fundingRate = Number(event.fundingRate);
    const annualized = fundingRate * 3 * 365;
    const spot = nearestClose(spotCandles, time);
    const mark = nearestClose(markCandles, time);
    if (!spot || !mark) {
      continue;
    }

    if (inPosition) {
      if (previousSpot && previousMark) {
        const residual = notional * (spot / previousSpot - mark / previousMark);
        equity += residual;
        hedgePnl += residual;
      }

      const fundingCash = notional * fundingRate;
      equity += fundingCash;
      fundingIncome += fundingCash;
    }

    if (!inPosition && annualized >= entryAnnual) {
      notional = equity * notionalFraction;
      const openCost = notional * feeRate * 2;
      equity -= openCost;
      costs += openCost;
      inPosition = true;
      trades += 1;
    } else if (inPosition && annualized < exitAnnual) {
      const closeCost = notional * feeRate * 2;
      equity -= closeCost;
      costs += closeCost;
      inPosition = false;
      notional = 0;
      trades += 1;
    }

    previousSpot = spot;
    previousMark = mark;
    points.push({ time, equity, fundingRate, annualized, inPosition, spot, mark });
  }

  sendJson(response, 200, {
    mode: "public-binance",
    symbol,
    days,
    frequency: "8h funding events with 8h spot and mark candles",
    rows: { funding: funding.length, markCandles: markCandles.length, spotCandles: spotCandles.length },
    assumptions: { entryAnnual, exitAnnual, feeBps, slippageBps, capital, notionalFraction },
    metrics: { ...summarizeReturns(points, capital), trades, fundingIncome, hedgePnl, costs },
    points
  });
}

async function handleFundingLive(url, response) {
  const symbol = (url.searchParams.get("symbol") || "BTCUSDT").toUpperCase();
  const cacheMs = 20 * 1000;
  const [premium, spot, openInterest] = await Promise.all([
    fetchJson(`${futuresBase}/fapi/v1/premiumIndex?symbol=${symbol}`, cacheMs),
    fetchJson(`${spotBase}/api/v3/ticker/price?symbol=${symbol}`, cacheMs),
    fetchJson(`${futuresBase}/fapi/v1/openInterest?symbol=${symbol}`, cacheMs)
  ]);

  sendJson(response, 200, {
    mode: "public-binance-live",
    symbol,
    time: Date.now(),
    frequency: "20s REST polling for paper monitoring",
    markPrice: Number(premium.markPrice),
    indexPrice: Number(premium.indexPrice),
    spotPrice: Number(spot.price),
    lastFundingRate: Number(premium.lastFundingRate),
    annualizedFunding: Number(premium.lastFundingRate) * 3 * 365,
    nextFundingTime: Number(premium.nextFundingTime),
    openInterest: Number(openInterest.openInterest)
  });
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, `http://${host}:${port}`);
  if (url.pathname === "/api/health") {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (url.pathname === "/api/funding-carry/backtest") {
    handleFundingBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/funding-carry/live") {
    handleFundingLive(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/futures-basis/backtest") {
    handleBasisBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/risk-parity-etf/backtest") {
    handleRiskParityEtfBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/hmm-etf-regime/backtest") {
    handleHmmEtfBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/etf-overlap-stat-arb/backtest") {
    handleEtfOverlapStatArbBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/congress-13f/backtest") {
    handleCongress13fBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/futures-basis/live") {
    handleBasisLive(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/options-vol/snapshot") {
    handleOptionsSnapshot(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/options-vol/backtest") {
    handleOptionsBacktest(url, response).catch((error) => {
      sendJson(response, 502, { error: error.message });
    });
    return;
  }

  if (url.pathname === "/api/microstructure/backtest") {
    handleMicroBacktest(url, response);
    return;
  }

  if (url.pathname === "/api/intraday/performance") {
    handleIntradayPerformance(url, response);
    return;
  }

  const requestedPath = url.pathname === "/" ? "/index.html" : url.pathname;
  const filePath = path.normalize(path.join(root, requestedPath));

  if (!filePath.startsWith(root)) {
    response.writeHead(403, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Forbidden");
    return;
  }

  sendFile(response, filePath);
});

server.listen(port, host, () => {
  console.log(`Dashboard running at http://${host}:${port}/`);
});

setInterval(() => {}, 60 * 60 * 1000);
