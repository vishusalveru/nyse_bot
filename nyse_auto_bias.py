"""
=============================================================
  nyse_auto_bias.py — NYSE/SPY Auto Bias + Trend Reversal
  ─────────────────────────────────────────────────────────
  BIAS SOURCES:
  1. Alpaca pre-market price vs prev close (20%)
  2. Fear & Greed Index CNN (15%)
  3. Yahoo Finance news sentiment (10%)
  4. VIX level (10%)
  5. ES Futures direction (5%)
  6. User /usbias command (40%)

  TREND REVERSAL DETECTION:
  1. CHoCH (Change of Character)
  2. BOS (Break of Structure)
  3. RSI Divergence
  4. VWAP Reclaim / Rejection
  5. Volume Climax
  6. EMA Cross (9/21)
  7. Double Top / Double Bottom
  8. Gap Fill Reversal

  Called before EVERY trade to confirm or reject entry
=============================================================
"""

import requests
import logging
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  SECTION 1: BIAS SOURCES
# ═══════════════════════════════════════════════════════════

def get_alpaca_bias(api, prev_close):
    """Pre-market SPY price vs previous close."""
    try:
        trade   = api.get_latest_trade("SPY")
        ltp     = float(trade.price)
        if not prev_close:
            return "neutral", 0, ltp
        chg_pct = ((ltp - prev_close) / prev_close) * 100
        bias    = "bullish" if chg_pct > 0.3 else "bearish" if chg_pct < -0.3 else "neutral"
        log.info(f"Alpaca bias: {bias} | ${ltp:.2f} | {chg_pct:+.2f}%")
        return bias, round(chg_pct, 2), ltp
    except Exception as e:
        log.error(f"Alpaca bias error: {e}")
        return "neutral", 0, 0


def get_fear_greed_bias():
    """CNN Fear & Greed Index."""
    try:
        resp   = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        data   = resp.json()
        score  = float(data["fear_and_greed"]["score"])
        rating = data["fear_and_greed"]["rating"].lower()
        bias   = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"
        log.info(f"F&G: {score} ({rating}) → {bias}")
        return bias, score, rating
    except Exception as e:
        log.error(f"F&G error: {e}")
        return "neutral", 50, "neutral"


