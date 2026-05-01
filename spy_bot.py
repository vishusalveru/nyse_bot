"""
=============================================================
  NYSE SPY Options Scalping Bot
  ─────────────────────────────────────────────
  INSTRUMENT   : SPY Options (S&P 500)
  STRATEGIES   : FVG+BOS | ORB | VWAP | Breakaway Gap
  DATA         : Alpaca Markets API (real-time)
  ORDERS       : Alpaca Paper Trading
  SESSIONS     : Pre-market + Regular
  SL           : 2 SPY points
  TARGET       : 1.5 SPY points (fixed)
  STRONG FVG   : Trailing SL 1.5 points
  CAPITAL      : $500/trade (1 contract)
  MAX TRADES   : 10/day
  DAILY LOSS   : $1,000
  DAILY PROFIT : $750
  LOGS         : spy_scan_log.csv | spy_trade_log.csv
  ALERTS       : Telegram
=============================================================
"""

import time
import logging
import datetime
import csv
import os
import threading
import requests
import pandas as pd
import pytz
from bs4 import BeautifulSoup
import alpaca_trade_api as tradeapi
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("spy_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS
# ─────────────────────────────────────────────
SL_POINTS           = 2.0
TARGET_POINTS       = 1.5
TRAIL_DISTANCE      = 1.5
TRAIL_START         = 2.0
STRONG_FVG_GAP      = 1.0     # SPY points
STRONG_FVG_BODY     = 1.5
BREAKAWAY_GAP_OPEN  = 2.0
BREAKAWAY_GAP_INTRA = 1.5
ORB_END_TIME        = datetime.time(10, 0)   # 30 min ORB
MAX_TRADES          = 10
CAPITAL_PER_TRADE   = 500     # USD per trade
DAILY_LOSS_LIMIT    = 1000    # USD
DAILY_PROFIT_TARGET = 750     # USD
SPY_SYMBOL          = "SPY"

# ─────────────────────────────────────────────
#  TIMEZONE
# ─────────────────────────────────────────────
EST  = pytz.timezone("US/Eastern")
IST  = pytz.timezone("Asia/Kolkata")

def now_est():
    return datetime.datetime.now(EST)

def est_time():
    return now_est().time()

# Market sessions EST
PREMARKET_START  = datetime.time(4,  0)
MARKET_START     = datetime.time(9, 30)
MARKET_END       = datetime.time(16, 0)
PREMARKET_END    = datetime.time(9, 30)


# ─────────────────────────────────────────────
#  ALPACA CLIENT
# ─────────────────────────────────────────────
def get_alpaca():
    return tradeapi.REST(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        config.ALPACA_BASE_URL
    )


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id"   : config.CHAT_ID,
            "text"      : message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.warning(f"Telegram failed: {resp.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")


# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias           = "neutral"
        self.last_update_id = 0
        self._running       = False

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()
        log.info("Telegram listener started")

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset" : self.last_update_id + 1,
                    "timeout": 30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result", []):
                    self.last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip().lower()
                    if text.startswith("/usbias"):
                        parts = text.split()
                        if len(parts) >= 2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"US Bias updated: {self.bias.upper()}")
                    elif text == "/usstatus":
                        t = now_est()
                        send_telegram(
                            f"SPY Bot Status\n"
                            f"Running  : YES\n"
                            f"US Bias  : {self.bias.upper()}\n"
                            f"EST Time : {t.strftime('%H:%M:%S')}\n"
                            f"IST Time : {datetime.datetime.now(IST).strftime('%H:%M:%S')}"
                        )
                    elif text == "/ushelp":
                        send_telegram(
                            "SPY Bot Commands:\n"
                            "/usbias bullish\n"
                            "/usbias bearish\n"
                            "/usbias neutral\n"
                            "/usstatus"
                        )
            except Exception as e:
                log.error(f"TG poll error: {e}")
                time.sleep(5)


# ─────────────────────────────────────────────
#  MARKET DATA — Alpaca
# ─────────────────────────────────────────────
def get_spy_ltp(api):
    try:
        trade = api.get_latest_trade(SPY_SYMBOL)
        return float(trade.price)
    except Exception as e:
        log.error(f"SPY LTP error: {e}")
        return None

def get_candles(api, interval="5Min", limit=50):
    """
    Fetch SPY OHLCV bars from Alpaca.
    interval: '1Min', '5Min', '15Min'
    """
    try:
        bars = api.get_bars(
            SPY_SYMBOL,
            interval,
            limit=limit,
            adjustment="raw"
        ).df
        if bars.empty:
            return None
        bars = bars.reset_index()
        bars.columns = [c.lower() for c in bars.columns]
        bars = bars.rename(columns={"timestamp":"timestamp",
                                     "open":"open","high":"high",
                                     "low":"low","close":"close",
                                     "volume":"volume"})
        for col in ["open","high","low","close","volume"]:
            bars[col] = bars[col].astype(float)
        log.info(f"SPY {len(bars)} bars [{interval}] fetched")
        return bars
    except Exception as e:
        log.error(f"Candle error: {e}")
        return None

