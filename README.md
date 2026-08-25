# Volume Profile + ICT Intraday Backtesting & Forward Trading Engine

An automated intraday trading research, backtesting, and forward execution system combining **Fixed Range Volume Profile (VAH / VAL / POC)** and **ICT (Inner Circle Trader) Market Structure Concepts** (Session Liquidity Sweeps, CISD - Change in State of Delivery, FVG/IFVG - Fair Value Gaps, Fixed 3R Target, and Causal Risk Controls).

---

## 📌 Features & Strategy Overview

* **Market Focus:** NAS100 (USATECH), SPX500 (USA500), Gold (XAUUSD), Silver (XAGUSD), EURUSD, GBPUSD.
* **Execution Timeframes:** 3-minute & 5-minute charts.
* **Trading Window:** New York Morning Cash Session (09:30 - 12:00 NY, excluding lunch).
* **Volume Profile Engine:**
  * Profile Range: Previous day 18:00 NY to trade date 09:30 NY.
  * Value Area Volume: 70%, 1000 price rows.
  * Computes dynamic VAH (Value Area High), VAL (Value Area Low), and POC (Point of Control).
* **Setup Criteria:**
  1. Price arrives near VAH / VAL.
  2. Untaken Session Liquidity Sweep (Asia High/Low, London High/Low, NY AM/PM High/Low, or Previous Day High/Low).
  3. Reversal confirmed via Bullish/Bearish **CISD** (Change in State of Delivery).
  4. Entry on **FVG** (Fair Value Gap) or **IFVG** (Inversion Fair Value Gap) retracement.
  5. Initial Stop: Sweep wick / CISD body stop; Target: Fixed 3R (Customizable RR).
  6. Maximum daily trade limit per symbol and pair-level daily loss cap (-1.0R).
* **Execution & Forward Shadow:**
  * Full deterministic backtesting engine with intrabar execution and pending limit lifecycle resolution.
  * MetaTrader 5 (MT5) demo forward shadow adapter with real-time tick/bar streaming, watchdog monitoring, and flat position verification.
  * Cryptographic candidate sealing and immutable engine manifest contracts for zero-leakage reproducibility.

---

## 🚀 Getting Started

### 1. Prerequisites
* Python 3.11+ (tested on Python 3.11, 3.12, 3.14)
* MetaTrader 5 (optional, for live forward execution on Windows)

### 2. Installation

Clone the repository and install the project:

```bash
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name/work/backtest

# Install dependencies and local package
pip install -e .
```

To install test dependencies:
```bash
pip install -e ".[test]"
```

---

## 🧪 Running Automated Tests

The repository includes an extensive test suite (233+ unit and integration tests) verifying data integrity, CISD detection, FVG/IFVG lifecycle, risk causality, execution replay, and MT5 forward contracts.

Run the tests with:
```bash
cd work/backtest
pytest tests
```

---

## 📊 Backtesting Usage

### Inspect Market Data CSVs
Inspect exported TradingView or Dukascopy CSV files for gaps, date ranges, and volume validity:
```bash
python -m backtest.cli inspect data/raw --output outputs/reports/data_inspection.csv
```

### Run Backtest (5-minute)
```bash
python -m backtest.cli run data/raw --symbol CAPITALCOM_NAS100 --timeframe 5m --output-dir outputs/reports/5m
```

### Run Backtest (3-minute) with Filters
```bash
python -m backtest.cli run data/raw --symbol DUKASCOPY_USATECHIDXUSD --timeframe 3m --max-trades-per-day 1 --reward-r 3.0 --output-dir outputs/reports/3m_nas100
```

### Command Line Options:
* `--symbol`: Target symbol (e.g. `CAPITALCOM_NAS100`, `CAPITALCOM_SPX500`, `DUKASCOPY_USATECHIDXUSD`, `DUKASCOPY_XAGUSD`)
* `--timeframe`: `3m` or `5m`
* `--max-trades-per-day`: Daily trade limit override (default: 2)
* `--reward-r`: Reward-to-risk multiple (e.g. `1.0`, `2.0`, `3.0`, `4.0`)
* `--entry-mode`: `start`, `quarter_25`, `midpoint`, `body_end`, `ote_705`, `cisd_close`
* `--stop-model`: `sweep_wick`, `cisd_body`, `fvg_opposite_edge`, `swing_based`
* `--allowed-weekdays`: e.g. `Monday,Wednesday,Friday`

---

## ⚙️ Forward Testing & MT5 Operations

Tek yetkili Windows ve Super1 operasyonel prosedürü için [docs/LIVE_OPERATIONS_TR.md](docs/LIVE_OPERATIONS_TR.md) belgesini inceleyin.

### Önemli Güvenlik ve Yapılandırma Kuralları:
* `.env` dosyası runtime süreçleri tarafından **otomatik yüklenmez**.
* `XM_MT5_LOGIN` ve `XM_MT5_PASSWORD` düz metin olarak kullanılmaz/desteklenmez.
* Gerçek ortam değişkenleri: `XM_MT5_SERVER`, `XM_MT5_READ_ONLY_PASSWORD` ve `XM_MT5_TERMINAL_PATH`.
* Windows üretim ortamında MT5 parolası asla düz metin saklanmaz; **yalnız DPAPI / launcher üzerinden** şifreli olarak enjekte edilir.
* **Tek Desteklenen Rollout Sırası**: `Signed Staging` → `Signed Upgrade` → `Flat Check` → `Sealed Rollover`.
* Eski yükleme betikleri (`install_super1_windows.ps1`, `finalize_super1_fresh_windows.ps1`, `repair_super1_task_s4u_windows.ps1`) **`LEGACY — DO NOT USE`** olarak işaretlenmiştir.

### Komut Satırı / Runner Kullanımı:
Forward runner komutları her zaman geçerli bir alt komut (`status`, `doctor`, `daily-health`, `run-once`, `smoke-order`) ile çağrılmalıdır:
```powershell
python scripts/run_super1_xm_mt5_forward.py status --output-root C:\Super1\state
```

---

## 📁 Repository Structure

```text
├── README.md                      # Root documentation
├── .gitignore                     # Git exclusion rules (sanitizes data & credentials)
├── .env.example                   # Environment variable template
├── docs/
│   └── LIVE_OPERATIONS_TR.md      # Tek yetkili Windows/Super1 operasyonel prosedürü
├── work/
│   └── backtest/
│       ├── pyproject.toml         # Package definition and pytest configuration
│       ├── backtest/              # Core Python package
│       │   ├── config.py          # Symbol and strategy configurations
│       │   ├── data_loader.py     # CSV loader & duplicate/gap validation
│       │   ├── volume_profile.py  # Fixed Range Volume Profile calculation
│       │   ├── strategy.py        # Strategy engine (CISD, FVG, exits)
│       │   ├── manual_state.py    # Explainable state machine & HTF arrays
│       │   ├── engine_pipeline.py # Multi-leg execution & risk pipeline
│       │   ├── risk.py            # Causal portfolio risk & daily loss cap
│       │   └── cli.py             # Command line interface
│       ├── deploy/                # Signed deployment and upgrade pipeline
│       ├── scripts/               # Download, diagnostic, and forward scripts
│       ├── tests/                 # Automated pytest suite (236+ tests)
│       └── live_forward/          # MT5 configuration templates
```

---

## 🔒 Security & Privacy Notice

* All historical CSV datasets, private account IDs, server logins, and proprietary compiled binaries are excluded from version control via `.gitignore`.
* Never commit real MT5 trading passwords or live funded account details.

---

## 📄 License
This project is for research and educational purposes.
