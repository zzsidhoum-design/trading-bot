# 03 — Database Design

Two stores, two jobs:

- **PostgreSQL 16** — source of truth, all business data. SQLAlchemy 2.0 (async) + Alembic migrations.
- **Redis 7** — hot cache, rankings, distributed locks, rate limits, event fan-out, job queue.

All timestamps are `TIMESTAMPTZ` (UTC). Monetary columns use `NUMERIC(20,6)` (no float money). Enums are native PG `ENUM` where values are stable, or `VARCHAR` + CHECK where they may evolve (agent names, metric names).

---

## 1. PostgreSQL — Entity Tables

### `stocks` — universe
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| symbol | VARCHAR(16) NOT NULL | |
| exchange | VARCHAR(32) NOT NULL | |
| name | VARCHAR(255) | |
| currency | CHAR(3) | |
| sector / industry | VARCHAR(64) | |
| market_cap | NUMERIC(20,2) | |
| shares_outstanding | NUMERIC(20,2) | |
| is_active | BOOL DEFAULT true | excluded from scans when false |
| created_at / updated_at | TIMESTAMPTZ | |

`UNIQUE (symbol, exchange)`, index on `(sector)`.

### `prices` — OHLCV bars (partitioned)
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK → stocks | |
| interval | VARCHAR(8) NOT NULL | `1m/5m/15m/1h/1d` |
| ts | TIMESTAMPTZ NOT NULL | bar open time, UTC |
| open / high / low / close | NUMERIC(18,6) | |
| volume | NUMERIC(20,0) | |
| vwap | NUMERIC(18,6) | |
| source | VARCHAR(32) | provider adapter name |

`UNIQUE (stock_id, interval, ts)`; index `(stock_id, interval, ts DESC)`.
**Partitioned by RANGE (ts) monthly** — hot month kept hot, old partitions detached/archived. This is what makes "thousands of symbols, real-time" feasible.

### `indicators` — computed technicals
| column | type |
|---|---|
| id | BIGSERIAL PK |
| stock_id | BIGINT FK |
| interval | VARCHAR(8) |
| ts | TIMESTAMPTZ |
| rsi, ema_9, ema_21, sma_50, sma_200 | NUMERIC(12,6) |
| macd, macd_signal, macd_hist | NUMERIC(12,6) |
| atr | NUMERIC(12,6) |
| vwap | NUMERIC(18,6) |
| boll_upper, boll_middle, boll_lower | NUMERIC(12,6) |
| adx | NUMERIC(12,6) |
| stoch_k, stoch_d | NUMERIC(12,6) |
| ichimoku_tenkan, ichimoku_kijun, ichimoku_senkou_a, ichimoku_senkou_b, ichimoku_chikou | NUMERIC(12,6) |
| volume_profile | JSONB |
| extras | JSONB |

`UNIQUE (stock_id, interval, ts)`.

### `fundamentals` — per-period financial snapshot
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK | |
| period | VARCHAR(16) | e.g. `2024Q4` |
| report_date | DATE | |
| revenue | NUMERIC(20,2) | |
| eps | NUMERIC(14,6) | |
| pe_ratio | NUMERIC(14,6) | |
| debt_total / cash_flow | NUMERIC(20,2) | |
| roe / roa | NUMERIC(10,6) | |
| gross_margin / operating_margin / net_margin | NUMERIC(10,6) | |
| revenue_growth / earnings_growth | NUMERIC(10,6) | YoY |
| price_to_book | NUMERIC(14,6) | |

`UNIQUE (stock_id, period)`.

### `earnings` — calendar & actuals
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK | |
| fiscal_period | VARCHAR(16) | |
| report_date | DATE | |
| eps_actual / eps_estimate | NUMERIC(14,6) | |
| revenue_actual / revenue_estimate | NUMERIC(20,2) | |
| surprise_pct | NUMERIC(10,4) | |
| is_upcoming | BOOL | pre-report flag for News Agent |

`UNIQUE (stock_id, fiscal_period)`, index `(report_date)`.

### `news` — articles & their LLM analysis
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK NULL | NULL = market-wide/economic |
| source | VARCHAR(64) | |
| title | VARCHAR(512) | |
| url | VARCHAR(1024) UNIQUE | dedup key |
| published_at | TIMESTAMPTZ | |
| content | TEXT | |
| categories | TEXT[] | |
| sentiment_score | NUMERIC(6,4) | -1..1 from LLM |
| summary | TEXT | LLM summary |
| expected_market_impact | VARCHAR(8) | LOW/MEDIUM/HIGH |
| impact_direction | SMALLINT | -1/0/1 |
| analysis_confidence | NUMERIC(6,4) | |
| analyzed_at | TIMESTAMPTZ | |

