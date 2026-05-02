"""
=============================================================
  NYSE SPY Options Scalping Bot v3
  ─────────────────────────────────────────────
  PATCHES APPLIED:
  #1  ORB breakout detected AT formation time
  #2  Trend relaxed to 3/4 candles HH+HL
  #3  FVG body filter fixed for SPY scale (0.5)
  #4  Candle cache cleared every scan
  #5  Telegram gated — no alerts when closed
  #6  F&G refresh every 30min with fallback
  #7  RVOL filter — skip if volume < 1.5x avg
  #8  OBV direction confirmation
  #9  VWAP bands ±1SD ±2SD
  #10 Multi-timeframe trend 5m+15m+30m
  #11 Alpaca auto-bias from pre-market data
  #12 1:00 PM IST bias reminder via Telegram
  #13 35+ scan columns
  #14 EMA 9/21/50
  #15 Daily CSV auto-send at session end
  #16 Same quant volume for Nifty (separate file)
=============================================================
"""

import time
import logging
import datetime
import csv
import os
import threading
import requests
import numpy as np
import pandas as pd
import pytz
from bs4 import BeautifulSoup
import alpaca_trade_api as tradeapi
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("spy_v3.log"),
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
STRONG_FVG_GAP      = 1.0
STRONG_FVG_BODY     = 0.5     # PATCH #3: fixed from 1.5 to 0.5
MIN_FVG_BODY        = 0.5     # PATCH #3: fixed from 15 to 0.5
BREAKAWAY_GAP_OPEN  = 2.0
BREAKAWAY_GAP_INTRA = 1.0     # PATCH #3: fixed from 1.5 to 1.0
ORB_END_TIME        = datetime.time(10, 0)
MAX_TRADES          = 10
CAPITAL_PER_TRADE   = 500
DAILY_LOSS_LIMIT    = 1000
DAILY_PROFIT_TARGET = 750
SPY_SYMBOL          = "SPY"
MIN_RVOL            = 1.5     # PATCH #7: minimum relative volume
TREND_CANDLES_MIN   = 3       # PATCH #2: relaxed from 4/4 to 3/4

EST = pytz.timezone("US/Eastern")
IST = pytz.timezone("Asia/Kolkata")

def now_est():
    return datetime.datetime.now(EST)

def now_ist():
    return datetime.datetime.now(IST)

def est_time():
    return now_est().time()

def ist_time():
    return now_ist().time()

PREMARKET_START = datetime.time(4,  0)
MARKET_START    = datetime.time(9, 30)
MARKET_END      = datetime.time(16, 0)
IST_REMINDER    = datetime.time(13, 0)   # PATCH #12: 1PM IST reminder
IST_PREMARKET   = datetime.time(13, 30)  # 1:30PM IST = premarket open


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

# PATCH #15: Send CSV files to Telegram
def send_csv_files():
    files = [
        ("spy_scan_log_v3.csv",  "SPY Scan Log v3"),
        ("spy_trade_log_v3.csv", "SPY Trade Log v3"),
    ]
    send_telegram("📊 <b>Daily CSV Report</b>\nSending log files...")
    sent = 0
    for fname, caption in files:
        path = f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path):
            continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path, "rb") as f:
                resp = requests.post(url, data={
                    "chat_id": config.CHAT_ID,
                    "caption": caption
                }, files={"document": f}, timeout=30)
            if resp.json().get("ok"):
                sent += 1
        except Exception as e:
            log.error(f"File send error: {e}")
    send_telegram(f"✅ Sent {sent}/{len(files)} files\nUpload to Claude for analysis!")


# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias           = "neutral"
        self.last_update_id = 0
        self._running       = False
        self.reminder_sent  = False   # PATCH #12

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
                            send_telegram(
                                f"✅ <b>US Bias: {self.bias.upper()}</b>\n"
                                f"  Bot will prefer {'CALL' if self.bias=='bullish' else 'PUT' if self.bias=='bearish' else 'both'} trades today."
                            )
                    elif text == "/usstatus":
                        send_telegram(
                            f"🤖 <b>SPY Bot v3 Status</b>\n"
                            f"  Running  : ✅\n"
                            f"  US Bias  : {self.bias.upper()}\n"
                            f"  EST Time : {now_est().strftime('%H:%M:%S')}\n"
                            f"  IST Time : {now_ist().strftime('%H:%M:%S')}"
                        )
                    elif text == "/usreport":
                        send_csv_files()
                    elif text == "/ushelp":
                        send_telegram(
                            "📋 <b>SPY Bot v3 Commands</b>\n"
                            "  /usbias bullish\n"
                            "  /usbias bearish\n"
                            "  /usbias neutral\n"
                            "  /usstatus\n"
                            "  /usreport  → get CSV files now"
                        )
            except Exception as e:
                log.error(f"TG poll error: {e}")
                time.sleep(5)


# ─────────────────────────────────────────────
#  MARKET DATA — Alpaca
# ─────────────────────────────────────────────
_candle_cache = {}   # PATCH #4: track last fetch time

def get_spy_ltp(api):
    try:
        trade = api.get_latest_trade(SPY_SYMBOL)
        return float(trade.price)
    except Exception as e:
        log.error(f"LTP error: {e}")
        return None

def get_candles(api, interval="5Min", limit=50):
    """PATCH #4: Force fresh data every call — no caching."""
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
        bars = bars.rename(columns={
            "timestamp": "timestamp",
            "open"     : "open",
            "high"     : "high",
            "low"      : "low",
            "close"    : "close",
            "volume"   : "volume"
        })
        for col in ["open","high","low","close","volume"]:
            bars[col] = bars[col].astype(float)
        # PATCH #4: Verify data is fresh
        if len(bars) > 1:
            last_ts = pd.to_datetime(bars["timestamp"].iloc[-1])
            if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            age_mins = (datetime.datetime.now(pytz.UTC) - last_ts).seconds / 60
            if age_mins > 15:
                log.warning(f"Stale candle data: {age_mins:.1f} min old")
        log.info(f"Fresh {len(bars)} bars [{interval}]")
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