def get_news_bias():
    """Yahoo Finance news sentiment."""
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
                if any(w in tl for w in ["s&p","spy","market","stocks",
                                          "fed","rally","nasdaq","economy"]):
                    heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:10]
        bull  = ["rally","surge","gain","rise","bullish","positive","strong",
                 "up","buy","support","recovery","boost","record","optimism"]
        bear  = ["fall","drop","decline","bearish","negative","weak","down",
                 "sell","crash","pressure","recession","fear","loss","risk"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        bias  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        log.info(f"News bias: {bias} | score={score}")
        return bias, score, heads
    except Exception as e:
        log.error(f"News error: {e}")
        return "neutral", 0, []


def get_vix_bias():
    """VIX volatility index level."""
    try:
        resp = requests.get(
            "https://finance.yahoo.com/quote/%5EVIX/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        tag  = soup.find("fin-streamer", {"data-symbol": "^VIX"})
        vix  = float(tag.get("value", 20)) if tag else 20.0
        bias = "bullish" if vix < 15 else "bearish" if vix > 25 else "neutral"
        log.info(f"VIX: {vix:.2f} → {bias}")
        return bias, round(vix, 2)
    except Exception as e:
        log.error(f"VIX error: {e}")
        return "neutral", 20.0


def get_futures_bias():
    """ES S&P 500 Futures direction."""
    try:
        resp = requests.get(
            "https://finance.yahoo.com/quote/ES%3DF/",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        tags = soup.find_all("fin-streamer",
                             {"data-field": "regularMarketChangePercent"})
        for tag in tags:
            val = tag.get("value")
            if val:
                chg  = float(val)
                bias = "bullish" if chg > 0.2 else "bearish" if chg < -0.2 else "neutral"
                log.info(f"Futures ES: {chg:+.2f}% → {bias}")
                return bias, round(chg, 2)
        return "neutral", 0
    except Exception as e:
        log.error(f"Futures error: {e}")
        return "neutral", 0


# ═══════════════════════════════════════════════════════════
#  SECTION 2: TREND REVERSAL DETECTION
#  Called before EVERY trade to confirm or block entry
# ═══════════════════════════════════════════════════════════

def detect_choch(df, current_trend):
    """
    CHoCH — Change of Character
    The earliest signal of a trend reversal.

    Bullish CHoCH: In a downtrend, price breaks above last lower high
    Bearish CHoCH: In an uptrend, price breaks below last higher low

    Returns: (choch_detected, choch_type, reason)
    """
    if df is None or len(df) < 6:
        return False, None, "Not enough candles"

    recent     = df.tail(10)
    last_close = float(recent["close"].iloc[-1])
    last_high  = float(recent["high"].iloc[-1])
    last_low   = float(recent["low"].iloc[-1])

    # In uptrend — watch for bearish CHoCH
    if current_trend == "bullish":
        # Find last higher low
        lows = [float(x) for x in recent["low"].tolist()[:-1]]
        if len(lows) >= 2:
            higher_low = max(lows[-3:])   # recent swing low
            if last_close < higher_low:
                reason = (f"Bearish CHoCH: Close ${last_close:.2f} < "
                          f"Higher low ${higher_low:.2f} — uptrend broken!")
                log.warning(f"CHoCH detected: {reason}")
                return True, "bearish", reason

    # In downtrend — watch for bullish CHoCH
    elif current_trend == "bearish":
        # Find last lower high
        highs = [float(x) for x in recent["high"].tolist()[:-1]]
        if len(highs) >= 2:
            lower_high = min(highs[-3:])   # recent swing high
            if last_close > lower_high:
                reason = (f"Bullish CHoCH: Close ${last_close:.2f} > "
                          f"Lower high ${lower_high:.2f} — downtrend broken!")
                log.info(f"CHoCH detected: {reason}")
                return True, "bullish", reason

    return False, None, "No CHoCH detected"


def detect_rsi_divergence(df):
    """
    RSI Divergence — leading reversal signal.

    Bullish divergence : Price makes lower low but RSI makes higher low
                         → reversal UP likely
    Bearish divergence : Price makes higher high but RSI makes lower high
                         → reversal DOWN likely

    Returns: (divergence_found, type, strength, reason)
    """
    if df is None or len(df) < 14:
        return False, None, 0, "Not enough candles for RSI"

    # Calculate RSI
    df    = df.copy()
    delta = df["close"].astype(float).diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=13, adjust=False).mean()
    avg_l = loss.ewm(com=13, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    recent = df.tail(10)
    prices = recent["close"].astype(float).tolist()
    rsis   = recent["rsi"].tolist()

    if len(prices) < 4:
        return False, None, 0, "Not enough recent data"

    # Compare last two swing points
    price_diff = prices[-1] - prices[-4]
    rsi_diff   = rsis[-1]  - rsis[-4]

    # Bullish divergence: price lower, RSI higher
    if price_diff < -0.5 and rsi_diff > 2:
        strength = min(10, abs(rsi_diff))
        reason   = (f"Bullish RSI divergence: Price {price_diff:+.2f} "
                    f"but RSI +{rsi_diff:.1f} → reversal UP likely")
        log.info(reason)
        return True, "bullish", round(strength, 1), reason

    # Bearish divergence: price higher, RSI lower
    if price_diff > 0.5 and rsi_diff < -2:
        strength = min(10, abs(rsi_diff))
        reason   = (f"Bearish RSI divergence: Price +{price_diff:.2f} "
                    f"but RSI {rsi_diff:.1f} → reversal DOWN likely")
        log.warning(reason)
        return True, "bearish", round(strength, 1), reason

    cur_rsi = round(rsis[-1], 1)
    return False, None, 0, f"No RSI divergence | RSI:{cur_rsi}"


def detect_vwap_reversal(df):
    """
    VWAP Reclaim / Rejection — key reversal signal.

    VWAP Reclaim : Price was below VWAP, now closes above → bullish reversal
    VWAP Rejection: Price was above VWAP, now closes below → bearish reversal

    Returns: (reversal_found, type, reason)
    """
    if df is None or len(df) < 5:
        return False, None, "Not enough candles"

    df         = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]  = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"] / df["cum_vol"]

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    vwap  = float(last["vwap"])
    close = float(last["close"])
    prev_close = float(prev["close"])

    # VWAP Reclaim: was below, now above
    if prev_close < float(prev["vwap"]) and close > vwap:
        reason = f"VWAP Reclaim: Price ${close:.2f} crossed above VWAP ${vwap:.2f} → bullish reversal"
        log.info(reason)
        return True, "bullish", reason

    # VWAP Rejection: was above, now below
    if prev_close > float(prev["vwap"]) and close < vwap:
        reason = f"VWAP Rejection: Price ${close:.2f} crossed below VWAP ${vwap:.2f} → bearish reversal"
        log.warning(reason)
        return True, "bearish", reason

    return False, None, f"No VWAP reversal | VWAP:${vwap:.2f} Close:${close:.2f}"


def detect_volume_climax(df):
    """
    Volume Climax — exhaustion signal leading to reversal.

    Bullish climax : Extremely high volume on a down candle → sellers exhausted
    Bearish climax : Extremely high volume on an up candle → buyers exhausted

    Returns: (climax_found, type, rvol, reason)
    """
    if df is None or len(df) < 10:
        return False, None, 0, "Not enough candles"

    avg_vol  = float(df["volume"].mean())
    last     = df.iloc[-1]
    cur_vol  = float(last["volume"])
    rvol     = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1
    body     = float(last["close"]) - float(last["open"])

    # Climax threshold: 3x average volume
    if rvol >= 3.0:
        if body < 0:   # Down candle with huge volume → bullish exhaustion
            reason = (f"Bullish volume climax: RVOL {rvol}x on DOWN candle "
                      f"${body:.2f} → sellers exhausted → reversal UP possible")
            log.info(reason)
            return True, "bullish", rvol, reason
        elif body > 0: # Up candle with huge volume → bearish exhaustion
            reason = (f"Bearish volume climax: RVOL {rvol}x on UP candle "
                      f"+${body:.2f} → buyers exhausted → reversal DOWN possible")
            log.warning(reason)
            return True, "bearish", rvol, reason

    return False, None, rvol, f"No volume climax | RVOL:{rvol}x"


def detect_ema_cross(df):
    """
    EMA 9/21 Cross — trend reversal confirmation.

    Bullish cross : EMA9 crosses above EMA21 → uptrend starting
    Bearish cross : EMA9 crosses below EMA21 → downtrend starting

    Returns: (cross_found, type, reason)
    """
    if df is None or len(df) < 21:
        return False, None, "Not enough candles for EMA"

    df       = df.copy()
    df["e9"]  = df["close"].astype(float).ewm(span=9,  adjust=False).mean()
    df["e21"] = df["close"].astype(float).ewm(span=21, adjust=False).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    e9_now  = float(last["e9"]);  e21_now  = float(last["e21"])
    e9_prev = float(prev["e9"]);  e21_prev = float(prev["e21"])

    # Bullish cross: EMA9 crossed above EMA21
    if e9_prev <= e21_prev and e9_now > e21_now:
        reason = (f"Bullish EMA cross: EMA9 ${e9_now:.2f} crossed above "
                  f"EMA21 ${e21_now:.2f} → uptrend confirmed")
        log.info(reason)
        return True, "bullish", reason

    # Bearish cross: EMA9 crossed below EMA21
    if e9_prev >= e21_prev and e9_now < e21_now:
        reason = (f"Bearish EMA cross: EMA9 ${e9_now:.2f} crossed below "
                  f"EMA21 ${e21_now:.2f} → downtrend confirmed")
        log.warning(reason)
        return True, "bearish", reason

    gap = round(e9_now - e21_now, 3)
    return False, None, f"No EMA cross | EMA9-EMA21 gap:${gap:+.3f}"


def detect_double_top_bottom(df):
    """
    Double Top / Double Bottom — strong reversal pattern.

    Double Top    : Two similar highs with a valley between → bearish reversal
    Double Bottom : Two similar lows with a peak between → bullish reversal

    Returns: (pattern_found, type, level, reason)
    """
    if df is None or len(df) < 15:
        return False, None, 0, "Not enough candles"

    recent = df.tail(20)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]

    tolerance = 0.3   # $0.30 tolerance for SPY

    # Double Top: find two peaks within tolerance
    peak_indices = []
    for i in range(1, len(highs)-1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            peak_indices.append((i, highs[i]))

    if len(peak_indices) >= 2:
        last_two = peak_indices[-2:]
        h1, h2   = last_two[0][1], last_two[1][1]
        if abs(h1 - h2) <= tolerance and last_two[1][0] - last_two[0][0] >= 3:
            level  = round((h1 + h2) / 2, 2)
            reason = (f"Double Top at ${level:.2f} "
                      f"(peaks: ${h1:.2f} & ${h2:.2f}) → bearish reversal")
            log.warning(reason)
            return True, "bearish", level, reason

    # Double Bottom: find two troughs within tolerance
    trough_indices = []
    for i in range(1, len(lows)-1):
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            trough_indices.append((i, lows[i]))

    if len(trough_indices) >= 2:
        last_two = trough_indices[-2:]
        l1, l2   = last_two[0][1], last_two[1][1]
        if abs(l1 - l2) <= tolerance and last_two[1][0] - last_two[0][0] >= 3:
            level  = round((l1 + l2) / 2, 2)
            reason = (f"Double Bottom at ${level:.2f} "
                      f"(troughs: ${l1:.2f} & ${l2:.2f}) → bullish reversal")
            log.info(reason)
            return True, "bullish", level, reason

    return False, None, 0, "No double top/bottom pattern"


def detect_gap_fill_reversal(df, prev_close):
    """
    Gap Fill Reversal.
    When a gap is filled (price returns to prev close level),
    it often leads to a reversal.

    Returns: (reversal_likely, type, reason)
    """
    if df is None or len(df) < 2 or not prev_close:
        return False, None, "No prev close available"

    open_price = float(df["open"].iloc[0])
    last_price = float(df["close"].iloc[-1])
    gap        = open_price - prev_close

    # Bullish gap that is being filled (price falling back)
    if gap > 0.5 and last_price <= prev_close + (gap * 0.5):
        reason = (f"Gap fill reversal: Bullish gap ${gap:.2f} being filled "
                  f"→ potential support at prev close ${prev_close:.2f}")
        log.info(reason)
        return True, "bullish", reason

    # Bearish gap that is being filled (price rising back)
    if gap < -0.5 and last_price >= prev_close + (gap * 0.5):
        reason = (f"Gap fill reversal: Bearish gap ${abs(gap):.2f} being filled "
                  f"→ potential resistance at prev close ${prev_close:.2f}")
        log.warning(reason)
        return True, "bearish", reason

    return False, None, f"No gap fill reversal | Gap:${gap:+.2f}"


def detect_support_resistance_break(df, orb_high=None, orb_low=None, prev_ohlc=None):
    """
    Key level breaks — reversal signals at S&R.

    Checks: ORB levels, Previous day high/low/close
    A break + rejection of key level → reversal signal

    Returns: (break_found, type, level, reason)
    """
    if df is None or len(df) < 2:
        return False, None, 0, "Not enough candles"

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    close = float(last["close"])
    high  = float(last["high"])
    low   = float(last["low"])

    tolerance = 0.3

    levels = []
    if orb_high: levels.append(("ORB High", orb_high, "resistance"))
    if orb_low:  levels.append(("ORB Low",  orb_low,  "support"))
    if prev_ohlc:
        levels.append(("Prev High",  prev_ohlc["high"],  "resistance"))
        levels.append(("Prev Low",   prev_ohlc["low"],   "support"))
        levels.append(("Prev Close", prev_ohlc["close"], "pivot"))

    for name, level, level_type in levels:
        # Break above resistance then rejection
        if level_type == "resistance":
            if float(prev["close"]) < level and high > level and close < level:
                reason = (f"Failed breakout at {name} ${level:.2f}: "
                          f"High ${high:.2f} rejected → bearish reversal")
                log.warning(reason)
                return True, "bearish", level, reason

        # Break below support then recovery
        if level_type == "support":
            if float(prev["close"]) > level and low < level and close > level:
                reason = (f"Failed breakdown at {name} ${level:.2f}: "
                          f"Low ${low:.2f} recovered → bullish reversal")
                log.info(reason)
                return True, "bullish", level, reason

    return False, None, 0, "No S&R reversal signals"


# ═══════════════════════════════════════════════════════════
#  SECTION 3: REVERSAL RISK ASSESSMENT
#  Called before EVERY trade entry
# ═══════════════════════════════════════════════════════════

def assess_reversal_risk(df_5, df_15, current_trend,
                          prev_close=None, orb_high=None,
                          orb_low=None, prev_ohlc=None):
    """
    Run ALL reversal detectors before a trade.
    Returns overall reversal risk and whether to proceed.

    Risk levels:
    - LOW    : No reversal signals → proceed with trade
    - MEDIUM : 1-2 weak signals → proceed with caution (tighter SL)
    - HIGH   : 2+ strong signals → skip trade
    - ABORT  : Strong reversal confirmed → definitely skip

    Returns: (risk_level, proceed, signals_found, summary)
    """
    signals  = []
    warnings = 0
    aborts   = 0

    # 1. CHoCH check
    choch, choch_type, choch_reason = detect_choch(df_5, current_trend)
    if choch and choch_type != current_trend:
        signals.append(f"⚠️ CHoCH: {choch_reason}")
        aborts += 1

    # 2. RSI Divergence
    div, div_type, div_strength, div_reason = detect_rsi_divergence(df_15)
    if div and div_type != current_trend:
        signals.append(f"⚠️ RSI Divergence ({div_strength:.1f}): {div_reason}")
        if div_strength >= 5:
            aborts  += 1
        else:
            warnings += 1

    # 3. VWAP Reversal
    vwap_rev, vwap_type, vwap_reason = detect_vwap_reversal(df_5)
    if vwap_rev and vwap_type != current_trend:
        signals.append(f"⚠️ VWAP Reversal: {vwap_reason}")
        warnings += 1

    # 4. Volume Climax
    climax, climax_type, climax_rvol, climax_reason = detect_volume_climax(df_5)
    if climax and climax_type != current_trend:
        signals.append(f"⚠️ Volume Climax ({climax_rvol}x): {climax_reason}")
        if climax_rvol >= 5:
            aborts  += 1
        else:
            warnings += 1

    # 5. EMA Cross
    ema_cross, ema_type, ema_reason = detect_ema_cross(df_15)
    if ema_cross and ema_type != current_trend:
        signals.append(f"⚠️ EMA Cross: {ema_reason}")
        warnings += 1

    # 6. Double Top/Bottom
    dbl, dbl_type, dbl_level, dbl_reason = detect_double_top_bottom(df_5)
    if dbl and dbl_type != current_trend:
        signals.append(f"⚠️ {dbl_reason}")
        aborts += 1

    # 7. Gap Fill Reversal
    gap_rev, gap_type, gap_reason = detect_gap_fill_reversal(df_5, prev_close)
    if gap_rev and gap_type != current_trend:
        signals.append(f"⚠️ {gap_reason}")
        warnings += 1

    # 8. S&R Level Break
    sr_rev, sr_type, sr_level, sr_reason = detect_support_resistance_break(
        df_5, orb_high, orb_low, prev_ohlc)
    if sr_rev and sr_type != current_trend:
        signals.append(f"⚠️ {sr_reason}")
        warnings += 1

    # ── Risk Assessment ──────────────────────
    if aborts >= 2:
        risk    = "ABORT"
        proceed = False
        summary = f"ABORT trade — {aborts} strong reversal signals against {current_trend}"
    elif aborts == 1 or warnings >= 3:
        risk    = "HIGH"
        proceed = False
        summary = f"HIGH reversal risk — skip trade ({aborts} aborts, {warnings} warnings)"
    elif warnings >= 2:
        risk    = "MEDIUM"
        proceed = True
        summary = f"MEDIUM reversal risk — proceed with tighter SL ({warnings} warnings)"
    elif warnings == 1:
        risk    = "LOW-MEDIUM"
        proceed = True
        summary = f"LOW-MEDIUM risk — proceed normally (1 weak signal)"
    else:
        risk    = "LOW"
        proceed = True
        summary = f"LOW reversal risk — all clear for {current_trend} trade"

    log.info(f"Reversal risk: {risk} | Proceed: {proceed} | Signals: {len(signals)}")
    return risk, proceed, signals, summary


# ═══════════════════════════════════════════════════════════
#  SECTION 4: COMBINED BIAS
# ═══════════════════════════════════════════════════════════

def get_combined_bias(api, prev_close, user_bias="neutral"):
    """
    Combine all bias sources into single direction.
    Called once at pre-market open.
    """
    score_map = {"bullish": 1, "neutral": 0, "bearish": -1}

    alpaca_bias, alpaca_chg, spy_ltp = get_alpaca_bias(api, prev_close)
    fg_bias,     fg_score,   fg_rating = get_fear_greed_bias()
    news_bias,   news_score, headlines  = get_news_bias()
    vix_bias,    vix_level              = get_vix_bias()
    futures_bias, futures_chg           = get_futures_bias()

    score = (
        score_map.get(user_bias,    0) * 0.40 +
        score_map.get(alpaca_bias,  0) * 0.20 +
        score_map.get(fg_bias,      0) * 0.15 +
        score_map.get(news_bias,    0) * 0.10 +
        score_map.get(vix_bias,     0) * 0.10 +
        score_map.get(futures_bias, 0) * 0.05
    )

    final_bias = "bullish" if score >= 0.25 else "bearish" if score <= -0.25 else "neutral"
    conf       = "HIGH" if abs(score) > 0.5 else "MEDIUM" if abs(score) > 0.25 else "LOW"

    report = {
        "final_bias"    : final_bias,
        "confidence"    : conf,
        "score"         : round(score, 3),
        "spy_ltp"       : spy_ltp,
        "alpaca_chg_pct": alpaca_chg,
        "alpaca_bias"   : alpaca_bias,
        "fg_score"      : fg_score,
        "fg_rating"     : fg_rating,
        "fg_bias"       : fg_bias,
        "news_score"    : news_score,
        "news_bias"     : news_bias,
        "headlines"     : headlines,
        "vix_level"     : vix_level,
        "vix_bias"      : vix_bias,
        "futures_chg"   : futures_chg,
        "futures_bias"  : futures_bias,
        "user_bias"     : user_bias,
    }

    log.info(f"Combined bias: {final_bias} ({conf}) score={score:.3f}")
    return final_bias, report


# ═══════════════════════════════════════════════════════════
#  SECTION 5: PRE-TRADE CHECK
#  Call this before EVERY trade entry
# ═══════════════════════════════════════════════════════════

def pre_trade_check(df_5, df_15, direction, pre_bias,
                    prev_close=None, orb_high=None,
                    orb_low=None, prev_ohlc=None):
    """
    Complete pre-trade validation:
    1. Check if direction matches pre-market bias
    2. Check all reversal signals
    3. Return final go/no-go decision

    Returns: (proceed, risk_level, reason, reversal_signals)
    """
    # Check 1: Bias alignment
    if pre_bias != "neutral" and pre_bias != direction:
        return (False, "HIGH",
                f"Direction {direction} conflicts with pre-market bias {pre_bias}",
                [])

    # Check 2: Reversal risk
    risk, proceed, signals, summary = assess_reversal_risk(
        df_5, df_15, direction,
        prev_close, orb_high, orb_low, prev_ohlc
    )

    return proceed, risk, summary, signals


# ═══════════════════════════════════════════════════════════
#  SECTION 6: TELEGRAM FORMATTING
# ═══════════════════════════════════════════════════════════

def format_bias_message(report):
    """Format complete bias report for Telegram."""
    bias  = report["final_bias"]
    score = report["score"]
    conf  = report["confidence"]
    icon  = "📈" if bias == "bullish" else "📉" if bias == "bearish" else "➡️"

    lines = [
        f"{icon} <b>NYSE AUTO BIAS: {bias.upper()} ({conf})</b>",
        f"  Combined score   : {score:+.3f}",
        f"",
        f"  <b>Sources (weighted):</b>",
        f"  User /usbias     : {report['user_bias'].upper()} (40%)",
        f"  Alpaca pre-mkt   : {report['alpaca_bias'].upper()} "
        f"(${report['spy_ltp']:.2f} | {report['alpaca_chg_pct']:+.2f}%) (20%)",
        f"  Fear & Greed     : {report['fg_bias'].upper()} "
        f"({report['fg_score']:.0f} — {report['fg_rating']}) (15%)",
        f"  News sentiment   : {report['news_bias'].upper()} "
        f"(score={report['news_score']}) (10%)",
        f"  VIX level        : {report['vix_bias'].upper()} "
        f"(VIX={report['vix_level']:.1f}) (10%)",
        f"  Futures (ES)     : {report['futures_bias'].upper()} "
        f"({report['futures_chg']:+.2f}%) (5%)",
        f"",
        f"  Decision: Prefer {'CALL' if bias=='bullish' else 'PUT' if bias=='bearish' else 'both'} trades",
        f"  Override: /usbias bullish|bearish|neutral",
    ]

    if report["headlines"]:
        lines += [f"", f"  <b>Headlines:</b>"]
        for h in report["headlines"][:3]:
            lines.append(f"  • {h[:80]}")

    return "\n".join(lines)


def format_reversal_alert(risk, proceed, signals, summary, strategy, direction):
    """Format reversal risk alert for Telegram."""
    icon = "✅" if proceed else "🛑"
    lines = [
        f"{icon} <b>PRE-TRADE CHECK — {strategy} {direction.upper()}</b>",
        f"  Risk level : {risk}",
        f"  Decision   : {'PROCEED' if proceed else 'SKIP TRADE'}",
        f"  Summary    : {summary}",
    ]
    if signals:
        lines += [f"", f"  <b>Reversal signals detected:</b>"]
        for s in signals:
            lines.append(f"  {s[:100]}")
    return "\n".join(lines)