def get_prev_day_ohlc(api):
    try:
        bars = api.get_bars(SPY_SYMBOL, "1D", limit=3).df
        if len(bars) < 2:
            return None
        prev = bars.iloc[-2]
        return {
            "open" : float(prev["open"]),
            "high" : float(prev["high"]),
            "low"  : float(prev["low"]),
            "close": float(prev["close"])
        }
    except Exception as e:
        log.error(f"Prev OHLC error: {e}")
        return None


# ─────────────────────────────────────────────
#  NEWS & SENTIMENT — US Market
# ─────────────────────────────────────────────
def fetch_fear_greed():
    """Fetch CNN Fear & Greed Index."""
    try:
        resp = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data  = resp.json()
        score = float(data["fear_and_greed"]["score"])
        rating = data["fear_and_greed"]["rating"].lower()
        if score >= 60:   sentiment = "bullish"
        elif score <= 40: sentiment = "bearish"
        else:             sentiment = "neutral"
        log.info(f"Fear & Greed: {score} ({rating}) → {sentiment}")
        return score, rating, sentiment
    except Exception as e:
        log.error(f"Fear & Greed error: {e}")
        return 50, "neutral", "neutral"

def fetch_us_news():
    """Scrape Yahoo Finance for US market headlines."""
    try:
        resp  = requests.get(
            "https://finance.yahoo.com/topic/stock-market-news/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        soup  = BeautifulSoup(resp.text, "html.parser")
        heads = []
        for tag in soup.find_all(["h3","h4"], limit=30):
            text = tag.get_text(strip=True)
            if len(text) > 20:
                tl = text.lower()
                if any(w in tl for w in ["s&p","spy","market","stocks",
                                          "fed","rally","sell","nasdaq"]):
                    heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:8]
        bull  = ["rally","surge","gain","rise","bullish","positive",
                 "strong","up","buy","support","recovery","boost"]
        bear  = ["fall","drop","decline","bearish","negative","weak",
                 "down","sell","crash","pressure","recession","fear"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        sent  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        return heads, sent, score
    except Exception as e:
        log.error(f"US news error: {e}")
        return [], "neutral", 0

def compute_bias(fg_sentiment, news_sentiment, user_bias):
    m = {"bullish":1,"neutral":0,"bearish":-1}
    s = (m.get(fg_sentiment,0)  * 0.35 +
         m.get(news_sentiment,0) * 0.25 +
         m.get(user_bias,0)      * 0.40)
    return "bullish" if s >= 0.35 else "bearish" if s <= -0.35 else "neutral"


# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap(df):
    df = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]  = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"] / df["cum_vol"]
    return df

def detect_trend(df):
    if df is None or len(df) < 4:
        return "neutral", "Not enough candles"
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh = all(highs[i] > highs[i-1] for i in range(1, len(highs)))
    hl = all(lows[i]  > lows[i-1]  for i in range(1, len(lows)))
    ll = all(lows[i]  < lows[i-1]  for i in range(1, len(lows)))
    lh = all(highs[i] < highs[i-1] for i in range(1, len(highs)))
    if hh and hl:
        return "bullish", f"HH+HL | H:{[round(h,2) for h in highs]}"
    if ll and lh:
        return "bearish", f"LL+LH | L:{[round(l,2) for l in lows]}"
    return "neutral", f"No structure | H:{[round(h,2) for h in highs]}"

def detect_bos(df, trend):
    if df is None or len(df) < 6:
        return False, 0
    recent     = df.tail(10)
    last_close = float(recent["close"].iloc[-1])
    if trend == "bullish":
        swing_high = float(recent["high"].iloc[:-1].max())
        if last_close > swing_high:
            return True, swing_high
    elif trend == "bearish":
        swing_low = float(recent["low"].iloc[:-1].min())
        if last_close < swing_low:
            return True, swing_low
    return False, 0

def detect_fvg(df):
    if df is None or len(df) < 3:
        return None, "Not enough candles"
    candles = df.tail(15)
    for i in range(len(candles)-1, 1, -1):
        c1   = candles.iloc[i-2]
        c2   = candles.iloc[i-1]
        c3   = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < 0.5: continue
        c1h  = float(c1["high"]); c1l = float(c1["low"])
        c3h  = float(c3["high"]); c3l = float(c3["low"])
        if c1h < c3l:
            size   = round(c3l - c1h, 3)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {
                "type":"bullish","top":round(c3l,3),
                "bottom":round(c1h,3),"mid":round((c3l+c1h)/2,3),
                "size":size,"strong":strong
            }, f"{'STRONG' if strong else 'WEAK'} Bullish FVG | Gap:${size:.2f} | Body:${body:.2f}"
        if c1l > c3h:
            size   = round(c1l - c3h, 3)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {
                "type":"bearish","top":round(c1l,3),
                "bottom":round(c3h,3),"mid":round((c1l+c3h)/2,3),
                "size":size,"strong":strong
            }, f"{'STRONG' if strong else 'WEAK'} Bearish FVG | Gap:${size:.2f} | Body:${body:.2f}"
    return None, "No FVG in last 15 candles"