# PATCH #11: Alpaca auto-bias from pre-market
def get_alpaca_auto_bias(api, prev_close):
    try:
        ltp = get_spy_ltp(api)
        if not ltp or not prev_close:
            return "neutral", 0
        change_pct = ((ltp - prev_close) / prev_close) * 100
        if change_pct > 0.3:
            bias = "bullish"
        elif change_pct < -0.3:
            bias = "bearish"
        else:
            bias = "neutral"
        log.info(f"Alpaca auto-bias: {bias} (SPY {change_pct:+.2f}% vs prev close)")
        return bias, round(change_pct, 2)
    except Exception as e:
        log.error(f"Auto-bias error: {e}")
        return "neutral", 0


# ─────────────────────────────────────────────
#  NEWS & SENTIMENT
# ─────────────────────────────────────────────
_last_fg_score   = 50
_last_fg_rating  = "neutral"
_last_fg_sent    = "neutral"
_last_fg_time    = None

def fetch_fear_greed():
    """PATCH #6: Refresh every 30min with fallback to last value."""
    global _last_fg_score, _last_fg_rating, _last_fg_sent, _last_fg_time
    try:
        now = datetime.datetime.now()
        if _last_fg_time and (now - _last_fg_time).seconds < 1800:
            return _last_fg_score, _last_fg_rating, _last_fg_sent
        resp  = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data   = resp.json()
        score  = float(data["fear_and_greed"]["score"])
        rating = data["fear_and_greed"]["rating"].lower()
        sent   = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"
        _last_fg_score  = score
        _last_fg_rating = rating
        _last_fg_sent   = sent
        _last_fg_time   = now
        log.info(f"F&G refreshed: {score} ({rating}) → {sent}")
        return score, rating, sent
    except Exception as e:
        log.error(f"F&G error: {e} — using last known: {_last_fg_score}")
        return _last_fg_score, _last_fg_rating, _last_fg_sent

def fetch_us_news():
    try:
        resp  = requests.get(
            "https://finance.yahoo.com/topic/stock-market-news/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup  = BeautifulSoup(resp.text, "html.parser")
        heads = []
        for tag in soup.find_all(["h3","h4"], limit=30):
            text = tag.get_text(strip=True)
            if len(text) > 20:
                tl = text.lower()
                if any(w in tl for w in ["s&p","spy","market","stocks","fed","rally","sell","nasdaq"]):
                    heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:8]
        bull  = ["rally","surge","gain","rise","bullish","positive","strong","up","buy","support"]
        bear  = ["fall","drop","decline","bearish","negative","weak","down","sell","crash","pressure"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        sent  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        return heads, sent, score
    except Exception as e:
        log.error(f"News error: {e}")
        return [], "neutral", 0

def compute_bias(fg_sent, news_sent, user_bias, alpaca_bias):
    """PATCH #11: Include Alpaca auto-bias in computation."""
    m = {"bullish":1,"neutral":0,"bearish":-1}
    s = (m.get(fg_sent,0)     * 0.25 +
         m.get(news_sent,0)   * 0.15 +
         m.get(user_bias,0)   * 0.35 +   # user bias weighted highest
         m.get(alpaca_bias,0) * 0.25)
    return "bullish" if s >= 0.3 else "bearish" if s <= -0.3 else "neutral"


# ─────────────────────────────────────────────
#  INDICATORS — PATCH #7 #8 #9 #10 #14
# ─────────────────────────────────────────────
def calc_vwap_bands(df):
    """PATCH #9: VWAP with ±1SD and ±2SD bands."""
    df = df.copy()
    df["typical"]  = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]   = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"]  = df["volume"].cumsum()
    df["vwap"]     = df["cum_tv"] / df["cum_vol"]
    df["cum_tv2"]  = (((df["typical"] - df["vwap"]) ** 2) * df["volume"]).cumsum()
    df["variance"] = df["cum_tv2"] / df["cum_vol"]
    df["sd"]       = np.sqrt(df["variance"])
    df["vwap_u1"]  = df["vwap"] + df["sd"]
    df["vwap_l1"]  = df["vwap"] - df["sd"]
    df["vwap_u2"]  = df["vwap"] + 2 * df["sd"]
    df["vwap_l2"]  = df["vwap"] - 2 * df["sd"]
    return df

def calc_ema(df, periods=[9, 21, 50]):
    """PATCH #14: EMA 9/21/50."""
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df

def calc_rvol(df):
    """PATCH #7: Relative Volume."""
    if df is None or len(df) < 5:
        return 1.0
    avg_vol = float(df["volume"].mean())
    cur_vol = float(df["volume"].iloc[-1])
    if avg_vol == 0:
        return 1.0
    return round(cur_vol / avg_vol, 2)

def calc_obv(df):
    """PATCH #8: On Balance Volume direction."""
    if df is None or len(df) < 3:
        return "neutral"
    df   = df.copy()
    obv  = [0]
    for i in range(1, len(df)):
        if float(df["close"].iloc[i]) > float(df["close"].iloc[i-1]):
            obv.append(obv[-1] + float(df["volume"].iloc[i]))
        elif float(df["close"].iloc[i]) < float(df["close"].iloc[i-1]):
            obv.append(obv[-1] - float(df["volume"].iloc[i]))
        else:
            obv.append(obv[-1])
    # OBV trend: compare last 3 values
    if obv[-1] > obv[-2] > obv[-3]:
        return "bullish"
    elif obv[-1] < obv[-2] < obv[-3]:
        return "bearish"
    return "neutral"

def calc_cumulative_delta(df):
    """Buying volume - Selling volume estimate."""
    if df is None or len(df) < 1:
        return 0
    df    = df.copy()
    delta = 0
    for _, row in df.iterrows():
        body = float(row["close"]) - float(row["open"])
        vol  = float(row["volume"])
        if body > 0:
            delta += vol
        elif body < 0:
            delta -= vol
    return round(delta, 0)