Indexes: `(stock_id, published_at DESC)`, `(published_at DESC)`.

### `signals` — every agent's scored output
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK | |
| agent | VARCHAR(32) | `technical/news/fundamental/combined` |
| interval | VARCHAR(8) NULL | |
| signal_type | VARCHAR(16) | STRONG_BUY/BUY/NEUTRAL/HOLD/SELL/STRONG_SELL |
| score | NUMERIC(8,4) | -1..1 |
| strength | NUMERIC(8,4) | 0..1 |
| horizon | VARCHAR(16) | |
| metadata | JSONB | sub-scores, indicator snapshot |
| created_at | TIMESTAMPTZ | |

Indexes: `(stock_id, agent, created_at DESC)`, `(created_at)`.

### `predictions` — ML outputs
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| stock_id | BIGINT FK | |
| model_name | VARCHAR(64) | |
| model_version | INT | |
| horizon | VARCHAR(16) | |
| prob_up / prob_down / prob_trend | NUMERIC(8,4) | |
| confidence | NUMERIC(8,4) | calibrated |
| expected_return | NUMERIC(12,6) | |
| expected_volatility | NUMERIC(12,6) | |
| features_hash | VARCHAR(64) | provenance |
| created_at | TIMESTAMPTZ | |

Index `(stock_id, created_at DESC)`.

### `portfolios`
| column | type |
|---|---|
| id | BIGSERIAL PK |
| name | VARCHAR(128) |
| currency | CHAR(3) |
| initial_capital | NUMERIC(20,2) |
| current_cash | NUMERIC(20,2) |
| mode | VARCHAR(16) | backtest/paper/live |
| status | VARCHAR(16) | active/frozen/archived |
| created_at / updated_at | TIMESTAMPTZ |

### `positions`
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| portfolio_id | BIGINT FK | |
| stock_id | BIGINT FK | |
| status | VARCHAR(16) | OPEN/CLOSED |
| quantity | NUMERIC(20,4) | signed (+ long / − short) |
| avg_entry_price | NUMERIC(18,6) | |
| current_price | NUMERIC(18,6) | refreshed |
| stop_loss / take_profit | NUMERIC(18,6) | bracket |
| realized_pnl | NUMERIC(20,6) | when closed |
| opened_at / closed_at | TIMESTAMPTZ | |

Indexes: `(portfolio_id, status)`, `(stock_id)`.

### `orders`
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| idempotency_key | UUID UNIQUE | replay protection |
| portfolio_id / stock_id | BIGINT FK | |
| side | VARCHAR(4) | BUY/SELL |
| order_type | VARCHAR(8) | MARKET/LIMIT/STOP |
| quantity | NUMERIC(20,4) | |
| limit_price / stop_price | NUMERIC(18,6) | |
| status | VARCHAR(16) | PENDING/SUBMITTED/PARTIAL/FILLED/CANCELED/REJECTED |
| broker_order_id | VARCHAR(64) | |
| filled_qty | NUMERIC(20,4) | |
| avg_fill_price | NUMERIC(18,6) | |
| commission | NUMERIC(14,6) | |
| mode | VARCHAR(16) | |
| decision_ref | UUID | link to decision_log |
| reason | JSONB | signals that produced this order |
| created_at / submitted_at / executed_at | TIMESTAMPTZ | |

Indexes: `(portfolio_id, status)`, `(stock_id)`, `(created_at)`.

### `trades` — closed P/L records (Memory System core)
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| position_id | BIGINT FK | |
| portfolio_id | BIGINT FK | |
| stock_id | BIGINT FK | |
| strategy | VARCHAR(64) | |
| side | VARCHAR(4) | |
| quantity | NUMERIC(20,4) | |
| entry_price / exit_price | NUMERIC(18,6) | |
| pnl / pnl_pct | NUMERIC(20,6) / NUMERIC(12,6) | |
| fees | NUMERIC(14,6) | |
| entry_time / exit_time | TIMESTAMPTZ | |
| decision_reason | JSONB | full Chief rationale + agent scores |
| outcome | VARCHAR(16) | WIN/LOSS/BREAKEVEN |
| mode | VARCHAR(16) | |

Indexes: `(portfolio_id, exit_time DESC)`, `(stock_id)`, `(strategy)`.