def detect_breakaway_gap(df, prev_close):
    if df is None or len(df) < 2:
        return None, "Not enough candles"
    first_open = float(df["open"].iloc[0])
    if prev_close:
        gap = abs(first_open - prev_close)
        if gap >= BREAKAWAY_GAP_OPEN:
            direction = "bullish" if first_open > prev_close else "bearish"
            return {
                "type":direction,"gap_type":"gap_open",
                "size":round(gap,3),"level":round(prev_close,3),"strong":True
            }, f"Gap open {direction} | ${gap:.2f} from prev close ${prev_close:.2f}"
    candles = df.tail(10)
    for i in range(len(candles)-1, 0, -1):
        curr = candles.iloc[i]; prev = candles.iloc[i-1]
        body = abs(float(curr["close"]) - float(curr["open"]))
        if body < BREAKAWAY_GAP_INTRA: continue
        if float(curr["low"]) > float(prev["high"]):
            return {
                "type":"bullish","gap_type":"intraday",
                "size":round(body,3),"level":float(prev["high"]),"strong":True
            }, f"Intraday breakaway bullish | ${body:.2f}"
        if float(curr["high"]) < float(prev["low"]):
            return {
                "type":"bearish","gap_type":"intraday",
                "size":round(body,3),"level":float(prev["low"]),"strong":True
            }, f"Intraday breakaway bearish | ${body:.2f}"
    return None, "No breakaway gap"

def detect_orb(df, orb_high, orb_low):
    if orb_high is None or orb_low is None:
        return None, "ORB not formed yet"
    if df is None or len(df) < 1:
        return None, "No candles"
    last  = df.iloc[-1]
    close = float(last["close"])
    body  = abs(float(last["close"]) - float(last["open"]))
    if close > orb_high and body >= 0.5:
        return {"type":"bullish","level":orb_high,
                "size":round(close-orb_high,3)
                }, f"ORB bullish | Close:${close:.2f} > High:${orb_high:.2f}"
    if close < orb_low and body >= 0.5:
        return {"type":"bearish","level":orb_low,
                "size":round(orb_low-close,3)
                }, f"ORB bearish | Close:${close:.2f} < Low:${orb_low:.2f}"
    return None, f"No ORB | Range:${orb_low:.2f}-${orb_high:.2f}"

def detect_vwap_rejection(df_5, df_15):
    if df_5 is None or len(df_5) < 5: return None, "Not enough 5min candles"
    if df_15 is None or len(df_15) < 4: return None, "Not enough 15min candles"
    trend_15, _ = detect_trend(df_15)
    if trend_15 == "neutral": return None, "15min trend neutral"
    df_5    = calc_vwap(df_5)
    avg_vol = float(df_5["volume"].mean())
    last    = df_5.iloc[-1]; prev = df_5.iloc[-2]
    vwap    = float(last["vwap"])
    close   = float(last["close"])
    low     = float(last["low"]); high = float(last["high"])
    volume  = float(last["volume"])
    vol_surge = volume > avg_vol * 1.5
    if trend_15 == "bullish":
        touched = low <= vwap <= high or float(prev["low"]) <= vwap
        if touched and close > vwap and vol_surge:
            return {"type":"bullish","vwap":round(vwap,3),
                    "volume":round(volume,0),"avg_volume":round(avg_vol,0)
                    }, f"VWAP bullish | ${vwap:.2f} | Vol:{volume:.0f}"
    if trend_15 == "bearish":
        touched = low <= vwap <= high or float(prev["high"]) >= vwap
        if touched and close < vwap and vol_surge:
            return {"type":"bearish","vwap":round(vwap,3),
                    "volume":round(volume,0),"avg_volume":round(avg_vol,0)
                    }, f"VWAP bearish | ${vwap:.2f} | Vol:{volume:.0f}"
    return None, f"No VWAP | ${vwap:.2f} | VolSurge:{vol_surge}"

def is_retesting(price, bottom, top):
    return bottom <= price <= top