def detect_trend_relaxed(df, min_agree=3):
    """
    PATCH #2: Relaxed trend — 3/4 candles instead of 4/4.
    Returns trend, reason, strength (0-4)
    """
    if df is None or len(df) < 4:
        return "neutral", "Not enough candles", 0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]

    hh_count = sum(1 for i in range(1,len(highs)) if highs[i] > highs[i-1])
    hl_count = sum(1 for i in range(1,len(lows))  if lows[i]  > lows[i-1])
    ll_count = sum(1 for i in range(1,len(lows))  if lows[i]  < lows[i-1])
    lh_count = sum(1 for i in range(1,len(highs)) if highs[i] < highs[i-1])

    bull_score = min(hh_count, hl_count)
    bear_score = min(ll_count, lh_count)

    if bull_score >= min_agree:
        return "bullish", f"HH:{hh_count}/3 HL:{hl_count}/3 | H:{[round(h,2) for h in highs]}", bull_score
    elif bear_score >= min_agree:
        return "bearish", f"LL:{ll_count}/3 LH:{lh_count}/3 | L:{[round(l,2) for l in lows]}", bear_score
    return "neutral", f"No clear structure HH:{hh_count} HL:{hl_count} LL:{ll_count} LH:{lh_count}", 0

def detect_trend_multi(df_5, df_15, df_30):
    """PATCH #10: Multi-timeframe trend — 5m+15m+30m."""
    t5,  r5,  s5  = detect_trend_relaxed(df_5)
    t15, r15, s15 = detect_trend_relaxed(df_15)
    t30, r30, s30 = detect_trend_relaxed(df_30)

    trends = [t5, t15, t30]
    bull   = trends.count("bullish")
    bear   = trends.count("bearish")

    if bull >= 2:
        strength = "strong" if bull == 3 else "moderate"
        return "bullish", f"5m:{t5} 15m:{t15} 30m:{t30}", strength
    elif bear >= 2:
        strength = "strong" if bear == 3 else "moderate"
        return "bearish", f"5m:{t5} 15m:{t15} 30m:{t30}", strength
    return "neutral", f"5m:{t5} 15m:{t15} 30m:{t30}", "weak"

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
    """PATCH #3: Fixed body filter from 15 to MIN_FVG_BODY (0.5) for SPY."""
    if df is None or len(df) < 3:
        return None, "Not enough candles"
    candles = df.tail(15)
    for i in range(len(candles)-1, 1, -1):
        c1   = candles.iloc[i-2]
        c2   = candles.iloc[i-1]
        c3   = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < MIN_FVG_BODY:   # PATCH #3: was < 15, now < 0.5
            continue
        c1h = float(c1["high"]); c1l = float(c1["low"])
        c3h = float(c3["high"]); c3l = float(c3["low"])
        if c1h < c3l:
            size   = round(c3l - c1h, 3)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {
                "type"  : "bullish",
                "top"   : round(c3l, 3),
                "bottom": round(c1h, 3),
                "mid"   : round((c3l+c1h)/2, 3),
                "size"  : size,
                "strong": strong
            }, f"{'STRONG' if strong else 'WEAK'} Bullish FVG | Gap:${size:.3f} Body:${body:.3f}"
        if c1l > c3h:
            size   = round(c1l - c3h, 3)
            strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {
                "type"  : "bearish",
                "top"   : round(c1l, 3),
                "bottom": round(c3h, 3),
                "mid"   : round((c1l+c3h)/2, 3),
                "size"  : size,
                "strong": strong
            }, f"{'STRONG' if strong else 'WEAK'} Bearish FVG | Gap:${size:.3f} Body:${body:.3f}"
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
                "type"    : direction,
                "gap_type": "gap_open",
                "size"    : round(gap, 3),
                "level"   : round(prev_close, 3),
                "strong"  : True
            }, f"Gap open {direction} | ${gap:.3f}"
    candles = df.tail(10)
    for i in range(len(candles)-1, 0, -1):
        curr = candles.iloc[i]; prev = candles.iloc[i-1]
        body = abs(float(curr["close"]) - float(curr["open"]))
        if body < BREAKAWAY_GAP_INTRA:
            continue
        if float(curr["low"]) > float(prev["high"]):
            return {
                "type"    : "bullish",
                "gap_type": "intraday",
                "size"    : round(body, 3),
                "level"   : float(prev["high"]),
                "strong"  : True
            }, f"Intraday breakaway bullish | ${body:.3f}"
        if float(curr["high"]) < float(prev["low"]):
            return {
                "type"    : "bearish",
                "gap_type": "intraday",
                "size"    : round(body, 3),
                "level"   : float(prev["low"]),
                "strong"  : True
            }, f"Intraday breakaway bearish | ${body:.3f}"
    return None, "No breakaway gap"

def detect_orb(df, orb_high, orb_low, current_price=None):
    """PATCH #1: Check breakout AT formation + current price."""
    if orb_high is None or orb_low is None:
        return None, "ORB not formed yet"

    # PATCH #1: Check current price immediately on formation
    check_price = current_price
    if check_price is None and df is not None and len(df) > 0:
        check_price = float(df.iloc[-1]["close"])

    if check_price:
        body = 0
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            body = abs(float(last["close"]) - float(last["open"]))

        if check_price > orb_high and (check_price - orb_high) >= 0.3:
            return {
                "type" : "bullish",
                "level": orb_high,
                "size" : round(check_price - orb_high, 3)
            }, f"ORB bullish | ${check_price:.2f} > ${orb_high:.2f} (+${check_price-orb_high:.2f})"

        if check_price < orb_low and (orb_low - check_price) >= 0.3:
            return {
                "type" : "bearish",
                "level": orb_low,
                "size" : round(orb_low - check_price, 3)
            }, f"ORB bearish | ${check_price:.2f} < ${orb_low:.2f} (-${orb_low-check_price:.2f})"

    return None, f"No ORB breakout | Range:${orb_low:.2f}-${orb_high:.2f} | Price:${check_price:.2f if check_price else 0}"