### `decision_log` — every Chief decision, with explanation
| column | type |
|---|---|
| id | BIGSERIAL PK |
| decision_uuid | UUID UNIQUE |
| stock_id | BIGINT FK |
| decision | VARCHAR(8) | BUY/SELL/HOLD |
| confidence | NUMERIC(8,4) |
| rationale | TEXT | human-readable explanation |
| agent_scores | JSONB | each signal source score + weight |
| created_at | TIMESTAMPTZ |

### `risk_history` — every risk assessment
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| decision_uuid | UUID FK | |
| portfolio_id / stock_id | BIGINT FK | |
| approved | BOOL | |
| rejection_reason | TEXT | |
| position_size | NUMERIC(20,4) | |
| stop_loss / take_profit | NUMERIC(18,6) | |
| risk_per_trade_pct | NUMERIC(10,4) | |
| exposure_pct | NUMERIC(10,4) | portfolio after trade |
| max_daily_loss_pct | NUMERIC(10,4) | |
| daily_pnl_pct | NUMERIC(10,4) | |
| metadata | JSONB | correlation checks, ADV, etc. |
| created_at | TIMESTAMPTZ | |

### `agent_metrics` — per-agent accuracy (Memory)
| column | type |
|---|---|
| id | BIGSERIAL PK |
| agent_name | VARCHAR(32) |
| metric_name | VARCHAR(32) | accuracy/precision/recall/f1/pnl_attribution/calibration |
| value | NUMERIC(12,6) |
| window | VARCHAR(16) | 7d/30d/90d/all |
| computed_at | TIMESTAMPTZ |

Index `(agent_name, metric_name, computed_at DESC)`.

### `strategy_performance` — per-strategy backtest/live stats
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| strategy | VARCHAR(64) | |
| mode | VARCHAR(16) | |
| period_start / period_end | DATE | |
| total_return | NUMERIC(12,6) | |
| sharpe / sortino | NUMERIC(12,6) | |
| max_drawdown | NUMERIC(12,6) | |
| win_rate | NUMERIC(10,4) | |
| profit_factor | NUMERIC(12,6) | |
| trades_count | INT | |
| final_equity | NUMERIC(20,2) | |

`UNIQUE (strategy, mode, period_start, period_end)`.

### `model_registry` — ML model versions
| column | type |
|---|---|
| id | BIGSERIAL PK |
| name | VARCHAR(64) |
| version | INT |
| artifact_path | VARCHAR(512) |
| hyperparams | JSONB |
| offline_metrics | JSONB |
| is_active | BOOL |
| status | VARCHAR(16) | training/validated/promoted/retired |
| trained_at | TIMESTAMPTZ |
| training_window | VARCHAR(64) |

### `backtest_runs` — every backtest execution
| column | type |
|---|---|
| id | BIGSERIAL PK |
| name | VARCHAR(128) |
| universe | JSONB |
| start / end | DATE |
| initial_capital | NUMERIC(20,2) |
| final_capital | NUMERIC(20,2) |
| metrics | JSONB |
| status | VARCHAR(16) |
| created_at | TIMESTAMPTZ |

### `events` — outbox / audit journal
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| event_uuid | UUID UNIQUE | idempotency |
| type | VARCHAR(64) | event class name |
| payload | JSONB | |
| occurred_at | TIMESTAMPTZ | |
| processed_at | TIMESTAMPTZ NULL | |
| error | TEXT NULL | consumer failure detail |

Indexes: `(type, occurred_at)`, `(processed_at)`.

### `system_logs`
| column | type |
|---|---|
| id | BIGSERIAL PK |
| level | VARCHAR(8) |
| component | VARCHAR(64) |
| message | TEXT |
| context | JSONB |
| created_at | TIMESTAMPTZ |

---

## 2. Redis Keyspaces

| purpose | key pattern | type | TTL |
|---|---|---|---|
| latest quote cache | `quote:{symbol}` | hash | 30s |
| top scan rankings | `scan:top:{metric}` | zset | 5m |
| indicator cache | `ind:{symbol}:{interval}` | string(json) | 5m |
| portfolio snapshot | `portfolio:{id}` | hash | 30s |
| execution lock | `lock:order:{portfolio}:{symbol}` | string | 30s |
| daily-loss counter | `risk:daily:{portfolio}:{date}` | string | 1d |
| rate limit (providers/LLM) | `rl:{key}` | zset/counter | window |
| event bus topic | `bus:{topic}` | pub/sub | — |
| job queue | arq default keys | zset | — |
| LLM response cache | `llm:{hash(text)}` | string | 24h |

---

## 3. Migrations

- Alembic, async engine, one migration file per change; `alembic upgrade head` runs in Docker entrypoint.
- Migration policy: additive-first; backfill in the same migration where required; destructive changes only behind explicit release notes.