# ─────────────────────────────────────────────
#  OPTION DETAILS — SPY
# ─────────────────────────────────────────────
def get_option_details(spy_price, option_type):
    """
    Get slightly OTM SPY weekly option details.
    Strike rounded to nearest $1
    """
    atm    = round(spy_price)
    strike = atm + 2 if option_type == "CALL" else atm - 2
    # Next Friday expiry
    today       = datetime.date.today()
    days_to_fri = (4 - today.weekday()) % 7
    if days_to_fri == 0: days_to_fri = 7
    expiry = today + datetime.timedelta(days=days_to_fri)
    exp_str = expiry.strftime("%Y-%m-%d")
    symbol  = f"SPY{expiry.strftime('%y%m%d')}{'C' if option_type=='CALL' else 'P'}{strike*1000:08d}"
    return strike, expiry, exp_str, symbol


# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium,
                 signal, fg_score, user_bias, pre_bias, is_strong=False):
        self.trade_no    = trade_no
        self.strategy    = strategy
        self.direction   = direction
        self.entry_price = entry_price
        self.option_type = option_type
        self.strike      = strike
        self.expiry      = expiry
        self.premium     = premium
        self.signal      = signal
        self.fg_score    = fg_score
        self.user_bias   = user_bias
        self.pre_bias    = pre_bias
        self.is_strong   = is_strong
        self.entry_time  = now_est().strftime("%H:%M:%S EST")
        self.start_time  = time.time()
        self.be_moved    = False
        self.trailing    = is_strong
        self.best_price  = entry_price
        self.sl_price    = (entry_price - SL_POINTS if direction == "bullish"
                            else entry_price + SL_POINTS)
        self.tgt_price   = (entry_price + TARGET_POINTS if direction == "bullish"
                            else entry_price - TARGET_POINTS)
        mode = "TRAILING" if is_strong else "FIXED"
        log.info(f"SPY Trade #{trade_no} | {strategy} | {direction} | {mode} | ${entry_price:.2f}")

    def check(self, ltp):
        if self.trailing:
            if self.direction == "bullish" and ltp > self.best_price:
                self.best_price = ltp
                profit = ltp - self.entry_price
                if profit >= TRAIL_START:
                    new_sl = round(ltp - TRAIL_DISTANCE, 3)
                    if new_sl > self.sl_price:
                        self.sl_price = new_sl
                        tg("📈", f"SPY Trade #{self.trade_no} Trail SL",
                           [f"SPY    : ${ltp:.2f}",
                            f"Profit : +${profit:.2f}",
                            f"New SL : ${new_sl:.2f}"])
            elif self.direction == "bearish" and ltp < self.best_price:
                self.best_price = ltp
                profit = self.entry_price - ltp
                if profit >= TRAIL_START:
                    new_sl = round(ltp + TRAIL_DISTANCE, 3)
                    if new_sl < self.sl_price:
                        self.sl_price = new_sl
                        tg("📉", f"SPY Trade #{self.trade_no} Trail SL",
                           [f"SPY    : ${ltp:.2f}",
                            f"Profit : +${profit:.2f}",
                            f"New SL : ${new_sl:.2f}"])
            if self.direction == "bullish" and ltp <= self.sl_price: return "sl"
            if self.direction == "bearish" and ltp >= self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half = (self.entry_price + self.tgt_price) / 2
                cond = (ltp >= half if self.direction == "bullish" else ltp <= half)
                if cond:
                    self.be_moved = True
                    self.sl_price = self.entry_price
                    tg("🔒", f"SPY Trade #{self.trade_no} Breakeven",
                       [f"SPY    : ${ltp:.2f}",
                        f"New SL : ${self.entry_price:.2f}"])
            if self.direction == "bullish":
                if ltp >= self.tgt_price: return "target"
                if ltp <= self.sl_price:  return "sl"
            else:
                if ltp <= self.tgt_price: return "target"
                if ltp >= self.sl_price:  return "sl"
        return None

    def duration(self):
        return round((time.time() - self.start_time) / 60, 1)

    def calc_pnl(self, exit_price):
        pts   = (exit_price - self.entry_price if self.direction == "bullish"
                 else self.entry_price - exit_price)
        delta = 0.4
        return round(pts * delta * 100, 2)   # 100 shares per contract


# ─────────────────────────────────────────────
#  CSV LOGS
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime_est","datetime_ist","spy_ltp","trend_15m",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "bos_confirmed","breakaway_found","breakaway_type",
    "orb_high","orb_low","orb_signal",
    "vwap_signal","vwap_level",
    "fear_greed_score","fear_greed_rating",
    "news_sentiment","user_bias","overall_bias",
    "session","entry_condition_met","strategy_triggered","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","session",
    "entry_time_est","exit_time_est",
    "pre_bias","user_bias","fear_greed",
    "direction","is_strong","exit_mode",
    "entry_spy","exit_spy","points_moved",
    "option_type","strike","expiry",
    "premium_est","contracts","capital_usd",
    "pnl_usd","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl","notes"
]