def detect_vwap_rejection(df_5, trend, rvol):
    """PATCH #7 #9: VWAP rejection with RVOL + VWAP bands."""
    if df_5 is None or len(df_5) < 5:
        return None, "Not enough 5min candles"
    if trend == "neutral":
        return None, "Trend neutral — no VWAP trade"
    if rvol < MIN_RVOL:
        return None, f"RVOL {rvol} < {MIN_RVOL} — volume too low"

    df_5  = calc_vwap_bands(df_5)
    last  = df_5.iloc[-1]
    prev  = df_5.iloc[-2]
    vwap  = float(last["vwap"])
    vwap_u1 = float(last["vwap_u1"])
    vwap_l1 = float(last["vwap_l1"])
    close = float(last["close"])
    low   = float(last["low"])
    high  = float(last["high"])
    vol   = float(last["volume"])
    avg_vol = float(df_5["volume"].mean())
    vol_surge = vol > avg_vol * MIN_RVOL

    if trend == "bullish":
        # Price dips to VWAP or lower band and bounces
        near_vwap = vwap_l1 <= low <= vwap or float(prev["low"]) <= vwap
        bounced   = close > vwap
        if near_vwap and bounced and vol_surge:
            return {
                "type"      : "bullish",
                "vwap"      : round(vwap, 3),
                "vwap_l1"   : round(vwap_l1, 3),
                "vwap_u1"   : round(vwap_u1, 3),
                "volume"    : round(vol, 0),
                "avg_volume": round(avg_vol, 0),
                "rvol"      : rvol
            }, f"VWAP bullish bounce | VWAP:${vwap:.2f} L1:${vwap_l1:.2f} RVOL:{rvol}"

    if trend == "bearish":
        # Price rises to VWAP or upper band and rejects
        near_vwap = vwap <= high <= vwap_u1 or float(prev["high"]) >= vwap
        rejected  = close < vwap
        if near_vwap and rejected and vol_surge:
            return {
                "type"      : "bearish",
                "vwap"      : round(vwap, 3),
                "vwap_l1"   : round(vwap_l1, 3),
                "vwap_u1"   : round(vwap_u1, 3),
                "volume"    : round(vol, 0),
                "avg_volume": round(avg_vol, 0),
                "rvol"      : rvol
            }, f"VWAP bearish rejection | VWAP:${vwap:.2f} U1:${vwap_u1:.2f} RVOL:{rvol}"

    return None, f"No VWAP | ${vwap:.2f} | RVOL:{rvol} | VolSurge:{vol_surge}"

def is_retesting(price, bottom, top):
    return bottom <= price <= top

def get_option_details(spy_price, option_type):
    atm    = round(spy_price)
    strike = atm + 2 if option_type == "CALL" else atm - 2
    today  = datetime.date.today()
    days_to_fri = (4 - today.weekday()) % 7
    if days_to_fri == 0: days_to_fri = 7
    expiry  = today + datetime.timedelta(days=days_to_fri)
    exp_str = expiry.strftime("%Y-%m-%d")
    return strike, expiry, exp_str

def is_expiry_day():
    return datetime.date.today().weekday() == 4  # Friday


# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium,
                 signal, fg_score, user_bias, pre_bias,
                 rvol, obv, trend_strength, is_strong=False):
        self.trade_no      = trade_no
        self.strategy      = strategy
        self.direction     = direction
        self.entry_price   = entry_price
        self.option_type   = option_type
        self.strike        = strike
        self.expiry        = expiry
        self.premium       = premium
        self.signal        = signal
        self.fg_score      = fg_score
        self.user_bias     = user_bias
        self.pre_bias      = pre_bias
        self.rvol          = rvol
        self.obv           = obv
        self.trend_strength= trend_strength
        self.is_strong     = is_strong
        self.entry_time    = now_est().strftime("%H:%M:%S EST")
        self.start_time    = time.time()
        self.be_moved      = False
        self.trailing      = is_strong
        self.best_price    = entry_price
        self.sl_price      = (entry_price - SL_POINTS if direction == "bullish"
                              else entry_price + SL_POINTS)
        self.tgt_price     = (entry_price + TARGET_POINTS if direction == "bullish"
                              else entry_price - TARGET_POINTS)
        mode = "TRAILING" if is_strong else "FIXED"
        log.info(f"Trade #{trade_no} | {strategy} | {direction} | {mode} | ${entry_price:.2f} | RVOL:{rvol} OBV:{obv}")

    def check(self, ltp):
        if self.trailing:
            if self.direction == "bullish" and ltp > self.best_price:
                self.best_price = ltp
                profit = ltp - self.entry_price
                if profit >= TRAIL_START:
                    new_sl = round(ltp - TRAIL_DISTANCE, 3)
                    if new_sl > self.sl_price:
                        self.sl_price = new_sl
                        tg("📈", f"Trade #{self.trade_no} Trail SL",
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
                        tg("📉", f"Trade #{self.trade_no} Trail SL",
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
                    tg("🔒", f"Trade #{self.trade_no} Breakeven",
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
        pts = (exit_price - self.entry_price if self.direction == "bullish"
               else self.entry_price - exit_price)
        return round(pts * 0.4 * 100, 2)


# ─────────────────────────────────────────────
#  CSV LOGS — PATCH #13: 35+ columns
# ─────────────────────────────────────────────
SCAN_COLS = [
    # Time
    "datetime_est","datetime_ist",
    # Price
    "spy_ltp","spy_change_from_open","spy_change_pct",
    # Trend (multi-timeframe)
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    # Volume quant
    "rvol","obv_direction","cumulative_delta","volume_current","volume_avg",
    # VWAP
    "vwap","vwap_upper1","vwap_lower1","vwap_upper2","vwap_lower2","price_vs_vwap",
    # EMA
    "ema9","ema21","ema50","price_vs_ema9","price_vs_ema21","price_vs_ema50",
    # Patterns
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "bos_confirmed","breakaway_found","breakaway_type",
    # ORB
    "orb_high","orb_low","orb_signal","orb_breakout_size",
    # VWAP signal
    "vwap_signal","vwap_rvol",
    # Bias
    "fear_greed_score","fear_greed_rating",
    "news_sentiment","user_bias","alpaca_auto_bias","overall_bias",
    # Session & Entry
    "session","entry_condition_met","strategy_triggered",
    # Risk
    "trades_today","daily_pnl","consec_losses",
    "reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","session",
    "entry_time_est","exit_time_est",
    "pre_bias","user_bias","alpaca_bias","fear_greed",
    "trend_combined","trend_strength",
    "rvol_at_entry","obv_at_entry","cum_delta",
    "direction","is_strong","exit_mode",
    "entry_spy","exit_spy","points_moved",
    "option_type","strike","expiry",
    "premium_est","contracts","capital_usd",
    "sl_points","target_points",
    "pnl_usd","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl","notes"
]

def init_logs():
    for fname, cols in [("spy_scan_log_v3.csv", SCAN_COLS),
                         ("spy_trade_log_v3.csv", TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
    log.info("SPY v3 logs initialised")

def write_scan(rec):
    with open("spy_scan_log_v3.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("spy_trade_log_v3.csv", "a", newline="") as f:
        row = {c: rec.get(c, "") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, fg_score):
    wr = (stats["wins"]/stats["trades"]*100) if stats["trades"] > 0 else 0
    tg("📊", "SPY v3 DAILY SUMMARY",
       [f"Pre-bias      : {pre_bias.upper()}",
        f"Fear & Greed  : {fg_score}",
        f"Trades        : {stats['trades']}",
        f"Wins ✅       : {stats['wins']}",
        f"Losses ❌     : {stats['losses']}",
        f"Timeouts ⏰   : {stats['timeouts']}",
        f"Skipped ⏭     : {stats['skipped']}",
        f"Win rate      : {wr:.1f}%",
        f"Total P&L     : ${stats['pnl']:+.2f}",
        f"FVG trades    : {stats.get('fvg_trades',0)}",
        f"ORB trades    : {stats.get('orb_trades',0)}",
        f"VWAP trades   : {stats.get('vwap_trades',0)}",
        f"Strong(trail) : {stats.get('strong_trades',0)}"])
    send_csv_files()  # PATCH #15: Auto-send CSVs at end of day


# ─────────────────────────────────────────────
#  OPEN TRADE HELPER
# ─────────────────────────────────────────────
def open_trade(trade_no, strategy, direction, entry_price,
               fg_score, tg_listener, pre_bias, alpaca_bias,
               is_strong, signal, session, rvol, obv,
               trend_strength, trend_combined):
    opt_type           = "CALL" if direction == "bullish" else "PUT"
    strike, expiry, exp_str = get_option_details(entry_price, opt_type)
    premium            = CAPITAL_PER_TRADE / 100
    trade              = PaperTrade(
        trade_no=trade_no, strategy=strategy,
        direction=direction, entry_price=entry_price,
        option_type=opt_type, strike=strike,
        expiry=expiry, premium=premium,
        signal=signal, fg_score=fg_score,
        user_bias=tg_listener.bias,
        pre_bias=pre_bias, rvol=rvol,
        obv=obv, trend_strength=trend_strength,
        is_strong=is_strong
    )
    mode = "Trailing SL" if is_strong else f"Fixed ${TARGET_POINTS}"
    tg("🚀", f"SPY PAPER TRADE #{trade_no} — {strategy}",
       [f"Session        : {session.upper()}",
        f"Direction      : {direction.upper()}",
        f"Trend          : {trend_combined} ({trend_strength})",
        f"Option         : {opt_type} ${strike} | {exp_str}",
        f"SPY entry      : ${entry_price:.2f}",
        f"SL             : ${trade.sl_price:.2f} (-${SL_POINTS})",
        f"Exit mode      : {mode}",
        f"RVOL           : {rvol}x avg volume",
        f"OBV            : {obv}",
        f"Capital        : ${CAPITAL_PER_TRADE}",
        f"Signal         : {signal}",
        f"NOTE           : PAPER TRADE ⚠️"])
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

    trade_no        = 0
    active_trade    = None
    last_scan_time  = None
    pre_bias        = "neutral"
    alpaca_bias     = "neutral"
    alpaca_chg_pct  = 0
    fg_score        = 50
    fg_rating       = "neutral"
    fg_sentiment    = "neutral"
    news_sent       = "neutral"
    premarket_done  = False
    reminder_sent   = False   # PATCH #12
    orb_high        = None
    orb_low         = None
    orb_formed      = False
    orb_checked     = False   # PATCH #1
    prev_ohlc       = None
    used_signals    = set()
    session_closed_summary_sent = False
    open_price      = None    # track today's open

    send_telegram(
        f"🤖 <b>SPY Scalping Bot v3 Started</b>\n"
        f"  Patches applied : 16\n"
        f"  Mode            : PAPER TRADING\n"
        f"  SL / TGT        : ${SL_POINTS} / ${TARGET_POINTS}\n"
        f"  Strong FVG      : Trailing ${TRAIL_DISTANCE}\n"
        f"  RVOL filter     : {MIN_RVOL}x minimum\n"
        f"  Trend           : 3/4 candles (relaxed)\n"
        f"  FVG body min    : ${MIN_FVG_BODY}\n"
        f"  Max trades      : {MAX_TRADES}/day\n"
        f"  Loss / Profit   : ${DAILY_LOSS_LIMIT} / ${DAILY_PROFIT_TARGET}\n\n"
        f"📱 Commands:\n"
        f"  /usbias bullish|bearish|neutral\n"
        f"  /usstatus\n"
        f"  /usreport"
    )

    while True:
        t_est = est_time()
        t_ist = ist_time()
        now   = now_est()

        # Determine session
        if PREMARKET_START <= t_est < MARKET_START:
            session = "premarket"
        elif MARKET_START <= t_est < MARKET_END:
            session = "regular"
        else:
            session = "closed"

        # PATCH #12: Bias reminder at 1:00 PM IST
        if not reminder_sent and t_ist >= IST_REMINDER and t_ist < IST_PREMARKET:
            auto_bias, chg = get_alpaca_auto_bias(api, prev_ohlc["close"] if prev_ohlc else None)
            send_telegram(
                f"🔔 <b>US Pre-market opens in 30 minutes!</b>\n\n"
                f"  Alpaca auto-bias : {auto_bias.upper()}\n"
                f"  SPY pre-market   : {chg:+.2f}% vs prev close\n"
                f"  Fear & Greed     : {fg_score} ({fg_rating})\n\n"
                f"Override with:\n"
                f"  /usbias bullish\n"
                f"  /usbias bearish\n"
                f"  /usbias neutral\n\n"
                f"Auto-bias used if no command sent."
            )
            reminder_sent = True

        # PATCH #5: Market closed — no Telegram scan alerts
        if session == "closed":
            if not session_closed_summary_sent and t_est >= MARKET_END:
                send_summary(stats, pre_bias, fg_score)
                send_telegram("💤 <b>NYSE Closed. Bot sleeping.</b>")
                session_closed_summary_sent = True
                # Reset for next day
                stats = {
                    "trades":0,"wins":0,"losses":0,"timeouts":0,
                    "skipped":0,"pnl":0.0,"consec_loss":0,
                    "fvg_trades":0,"orb_trades":0,
                    "vwap_trades":0,"strong_trades":0
                }
                trade_no=0; active_trade=None; last_scan_time=None
                pre_bias="neutral"; alpaca_bias="neutral"
                orb_high=None; orb_low=None
                orb_formed=False; orb_checked=False
                used_signals=set(); tg_listener.bias="neutral"
                reminder_sent=False; premarket_done=False
                open_price=None; session_closed_summary_sent=False
            time.sleep(60)
            continue

        session_closed_summary_sent = False

        # ── PRE-MARKET ANALYSIS ──────────────────
        if not premarket_done and session == "premarket":
            prev_ohlc = get_prev_day_ohlc(api)
            # PATCH #11: Alpaca auto-bias
            alpaca_bias, alpaca_chg_pct = get_alpaca_auto_bias(
                api, prev_ohlc["close"] if prev_ohlc else None)
            fg_score, fg_rating, fg_sentiment = fetch_fear_greed()
            heads, news_sent, score = fetch_us_news()
            user_bias  = tg_listener.bias
            pre_bias   = compute_bias(fg_sentiment, news_sent, user_bias, alpaca_bias)
            icon = "📈" if pre_bias=="bullish" else "📉" if pre_bias=="bearish" else "➡️"
            tg(icon, f"SPY PRE-MARKET BIAS v3: {pre_bias.upper()}",
               [f"Alpaca auto-bias : {alpaca_bias.upper()} ({alpaca_chg_pct:+.2f}%)",
                f"Fear & Greed     : {fg_score} ({fg_rating})",
                f"News sentiment   : {news_sent.upper()} (score={score})",
                f"User /usbias     : {user_bias.upper()}",
                f"Prev close       : ${prev_ohlc['close'] if prev_ohlc else 'N/A'}",
                f"Prev high        : ${prev_ohlc['high'] if prev_ohlc else 'N/A'}",
                f"Prev low         : ${prev_ohlc['low'] if prev_ohlc else 'N/A'}",
                f"",
                f"Headlines:",
                *[f"• {h[:80]}" for h in heads[:3]],
                f"",
                f"Overall bias     : {pre_bias.upper()}",
                f"IST time         : {now_ist().strftime('%H:%M')}"])
            premarket_done = True

        # ── GUARDS ───────────────────────────────
        if stats["trades"] >= MAX_TRADES:
            time.sleep(30*60); continue
        if stats["consec_loss"] >= 3:
            tg("🛑","Risk Protection",
               [f"Consec losses: {stats['consec_loss']}",
                "Bot paused for today"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue
        if stats["pnl"] <= -DAILY_LOSS_LIMIT:
            tg("🛑","Daily Loss Limit",
               [f"P&L: ${stats['pnl']:+.2f}"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue
        if stats["pnl"] >= DAILY_PROFIT_TARGET:
            tg("🎯","Daily Profit Target!",
               [f"P&L: ${stats['pnl']:+.2f}"])
            send_summary(stats, pre_bias, fg_score)
            time.sleep(16*3600); continue

        # ── MONITOR ACTIVE TRADE ─────────────────
        if active_trade is not None:
            ltp    = get_spy_ltp(api)
            result = None
            if ltp: result = active_trade.check(ltp)
            if t_est >= MARKET_END:
                result = "timeout"; ltp = ltp or active_trade.entry_price
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
                    f"Exit mode : {exit_mode}",
                    f"Direction : {active_trade.direction.upper()}",
                    f"Entry     : ${active_trade.entry_price:.2f}",
                    f"Exit      : ${ltp:.2f}",
                    f"Points    : ${pts_moved:+.3f}",
                    f"Duration  : {duration}min",
                    f"RVOL      : {active_trade.rvol}x",
                    f"OBV       : {active_trade.obv}",
                    f"P&L       : ${pnl:+.2f}",
                    f"Day P&L   : ${stats['pnl']:+.2f}",
                    f"Trades    : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date"           : datetime.date.today(),
                    "trade_no"       : active_trade.trade_no,
                    "strategy"       : active_trade.strategy,
                    "session"        : session,
                    "entry_time_est" : active_trade.entry_time,
                    "exit_time_est"  : exit_time,
                    "pre_bias"       : pre_bias,
                    "user_bias"      : active_trade.user_bias,
                    "alpaca_bias"    : alpaca_bias,
                    "fear_greed"     : active_trade.fg_score,
                    "trend_combined" : active_trade.trend_strength,
                    "trend_strength" : active_trade.trend_strength,
                    "rvol_at_entry"  : active_trade.rvol,
                    "obv_at_entry"   : active_trade.obv,
                    "direction"      : active_trade.direction,
                    "is_strong"      : active_trade.is_strong,
                    "exit_mode"      : exit_mode,
                    "entry_spy"      : active_trade.entry_price,
                    "exit_spy"       : round(ltp, 3),
                    "points_moved"   : pts_moved,
                    "option_type"    : active_trade.option_type,
                    "strike"         : active_trade.strike,
                    "expiry"         : active_trade.expiry,
                    "premium_est"    : active_trade.premium,
                    "contracts"      : 1,
                    "capital_usd"    : CAPITAL_PER_TRADE,
                    "sl_points"      : SL_POINTS,
                    "target_points"  : TARGET_POINTS,
                    "pnl_usd"        : pnl,
                    "result"         : result,
                    "be_triggered"   : active_trade.be_moved,
                    "trail_triggered": active_trade.trailing,
                    "duration_min"   : duration,
                    "consec_losses"  : stats["consec_loss"],
                    "daily_pnl"      : stats["pnl"],
                    "notes"          : active_trade.signal
                })
                active_trade = None
                time.sleep(2*60)
            else:
                time.sleep(15)
            continue

        # ── FETCH FRESH DATA ─────────────────────
        # PATCH #4: Force fresh candle fetch every scan
        ltp   = get_spy_ltp(api)
        df_5  = get_candles(api, "5Min",  50)
        df_15 = get_candles(api, "15Min", 30)
        df_30 = get_candles(api, "30Min", 20)

        if ltp is None or df_5 is None:
            time.sleep(15); continue

        # Track open price
        if open_price is None and session == "regular":
            open_price = ltp

        # ── ORB FORMATION ────────────────────────
        if not orb_formed and t_est >= ORB_END_TIME and session == "regular":
            try:
                orb_df = df_5[pd.to_datetime(df_5["timestamp"]).dt.time <= ORB_END_TIME]
                if not orb_df.empty:
                    orb_high   = float(orb_df["high"].max())
                    orb_low    = float(orb_df["low"].min())
                    orb_formed = True
                    tg("📐","SPY ORB Range Formed v3",
                       [f"High     : ${orb_high:.2f}",
                        f"Low      : ${orb_low:.2f}",
                        f"Size     : ${orb_high-orb_low:.2f}",
                        f"SPY now  : ${ltp:.2f}",
                        f"Vs ORB   : ${ltp-orb_high:+.2f} from high"])
            except Exception as e:
                log.error(f"ORB formation error: {e}")

        # PATCH #6: F&G refresh every 30 min
        fg_score, fg_rating, fg_sentiment = fetch_fear_greed()

        # ── CALCULATE INDICATORS ─────────────────
        # PATCH #10: Multi-timeframe trend
        trend, trend_reason, trend_strength = detect_trend_multi(df_5, df_15, df_30)
        trend_5m,  _, _ = detect_trend_relaxed(df_5)
        trend_15m, _, _ = detect_trend_relaxed(df_15)
        trend_30m, _, _ = detect_trend_relaxed(df_30)

        # PATCH #7: RVOL
        rvol = calc_rvol(df_5)

        # PATCH #8: OBV
        obv  = calc_obv(df_5)

        # Cumulative delta
        cum_delta = calc_cumulative_delta(df_5)

        # PATCH #9: VWAP bands
        df_5_vwap = calc_vwap_bands(df_5)
        last_row  = df_5_vwap.iloc[-1]
        vwap      = round(float(last_row["vwap"]),    3)
        vwap_u1   = round(float(last_row["vwap_u1"]), 3)
        vwap_l1   = round(float(last_row["vwap_l1"]), 3)
        vwap_u2   = round(float(last_row["vwap_u2"]), 3)
        vwap_l2   = round(float(last_row["vwap_l2"]), 3)

        # PATCH #14: EMA 9/21/50
        df_5_ema  = calc_ema(df_5)
        ema9      = round(float(df_5_ema["ema9"].iloc[-1]),  3)
        ema21     = round(float(df_5_ema["ema21"].iloc[-1]), 3)
        ema50     = round(float(df_5_ema["ema50"].iloc[-1]), 3)

        # ── RUN STRATEGY DETECTORS ───────────────
        fvg,     fvg_reason  = detect_fvg(df_5)
        bos,     bos_level   = detect_bos(df_5, trend)
        bgap,    bgap_reason = detect_breakaway_gap(
            df_5, prev_ohlc["close"] if prev_ohlc else None)
        # PATCH #1: Pass current price to ORB
        orb_sig, orb_reason  = detect_orb(df_5, orb_high, orb_low, ltp)
        vwap_sig,vwap_reason = detect_vwap_rejection(df_5, trend, rvol)

        # ── 5-MIN SCAN LOG ───────────────────────
        do_scan = (last_scan_time is None or
                   (now_est()-last_scan_time).seconds >= 300)
        if do_scan:
            last_scan_time = now_est()
            strats    = []
            if fvg and bos: strats.append("FVG+BOS")
            if bgap:        strats.append("Breakaway")
            if orb_sig:     strats.append("ORB")
            if vwap_sig:    strats.append("VWAP")
            entry_met = len(strats) > 0 and trend != "neutral" and rvol >= MIN_RVOL

            spy_chg_open = round(ltp - open_price, 3) if open_price else 0
            spy_chg_pct  = round((spy_chg_open / open_price * 100), 3) if open_price else 0

            write_scan({
                "datetime_est"      : now.strftime("%Y-%m-%d %H:%M EST"),
                "datetime_ist"      : now_ist().strftime("%Y-%m-%d %H:%M IST"),
                "spy_ltp"           : round(ltp, 3),
                "spy_change_from_open": spy_chg_open,
                "spy_change_pct"    : spy_chg_pct,
                "trend_5m"          : trend_5m,
                "trend_15m"         : trend_15m,
                "trend_30m"         : trend_30m,
                "trend_combined"    : trend,
                "trend_strength"    : trend_strength,
                "rvol"              : rvol,
                "obv_direction"     : obv,
                "cumulative_delta"  : cum_delta,
                "volume_current"    : round(float(df_5["volume"].iloc[-1]), 0),
                "volume_avg"        : round(float(df_5["volume"].mean()), 0),
                "vwap"              : vwap,
                "vwap_upper1"       : vwap_u1,
                "vwap_lower1"       : vwap_l1,
                "vwap_upper2"       : vwap_u2,
                "vwap_lower2"       : vwap_l2,
                "price_vs_vwap"     : round(ltp - vwap, 3),
                "ema9"              : ema9,
                "ema21"             : ema21,
                "ema50"             : ema50,
                "price_vs_ema9"     : round(ltp - ema9, 3),
                "price_vs_ema21"    : round(ltp - ema21, 3),
                "price_vs_ema50"    : round(ltp - ema50, 3),
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
                "orb_breakout_size" : orb_sig["size"] if orb_sig else "",
                "vwap_signal"       : vwap_sig["type"] if vwap_sig else "",
                "vwap_rvol"         : vwap_sig["rvol"] if vwap_sig else "",
                "fear_greed_score"  : fg_score,
                "fear_greed_rating" : fg_rating,
                "news_sentiment"    : news_sent,
                "user_bias"         : tg_listener.bias,
                "alpaca_auto_bias"  : alpaca_bias,
                "overall_bias"      : pre_bias,
                "session"           : session,
                "entry_condition_met": entry_met,
                "strategy_triggered": ",".join(strats),
                "trades_today"      : stats["trades"],
                "daily_pnl"         : stats["pnl"],
                "consec_losses"     : stats["consec_loss"],
                "reason"            : f"FVG:{fvg_reason}|ORB:{orb_reason}|VWAP:{vwap_reason}"
            })

            # PATCH #5: Only send Telegram if market is open
            cond_icon = "✅" if entry_met else "⏸️"
            tg(cond_icon, f"SPY SCAN v3 {now.strftime('%H:%M')} EST",
               [f"SPY        : ${ltp:.2f} ({spy_chg_pct:+.2f}%)",
                f"Session    : {session.upper()}",
                f"Trend      : {trend.upper()} ({trend_strength})",
                f"  5m/15m/30m: {trend_5m}/{trend_15m}/{trend_30m}",
                f"RVOL       : {rvol}x {'✅' if rvol >= MIN_RVOL else '❌ too low'}",
                f"OBV        : {obv}",
                f"Delta      : {cum_delta:+.0f}",
                f"VWAP       : ${vwap:.2f} (price {ltp-vwap:+.2f})",
                f"  Bands    : L1:${vwap_l1:.2f} U1:${vwap_u1:.2f}",
                f"EMA9/21/50 : ${ema9:.2f}/${ema21:.2f}/${ema50:.2f}",
                f"FVG        : {fvg_reason[:45] if fvg else 'NONE'}",
                f"BOS        : {'YES' if bos else 'NO'}",
                f"ORB        : {orb_reason[:45]}",
                f"VWAP sig   : {vwap_reason[:45]}",
                f"F&G        : {fg_score} ({fg_rating})",
                f"Bias       : {pre_bias.upper()} | Alpaca:{alpaca_bias.upper()}",
                f"Signals    : {', '.join(strats) if strats else 'NONE'}",
                f"IST        : {now_ist().strftime('%H:%M')}"])

        # ── VOLUME GATE ──────────────────────────
        # PATCH #7: Skip all trades if RVOL too low
        if rvol < MIN_RVOL and session == "regular":
            time.sleep(60); continue

        # ── STRATEGY 1: FVG + BOS ─────────────────
        if fvg and bos and trend != "neutral" and "FVG" not in used_signals:
            if fvg["type"] == trend and (pre_bias == "neutral" or pre_bias == trend):
                # PATCH #8: OBV must agree
                if obv == trend or obv == "neutral":
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
                            fg_score, tg_listener, pre_bias, alpaca_bias,
                            is_strong, f"FVG ${fvg['size']:.3f} BOS@${bos_level:.2f}",
                            session, rvol, obv, trend_strength, trend
                        )
                        used_signals.add("FVG")
                        stats["fvg_trades"] += 1
                        if is_strong: stats["strong_trades"] += 1
                        time.sleep(15); continue
                    else:
                        stats["skipped"] += 1
                else:
                    tg("⚠️","FVG Skipped — OBV Conflict",
                       [f"FVG : {fvg['type'].upper()}",
                        f"OBV : {obv.upper()} ← disagrees",
                        f"Action: Skipping — volume not confirming"])
                    stats["skipped"] += 1

        # ── STRATEGY 1B: BREAKAWAY GAP ────────────
        if bgap and trend != "neutral" and "BGAP" not in used_signals:
            if bgap["type"] == trend and (pre_bias == "neutral" or pre_bias == trend):
                if obv == trend or obv == "neutral":
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
                            fg_score, tg_listener, pre_bias, alpaca_bias,
                            True, f"Bgap {bgap['gap_type']} ${bgap['size']:.3f}",
                            session, rvol, obv, trend_strength, trend
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
                            fg_score, tg_listener, pre_bias, alpaca_bias,
                            False, f"ORB {orb_sig['type']} ${orb_sig['size']:.3f}",
                            session, rvol, obv, trend_strength, trend
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
                        fg_score, tg_listener, pre_bias, alpaca_bias,
                        False, f"VWAP {vwap_sig['type']} RVOL:{rvol}",
                        session, rvol, obv, trend_strength, trend
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
        log.info("SPY Bot v3 stopped")
        send_telegram("🛑 <b>SPY Bot v3 stopped manually.</b>")