def init_logs():
    for fname, cols in [("spy_scan_log.csv", SCAN_COLS),
                         ("spy_trade_log.csv", TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
    log.info("SPY logs initialised")

def write_scan(rec):
    with open("spy_scan_log.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("spy_trade_log.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, fg_score):
    wr = (stats["wins"]/stats["trades"]*100) if stats["trades"] > 0 else 0
    tg("📊", "SPY DAILY SUMMARY",
       [f"Pre-bias      : {pre_bias.upper()}",
        f"Fear & Greed  : {fg_score}",
        f"Trades        : {stats['trades']}",
        f"Wins          : {stats['wins']}",
        f"Losses        : {stats['losses']}",
        f"Win rate      : {wr:.1f}%",
        f"Total P&L     : ${stats['pnl']:+.2f}",
        f"FVG trades    : {stats.get('fvg_trades',0)}",
        f"ORB trades    : {stats.get('orb_trades',0)}",
        f"VWAP trades   : {stats.get('vwap_trades',0)}",
        f"Strong(trail) : {stats.get('strong_trades',0)}",
        f"Scan log      : spy_scan_log.csv",
        f"Trade log     : spy_trade_log.csv"])


# ─────────────────────────────────────────────
#  OPEN TRADE HELPER
# ─────────────────────────────────────────────
def open_trade(trade_no, strategy, direction, entry_price,
               fg_score, tg_listener, pre_bias, is_strong, signal, session):
    opt_type         = "CALL" if direction == "bullish" else "PUT"
    strike, expiry, exp_str, sym = get_option_details(entry_price, opt_type)
    premium          = CAPITAL_PER_TRADE / 100   # per share estimate
    trade            = PaperTrade(
        trade_no=trade_no, strategy=strategy,
        direction=direction, entry_price=entry_price,
        option_type=opt_type, strike=strike,
        expiry=expiry, premium=premium,
        signal=signal, fg_score=fg_score,
        user_bias=tg_listener.bias,
        pre_bias=pre_bias, is_strong=is_strong
    )
    mode = "Trailing SL" if is_strong else f"Fixed ${TARGET_POINTS}"
    tg("🚀", f"SPY PAPER TRADE #{trade_no} — {strategy}",
       [f"Session   : {session.upper()}",
        f"Direction : {direction.upper()}",
        f"Option    : {opt_type} ${strike} | {exp_str}",
        f"SPY entry : ${entry_price:.2f}",
        f"SL        : ${trade.sl_price:.2f} (-${SL_POINTS})",
        f"Exit mode : {mode}",
        f"Capital   : ${CAPITAL_PER_TRADE}",
        f"Signal    : {signal}",
        f"NOTE      : PAPER TRADE"])
    return trade


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    api         = get_alpaca()
    tg_listener = TelegramListener()
    tg_listener.start()

    stats = {
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg_trades":0,"orb_trades":0,
        "vwap_trades":0,"strong_trades":0
    }

    trade_no       = 0
    active_trade   = None
    last_scan_time = None
    pre_bias       = "neutral"
    fg_score       = 50
    fg_rating      = "neutral"
    fg_sentiment   = "neutral"
    news_sent      = "neutral"
    premarket_done = False
    orb_high       = None
    orb_low        = None
    orb_formed     = False
    prev_ohlc      = None
    used_signals   = set()

    ist_now = datetime.datetime.now(IST).strftime("%H:%M IST")
    send_telegram(
        f"SPY Scalping Bot Started\n"
        f"IST Time  : {ist_now}\n"
        f"Strategies: FVG+BOS | ORB | VWAP | Breakaway\n"
        f"SL: ${SL_POINTS} | TGT: ${TARGET_POINTS} | Strong: Trail\n"
        f"Capital: ${CAPITAL_PER_TRADE}/trade | Max: {MAX_TRADES}\n"
        f"Loss limit: ${DAILY_LOSS_LIMIT} | Profit: ${DAILY_PROFIT_TARGET}\n\n"
        f"Commands:\n"
        f"/usbias bullish|bearish|neutral\n"
        f"/usstatus"
    )

    while True:
        t   = est_time()
        now = now_est()

        # Determine session
        if PREMARKET_START <= t < MARKET_START:
            session = "premarket"
        elif MARKET_START <= t < MARKET_END:
            session = "regular"
        else:
            session = "closed"

        # ── MARKET CLOSED ───────────────────────
        if session == "closed":
            # Send summary at market close
            if t >= MARKET_END and t < datetime.time(16, 5):
                send_summary(stats, pre_bias, fg_score)
                send_telegram(
                    f"NYSE Market Closed\n"
                    f"EST: {now.strftime('%H:%M')}\n"
                    f"IST: {datetime.datetime.now(IST).strftime('%H:%M')}\n"
                    f"Bot sleeping till pre-market tomorrow."
                )
                # Reset for next day
                stats = {
                    "trades":0,"wins":0,"losses":0,"timeouts":0,
                    "skipped":0,"pnl":0.0,"consec_loss":0,
                    "fvg_trades":0,"orb_trades":0,
                    "vwap_trades":0,"strong_trades":0
                }
                trade_no=0; active_trade=None; last_scan_time=None
                pre_bias="neutral"; orb_high=None; orb_low=None
                orb_formed=False; used_signals=set()
                premarket_done=False; tg_listener.bias="neutral"
            time.sleep(60)
            continue

        # ── PRE-MARKET ANALYSIS ──────────────────
        if not premarket_done and t >= PREMARKET_START:
            prev_ohlc              = get_prev_day_ohlc(api)
            fg_score, fg_rating, fg_sentiment = fetch_fear_greed()
            heads, news_sent, score = fetch_us_news()
            user_bias              = tg_listener.bias
            pre_bias               = compute_bias(fg_sentiment, news_sent, user_bias)

            icon = "📈" if pre_bias=="bullish" else "📉" if pre_bias=="bearish" else "➡️"
            tg(icon, f"SPY PRE-MARKET BIAS: {pre_bias.upper()}",
               [f"Fear & Greed : {fg_score} ({fg_rating})",
                f"News         : {news_sent.upper()} (score={score})",
                f"User bias    : {user_bias.upper()} (/usbias)",
                f"Prev close   : ${prev_ohlc['close'] if prev_ohlc else 'N/A'}",
                f"Prev high    : ${prev_ohlc['high'] if prev_ohlc else 'N/A'}",
                f"Prev low     : ${prev_ohlc['low'] if prev_ohlc else 'N/A'}",
                f"",
                f"Top headlines:",
                *[f"• {h[:80]}" for h in heads[:3]],
                f"",
                f"Overall bias : {pre_bias.upper()}",
                f"Session      : {session.upper()}",
                f"EST time     : {now.strftime('%H:%M')}",
                f"IST time     : {datetime.datetime.now(IST).strftime('%H:%M')}"])
            premarket_done = True

        # ── GUARDS ───────────────────────────────
        if stats["trades"] >= MAX_TRADES:
            time.sleep(30*60); continue
        if stats["consec_loss"] >= 3:
            tg("STOP","Risk Protection",
               [f"Consec losses: {stats['consec_loss']}",
                "Bot paused for today"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue
        if stats["pnl"] <= -DAILY_LOSS_LIMIT:
            tg("STOP","Daily Loss Limit Hit",
               [f"P&L: ${stats['pnl']:+.2f}",
                f"Limit: -${DAILY_LOSS_LIMIT}"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue
        if stats["pnl"] >= DAILY_PROFIT_TARGET:
            tg("DONE","Daily Profit Target Hit",
               [f"P&L: ${stats['pnl']:+.2f}",
                "Protecting gains!"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue

        # ── MONITOR ACTIVE TRADE ─────────────────
        if active_trade is not None:
            ltp    = get_spy_ltp(api)
            result = None
            if ltp: result = active_trade.check(ltp)
            if t >= MARKET_END:
                result = "timeout"
                ltp    = ltp or active_trade.entry_price
            if result:
                exit_time = now.strftime("%H:%M:%S EST")
                duration  = active_trade.duration()
                pnl       = active_trade.calc_pnl(ltp)
                pts_moved = round(ltp - active_trade.entry_price, 3) \
                            if active_trade.direction == "bullish" \
                            else round(active_trade.entry_price - ltp, 3)
                if result == "target":
                    icon="✅"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result == "sl":
                    icon="❌"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:
                    icon="⏰"; stats["timeouts"]+=1; stats["consec_loss"]=0
                stats["trades"] += 1
                stats["pnl"]    += pnl
                exit_mode = "Trail" if active_trade.trailing else "Fixed"
                tg(icon, f"SPY TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy  : {active_trade.strategy}",
                    f"Session   : {session.upper()}",
                    f"Exit mode : {exit_mode}",
                    f"Direction : {active_trade.direction.upper()}",
                    f"Entry SPY : ${active_trade.entry_price:.2f}",
                    f"Exit SPY  : ${ltp:.2f}",
                    f"Points    : ${pts_moved:+.2f}",
                    f"Duration  : {duration}min",
                    f"P&L       : ${pnl:+.2f}",
                    f"Day P&L   : ${stats['pnl']:+.2f}",
                    f"Trades    : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date"          : datetime.date.today(),
                    "trade_no"      : active_trade.trade_no,
                    "strategy"      : active_trade.strategy,
                    "session"       : session,
                    "entry_time_est": active_trade.entry_time,
                    "exit_time_est" : exit_time,
                    "pre_bias"      : pre_bias,
                    "user_bias"     : active_trade.user_bias,
                    "fear_greed"    : active_trade.fg_score,
                    "direction"     : active_trade.direction,
                    "is_strong"     : active_trade.is_strong,
                    "exit_mode"     : exit_mode,
                    "entry_spy"     : active_trade.entry_price,
                    "exit_spy"      : round(ltp, 3),
                    "points_moved"  : pts_moved,
                    "option_type"   : active_trade.option_type,
                    "strike"        : active_trade.strike,
                    "expiry"        : active_trade.expiry,
                    "premium_est"   : active_trade.premium,
                    "contracts"     : 1,
                    "capital_usd"   : CAPITAL_PER_TRADE,
                    "pnl_usd"       : pnl,
                    "result"        : result,
                    "be_triggered"  : active_trade.be_moved,
                    "trail_triggered": active_trade.trailing,
                    "duration_min"  : duration,
                    "consec_losses" : stats["consec_loss"],
                    "daily_pnl"     : stats["pnl"],
                    "notes"         : active_trade.signal
                })
                active_trade = None
                time.sleep(2*60)
            else:
                time.sleep(15)
            continue

        # ── FETCH DATA ───────────────────────────
        ltp   = get_spy_ltp(api)
        df_5  = get_candles(api, "5Min",  50)
        df_15 = get_candles(api, "15Min", 30)

        if ltp is None or df_5 is None:
            time.sleep(15); continue

        trend, trend_reason = detect_trend(df_15)

        # ── ORB FORMATION ────────────────────────
        if not orb_formed and t >= ORB_END_TIME and session == "regular":
            orb_df = df_5[pd.to_datetime(df_5["timestamp"]).dt.time <= ORB_END_TIME]
            if not orb_df.empty:
                orb_high   = float(orb_df["high"].max())
                orb_low    = float(orb_df["low"].min())
                orb_formed = True
                tg("📐","SPY ORB Range Formed",
                   [f"High : ${orb_high:.2f}",
                    f"Low  : ${orb_low:.2f}",
                    f"Size : ${orb_high-orb_low:.2f}",
                    f"Session: REGULAR"])

        # ── REFRESH FEAR & GREED every hour ──────
        if last_scan_time is None or (now_est()-last_scan_time).seconds >= 3600:
            fg_score, fg_rating, fg_sentiment = fetch_fear_greed()

        # ── RUN DETECTORS ────────────────────────
        fvg,      fvg_reason  = detect_fvg(df_5)
        bos,      bos_level   = detect_bos(df_5, trend)
        bgap,     bgap_reason = detect_breakaway_gap(
            df_5, prev_ohlc["close"] if prev_ohlc else None)
        orb_sig,  orb_reason  = detect_orb(df_5, orb_high, orb_low)
        vwap_sig, vwap_reason = detect_vwap_rejection(df_5, df_15)

        # ── 5-MIN SCAN LOG ───────────────────────
        do_scan = (last_scan_time is None or
                   (now_est()-last_scan_time).seconds >= 300)
        if do_scan:
            last_scan_time = now_est()
            strats = []
            if fvg and bos: strats.append("FVG+BOS")
            if bgap:        strats.append("Breakaway")
            if orb_sig:     strats.append("ORB")
            if vwap_sig:    strats.append("VWAP")
            entry_met = len(strats) > 0 and trend != "neutral"
            write_scan({
                "datetime_est"      : now.strftime("%Y-%m-%d %H:%M EST"),
                "datetime_ist"      : datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
                "spy_ltp"           : round(ltp, 3),
                "trend_15m"         : trend,
                "fvg_found"         : fvg is not None,
                "fvg_type"          : fvg["type"] if fvg else "",
                "fvg_strong"        : fvg["strong"] if fvg else "",
                "fvg_size"          : fvg["size"] if fvg else "",
                "bos_confirmed"     : bos,
                "breakaway_found"   : bgap is not None,
                "breakaway_type"    : bgap["gap_type"] if bgap else "",
                "orb_high"          : orb_high or "",
                "orb_low"           : orb_low or "",
                "orb_signal"        : orb_sig["type"] if orb_sig else "",
                "vwap_signal"       : vwap_sig["type"] if vwap_sig else "",
                "vwap_level"        : vwap_sig["vwap"] if vwap_sig else "",
                "fear_greed_score"  : fg_score,
                "fear_greed_rating" : fg_rating,
                "news_sentiment"    : news_sent,
                "user_bias"         : tg_listener.bias,
                "overall_bias"      : pre_bias,
                "session"           : session,
                "entry_condition_met": entry_met,
                "strategy_triggered": ",".join(strats),
                "reason"            : f"FVG:{fvg_reason}|ORB:{orb_reason}|VWAP:{vwap_reason}"
            })
            cond_icon = "✅" if entry_met else "⏸️"
            tg(cond_icon, f"SPY 5-MIN SCAN {now.strftime('%H:%M')} EST",
               [f"SPY LTP    : ${ltp:.2f}",
                f"Session    : {session.upper()}",
                f"Trend(15m) : {trend.upper()}",
                f"FVG        : {fvg_reason[:40] if fvg else 'NONE'}",
                f"BOS        : {'YES' if bos else 'NO'}",
                f"Breakaway  : {bgap_reason[:40] if bgap else 'NONE'}",
                f"ORB        : {orb_reason[:40]}",
                f"VWAP       : {vwap_reason[:40]}",
                f"F&G Index  : {fg_score} ({fg_rating})",
                f"Bias       : {pre_bias.upper()}",
                f"Signals    : {', '.join(strats) if strats else 'NONE'}",
                f"IST time   : {datetime.datetime.now(IST).strftime('%H:%M')}"])

        # ── STRATEGY 1: FVG + BOS ─────────────────
        if fvg and bos and trend != "neutral" and "FVG" not in used_signals:
            if fvg["type"] == trend and (pre_bias == "neutral" or pre_bias == trend):
                is_strong = fvg["strong"]
                retest_ok=False; entry_price=None
                start_wait=time.time()
                while time.time()-start_wait < 10*60:
                    cur = get_spy_ltp(api)
                    if cur and is_retesting(cur, fvg["bottom"], fvg["top"]):
                        retest_ok=True; entry_price=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no += 1
                    active_trade = open_trade(
                        trade_no, "FVG+BOS", trend, entry_price,
                        fg_score, tg_listener, pre_bias, is_strong,
                        f"FVG ${fvg['size']:.2f} BOS@${bos_level:.2f}", session
                    )
                    used_signals.add("FVG")
                    stats["fvg_trades"] += 1
                    if is_strong: stats["strong_trades"] += 1
                    time.sleep(15); continue
                else:
                    stats["skipped"] += 1

        # ── STRATEGY 1B: BREAKAWAY GAP ────────────
        if bgap and trend != "neutral" and "BGAP" not in used_signals:
            if bgap["type"] == trend and (pre_bias == "neutral" or pre_bias == trend):
                level=bgap["level"]
                retest_ok=False; entry_price=None
                start_wait=time.time()
                while time.time()-start_wait < 10*60:
                    cur = get_spy_ltp(api)
                    if cur and is_retesting(cur, level-0.3, level+0.3):
                        retest_ok=True; entry_price=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no += 1
                    active_trade = open_trade(
                        trade_no, "BreakawayGap", trend, entry_price,
                        fg_score, tg_listener, pre_bias, True,
                        f"Bgap {bgap['gap_type']} ${bgap['size']:.2f}", session
                    )
                    used_signals.add("BGAP")
                    stats["fvg_trades"] += 1
                    stats["strong_trades"] += 1
                    time.sleep(15); continue
                else:
                    stats["skipped"] += 1

        # ── STRATEGY 2: ORB ───────────────────────
        if orb_sig and orb_formed and "ORB" not in used_signals:
            if session == "regular":
                if pre_bias == "neutral" or pre_bias == orb_sig["type"]:
                    level=orb_sig["level"]
                    retest_ok=False; entry_price=None
                    start_wait=time.time()
                    while time.time()-start_wait < 10*60:
                        cur = get_spy_ltp(api)
                        if cur and is_retesting(cur, level-0.3, level+0.3):
                            retest_ok=True; entry_price=cur; break
                        time.sleep(15)
                    if retest_ok:
                        trade_no += 1
                        active_trade = open_trade(
                            trade_no, "ORB", orb_sig["type"], entry_price,
                            fg_score, tg_listener, pre_bias, False,
                            f"ORB {orb_sig['type']} ${orb_sig['size']:.2f}", session
                        )
                        used_signals.add("ORB")
                        stats["orb_trades"] += 1
                        time.sleep(15); continue
                    else:
                        stats["skipped"] += 1

        # ── STRATEGY 3: VWAP ──────────────────────
        if vwap_sig and "VWAP" not in used_signals:
            if pre_bias == "neutral" or pre_bias == vwap_sig["type"]:
                cur = get_spy_ltp(api)
                if cur:
                    trade_no += 1
                    active_trade = open_trade(
                        trade_no, "VWAP", vwap_sig["type"], cur,
                        fg_score, tg_listener, pre_bias, False,
                        f"VWAP {vwap_sig['type']} @ ${vwap_sig['vwap']:.2f}", session
                    )
                    used_signals.add("VWAP")
                    stats["vwap_trades"] += 1
                    time.sleep(15); continue

        time.sleep(60)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("SPY Bot stopped")
        send_telegram("SPY Bot stopped manually.")
