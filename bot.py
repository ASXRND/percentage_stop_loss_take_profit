# Импорт необходимых библиотек
import pandas as pd
import ccxt
import talib
from datetime import datetime, timezone
import time
import threading
import sys
import telegram
from telegram import Bot
from telegram.error import TelegramError
import asyncio
import concurrent.futures
import signal
import numpy as np
import psycopg2

# Глобальный event loop и executor
loop = None
executor = None

# ===== НАСТРОЙКИ СКРИПТА =====

# Настройки Telegram
TELEGRAM_TOKEN = '7541095254:AAF_X981BdUpnQdsqfl2YLppJeFutceyjk8'
TELEGRAM_CHAT_ID = '-1002643357491'

# Новый список только для фьючерсов Bybit (пример, можно расширить)
SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT',
    'DOGE/USDT', 'WIF/USDT', 'ANIME/USDT', 'LINK/USDT',
    'SUI/USDT', 'AXL/USDT', 'AAVE/USDT', 'ADA/USDT', 'TON/USDT'
]

# Параметры индикаторов и таймфрейма
TIMEFRAME = '5m'
EMA_FAST = 9
EMA_SLOW = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CHECK_INTERVAL = 60

# УЛУЧШЕННЫЕ ПАРАМЕТРЫ ДЛЯ SL/TP
SL_METHODS = {
    'percentage': {'buy': 0.02, 'sell': 0.02},  # 2% стоп-лосс
    'atr_multiplier': 2.5,  # Увеличенный множитель ATR
    'swing_lookback': 20    # Период для поиска экстремумов
}

TP_METHODS = {
    'risk_reward_ratio': 2.5,  # Увеличенное соотношение
    'percentage': {'buy': 0.05, 'sell': 0.05},  # 5% тейк-профит
    'resistance_support': True  # Учет уровней поддержки/сопротивления
}

# Минимальные размеры SL/TP в процентах от цены
MIN_SL_PERCENT = 0.015  # 1.5%
MIN_TP_PERCENT = 0.03   # 3%

# Настройка подключений
exchange = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
})

try:
    bot = Bot(token=TELEGRAM_TOKEN)
    print("Telegram бот успешно подключен!")
except Exception as e:
    print(f"Ошибка подключения Telegram бота: {e}")
    sys.exit(1)

DB_CONFIG = {
    "host": "192.168.0.121",
    "port": 5432,
    "user": "asx",
    "password": "asxAdmin1",
    "dbname": "signals_db"
}

# ===== УЛУЧШЕННЫЙ КЛАСС ДЛЯ АНАЛИЗА =====
class SymbolAnalyzer:
    def __init__(self, symbol):
        self.symbol = symbol
        self.last_signal = None
        self.last_price = None
        self.stop_loss = None
        self.take_profit = None

    def fetch_ohlcv(self):
        try:
            ohlcv = exchange.fetch_ohlcv(self.symbol, TIMEFRAME, limit=200, params={'category': 'linear'})  # Явно указываем фьючерсы
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df.set_index('timestamp')
        except Exception as e:
            print(f"Ошибка загрузки {self.symbol}: {e}")
            return None

    def find_swing_levels(self, df, lookback=20):
        """Поиск уровней поддержки и сопротивления"""
        highs = df['high'].rolling(window=lookback, center=True).max()
        lows = df['low'].rolling(window=lookback, center=True).min()
        
        # Находим последние значительные экстремумы
        recent_high = df['high'].tail(lookback).max()
        recent_low = df['low'].tail(lookback).min()
        
        return recent_high, recent_low

    def calculate_dynamic_sl_tp(self, df, signal_type):
        """Улучшенный расчет SL и TP с несколькими методами"""
        current_price = df['close'].iloc[-1]
        self.last_price = current_price
        
        # Расчет ATR
        atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]
        
        # Поиск swing уровней
        resistance, support = self.find_swing_levels(df, SL_METHODS['swing_lookback'])
        
        # Расчет волатильности (стандартное отклонение)
        volatility = df['close'].pct_change().tail(20).std() * current_price
        
        if signal_type == 'BUY':
            # Методы расчета SL для покупки
            sl_atr = current_price - (atr * SL_METHODS['atr_multiplier'])
            sl_percentage = current_price * (1 - SL_METHODS['percentage']['buy'])
            sl_swing = support * 0.995  # Чуть ниже поддержки
            sl_volatility = current_price - (volatility * 2)
            
            # Выбираем наиболее консервативный (ближайший к цене) SL
            potential_sl = [sl_atr, sl_percentage, sl_swing, sl_volatility]
            self.stop_loss = max([sl for sl in potential_sl if sl > 0])
            
            # Проверка минимального размера SL
            min_sl = current_price * (1 - MIN_SL_PERCENT)
            if self.stop_loss > min_sl:
                self.stop_loss = min_sl
            
            # Методы расчета TP для покупки
            tp_rr = current_price + ((current_price - self.stop_loss) * TP_METHODS['risk_reward_ratio'])
            tp_percentage = current_price * (1 + TP_METHODS['percentage']['buy'])
            tp_resistance = resistance * 1.005  # Чуть ниже сопротивления
            
            # Выбираем наиболее реалистичный TP
            self.take_profit = min([tp for tp in [tp_rr, tp_percentage, tp_resistance] if tp > current_price])
            
        elif signal_type == 'SELL':
            # Методы расчета SL для продажи
            sl_atr = current_price + (atr * SL_METHODS['atr_multiplier'])
            sl_percentage = current_price * (1 + SL_METHODS['percentage']['sell'])
            sl_swing = resistance * 1.005  # Чуть выше сопротивления
            sl_volatility = current_price + (volatility * 2)
            
            # Выбираем наиболее консервативный SL
            potential_sl = [sl_atr, sl_percentage, sl_swing, sl_volatility]
            self.stop_loss = min([sl for sl in potential_sl if sl > 0])
            
            # Проверка минимального размера SL
            max_sl = current_price * (1 + MIN_SL_PERCENT)
            if self.stop_loss < max_sl:
                self.stop_loss = max_sl
            
            # Методы расчета TP для продажи
            tp_rr = current_price - ((self.stop_loss - current_price) * TP_METHODS['risk_reward_ratio'])
            tp_percentage = current_price * (1 - TP_METHODS['percentage']['sell'])
            tp_support = support * 0.995  # Чуть выше поддержки
            
            # Выбираем наиболее реалистичный TP
            self.take_profit = max([tp for tp in [tp_rr, tp_percentage, tp_support] if tp < current_price])

        # Проверка минимального размера TP
        if signal_type == 'BUY':
            min_tp = current_price * (1 + MIN_TP_PERCENT)
            if self.take_profit < min_tp:
                self.take_profit = min_tp
        else:
            max_tp = current_price * (1 - MIN_TP_PERCENT)
            if self.take_profit > max_tp:
                self.take_profit = max_tp

        return current_price, self.stop_loss, self.take_profit

    def calculate_position_size(self, account_balance, risk_percent=1):
        """Расчет размера позиции на основе риска"""
        if self.last_price and self.stop_loss:
            risk_amount = account_balance * (risk_percent / 100)
            if self.last_signal and 'BUY' in self.last_signal:
                price_diff = abs(self.last_price - self.stop_loss)
            else:
                price_diff = abs(self.stop_loss - self.last_price)
            
            if price_diff > 0:
                position_size = risk_amount / price_diff
                return position_size
        return None

    def get_additional_confirmations(self, df, signal_type):
        """Дополнительные подтверждения сигнала с учетом направления"""
        confirmations = []
        warnings = []
        
        # RSI
        rsi = talib.RSI(df['close'], timeperiod=14).iloc[-1]
        if signal_type == 'BUY':
            if rsi < 50:
                confirmations.append(f"RSI благоприятный: {rsi:.1f}")
            elif rsi > 70:
                warnings.append(f"⚠️ RSI перекуплен: {rsi:.1f}")
        else:  # SELL
            if rsi > 50:
                confirmations.append(f"RSI благоприятный: {rsi:.1f}")
            elif rsi < 30:
                warnings.append(f"⚠️ RSI перепродан: {rsi:.1f}")
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = talib.BBANDS(df['close'], timeperiod=20)
        current_price = df['close'].iloc[-1]
        bb_position = (current_price - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1])
        
        if signal_type == 'BUY':
            if bb_position < 0.3:  # В нижней части канала
                confirmations.append(f"BB позиция благоприятна: {bb_position*100:.0f}%")
            elif bb_position > 0.8:  # В верхней части канала
                warnings.append(f"⚠️ Цена у верхней границы BB: {bb_position*100:.0f}%")
        else:  # SELL
            if bb_position > 0.7:  # В верхней части канала
                confirmations.append(f"BB позиция благоприятна: {bb_position*100:.0f}%")
            elif bb_position < 0.2:  # В нижней части канала
                warnings.append(f"⚠️ Цена у нижней границы BB: {bb_position*100:.0f}%")
        
        # Volume analysis
        avg_volume = df['volume'].tail(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / avg_volume
        
        if volume_ratio > 1.5:
            confirmations.append(f"Повышенный объем: {volume_ratio:.1f}x")
        elif volume_ratio < 0.7:
            warnings.append(f"⚠️ Низкий объем: {volume_ratio:.1f}x")
        
        # Проверка силы тренда с помощью ADX
        try:
            adx = talib.ADX(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]
            if adx > 25:
                confirmations.append(f"Сильный тренд ADX: {adx:.1f}")
            elif adx < 20:
                warnings.append(f"⚠️ Слабый тренд ADX: {adx:.1f}")
        except:
            pass
        
        # Анализ последних свечей
        last_3_candles = df.tail(3)
        bullish_candles = sum(last_3_candles['close'] > last_3_candles['open'])
        bearish_candles = 3 - bullish_candles
        
        if signal_type == 'BUY' and bullish_candles >= 2:
            confirmations.append(f"Бычьи свечи: {bullish_candles}/3")
        elif signal_type == 'SELL' and bearish_candles >= 2:
            confirmations.append(f"Медвежьи свечи: {bearish_candles}/3")
        elif signal_type == 'BUY' and bearish_candles >= 2:
            warnings.append(f"⚠️ Преобладают медвежьи свечи: {bearish_candles}/3")
        elif signal_type == 'SELL' and bullish_candles >= 2:
            warnings.append(f"⚠️ Преобладают бычьи свечи: {bullish_candles}/3")
        
        return confirmations, warnings

    def get_higher_tf_trend(self):
        """Получить тренд на старшем таймфрейме (1h EMA9/EMA21)"""
        try:
            ohlcv = exchange.fetch_ohlcv(self.symbol, '1h', limit=50, params={'category': 'linear'})  # Явно указываем фьючерсы
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            ema_fast = talib.EMA(df['close'], timeperiod=EMA_FAST).iloc[-1]
            ema_slow = talib.EMA(df['close'], timeperiod=EMA_SLOW).iloc[-1]
            if ema_fast > ema_slow:
                return 'UP'
            elif ema_fast < ema_slow:
                return 'DOWN'
            else:
                return 'FLAT'
        except Exception as e:
            print(f"Ошибка получения тренда 1h для {self.symbol}: {e}")
            return None

    def is_liquidity_time(self):
        """Проверка, находится ли текущее время в периоде высокой ликвидности (UTC)"""
        now_utc = datetime.now(timezone.utc)
        # Пример: не торговать с 2:00 до 7:00 UTC (азиатская ночь)
        if 2 <= now_utc.hour < 7:
            return False, now_utc.strftime('%H:%M UTC')
        return True, now_utc.strftime('%H:%M UTC')

    def get_candle_pattern(self, df, signal_type):
        """Проверка на разворотные свечные паттерны (engulfing) на последних 3 свечах"""
        try:
            last3 = df.tail(3)
            # Bullish engulfing
            bull = talib.CDLENGULFING(last3['open'], last3['high'], last3['low'], last3['close']).iloc[-1] > 0
            # Bearish engulfing
            bear = talib.CDLENGULFING(last3['open'], last3['high'], last3['low'], last3['close']).iloc[-1] < 0
            if signal_type == 'BUY' and bull:
                return 'Бычье поглощение (усиление сигнала)'
            if signal_type == 'SELL' and bear:
                return 'Медвежье поглощение (усиление сигнала)'
            if signal_type == 'BUY' and bear:
                return 'Медвежье поглощение (ослабление сигнала)'
            if signal_type == 'SELL' and bull:
                return 'Бычье поглощение (ослабление сигнала)'
        except Exception as e:
            print(f"Ошибка анализа свечного паттерна: {e}")
        return None

    def get_dynamic_atr_threshold(self, df):
        """Вычисляет среднее ATR/цена за неделю и возвращает динамический порог (например, 0.5 от среднего)"""
        atr_week = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).tail(2016)  # 2016 5m свечей ≈ 1 неделя
        price_week = df['close'].tail(2016)
        atr_price_week = (atr_week / price_week).dropna()
        if len(atr_price_week) == 0:
            return 0.001  # fallback
        mean_atr_price = atr_price_week.mean() * 100  # в процентах
        return mean_atr_price * 0.5, mean_atr_price

    def analyze(self):
        df = self.fetch_ohlcv()
        if df is None or len(df) < 100:
            return None

        # Вычисление индикаторов
        df['ema_fast'] = talib.EMA(df['close'], timeperiod=EMA_FAST)
        df['ema_slow'] = talib.EMA(df['close'], timeperiod=EMA_SLOW)
        df['macd'], df['signal'], _ = talib.MACD(df['close'],
                                                fastperiod=MACD_FAST,
                                                slowperiod=MACD_SLOW,
                                                signalperiod=MACD_SIGNAL)

        # Текущие и предыдущие значения
        current_macd = df['macd'].iloc[-1]
        current_signal_line = df['signal'].iloc[-1]
        prev_macd = df['macd'].iloc[-2]
        prev_signal_line = df['signal'].iloc[-2]
        
        # Дополнительная фильтрация: проверяем тренд EMA
        current_ema_fast = df['ema_fast'].iloc[-1]
        current_ema_slow = df['ema_slow'].iloc[-1]

        # Получаем тренд старшего таймфрейма
        higher_tf_trend = self.get_higher_tf_trend()

        # Проверка времени суток (ликвидность)
        is_liq, now_utc_str = self.is_liquidity_time()
        time_note = f"\n<b>Время (UTC):</b> {now_utc_str}"
        if not is_liq:
            print(f"Сигнал по {self.symbol} отклонён: низкая ликвидность (ночь UTC)")
            return None

        # Динамический порог ATR/цена
        dynamic_threshold, mean_atr_price = self.get_dynamic_atr_threshold(df)

        # Расчёт ATR и волатильности
        atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]
        current_price = df['close'].iloc[-1]
        atr_percent = (atr / current_price) * 100
        volatility_note = f"\n<b>Волатильность (ATR):</b> {atr_percent:.2f}% (среднее: {mean_atr_price:.2f}%)"
        if atr_percent < dynamic_threshold:
            print(f"Сигнал по {self.symbol} отклонён: низкая волатильность {atr_percent:.2f}% (порог: {dynamic_threshold:.2f}%, среднее: {mean_atr_price:.2f}%)")
            return None
        if atr_percent > 3:
            print(f"Сигнал по {self.symbol} отклонён: слишком высокая волатильность ({atr_percent:.2f}%)")
            return None

        signal = None
        trend_note = ""

        # Сигнал на покупку с дополнительными условиями
        if (prev_macd < prev_signal_line and current_macd > current_signal_line and 
            current_ema_fast > current_ema_slow):  # EMA подтверждение
            
            entry_price, sl, tp = self.calculate_dynamic_sl_tp(df, 'BUY')
            confirmations, warnings = self.get_additional_confirmations(df, 'BUY')
            # Свечной паттерн
            pattern_note = self.get_candle_pattern(df, 'BUY')
            if pattern_note:
                if 'усиление' in pattern_note:
                    confirmations.append(f"Свечной паттерн: {pattern_note}")
                elif 'ослабление' in pattern_note:
                    warnings.append(f"Свечной паттерн: {pattern_note}")
            
            # Проверяем, есть ли критические предупреждения
            critical_warnings = [w for w in warnings if '⚠️' in w or 'ослабление' in w]
            if len(critical_warnings) >= 2:
                print(f"Сигнал BUY для {self.symbol} отклонен из-за предупреждений: {critical_warnings}")
                return None
            
            # Фильтрация по тренду старшего ТФ
            if higher_tf_trend == 'DOWN':
                print(f"BUY сигнал по {self.symbol} против тренда 1h — отклонён")
                return None
            elif higher_tf_trend == 'UP':
                trend_note = "\n<b>Тренд 1h:</b> Восходящий (усиление сигнала)"
            else:
                trend_note = "\n<b>Тренд 1h:</b> Неопределён"
            
            # Расчет процентов риска и прибыли
            risk_percent = ((entry_price - sl) / entry_price) * 100
            reward_percent = ((tp - entry_price) / entry_price) * 100
            rr_ratio = reward_percent / risk_percent if risk_percent > 0 else 0
            # Новое: предположительное движение цены
            move_percent = ((tp - entry_price) / entry_price) * 100
            move_note = f"\n<b>Потенциальное движение:</b> +{move_percent:.2f}%"  # всегда с плюсом для LONG
            
            # Определяем качество сигнала
            signal_quality = "🟢 СИЛЬНЫЙ" if len(confirmations) >= 3 else "🟡 СРЕДНИЙ" if len(confirmations) >= 2 else "🔴 СЛАБЫЙ"
            
            confirmation_text = "\n".join([f"- {conf}" for conf in confirmations]) if confirmations else "- Нет подтверждений"
            warning_text = "\n".join([f"- {warn}" for warn in warnings]) if warnings else ""
            
            # Определяем направление сделки
            direction = "<b>ЛОНГ (LONG)</b>"  # для BUY
            
            signal = (
                f" 2️⃣ 📈 {direction} <b>СИГНАЛ НА ПОКУПКУ</b> ({signal_quality})\n"
                f"🔹 Пара: <b>{self.symbol}</b>\n"
                f"⏱ Таймфрейм: {TIMEFRAME}\n"
                f"🕒 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{time_note}\n\n"
                f"💰 <b>Цена входа</b>: {entry_price:.6f}\n"
                f"🛑 <b>Стоп-лосс</b>: {sl:.6f} (-{risk_percent:.2f}%)\n"
                f"🎯 <b>Тейк-профит</b>: {tp:.6f} (+{reward_percent:.2f}%)\n"
                f"⚡️ {move_note}\n"
                f"⚖️ <b>R/R соотношение</b>: 1:{rr_ratio:.2f}\n\n"
                f" 2️⃣ 📊 <b>Техническое обоснование</b>:\n"
                f"- MACD пересечение сигнальной линии ↗️\n"
                f"- EMA9 выше EMA21 (восходящий тренд)\n"
                f"- Динамический расчет SL/TP\n"
                f"{trend_note}{volatility_note}\n\n"
                f"✅ <b>Подтверждения</b>:\n{confirmation_text}\n"
                + (f"\n⚠️ <b>Предупреждения</b>:\n{warning_text}\n" if warnings else "") +
                f"\n#BUY #{self.symbol.replace('/', '')}"
            )

        # Сигнал на продажу с дополнительными условиями
        elif (prev_macd > prev_signal_line and current_macd < current_signal_line and 
              current_ema_fast < current_ema_slow):  # EMA подтверждение
            
            entry_price, sl, tp = self.calculate_dynamic_sl_tp(df, 'SELL')
            confirmations, warnings = self.get_additional_confirmations(df, 'SELL')
            # Свечной паттерн
            pattern_note = self.get_candle_pattern(df, 'SELL')
            if pattern_note:
                if 'усиление' in pattern_note:
                    confirmations.append(f"Свечной паттерн: {pattern_note}")
                elif 'ослабление' in pattern_note:
                    warnings.append(f"Свечной паттерн: {pattern_note}")
            
            # Проверяем, есть ли критические предупреждения
            critical_warnings = [w for w in warnings if '⚠️' in w or 'ослабление' in w]
            if len(critical_warnings) >= 2:
                print(f"Сигнал SELL для {self.symbol} отклонен из-за предупреждений: {critical_warnings}")
                return None
            
            # Фильтрация по тренду старшего ТФ
            if higher_tf_trend == 'UP':
                print(f"SELL сигнал по {self.symbol} против тренда 1h — отклонён")
                return None
            elif higher_tf_trend == 'DOWN':
                trend_note = "\n<b>Тренд 1h:</b> Нисходящий (усиление сигнала)"
            else:
                trend_note = "\n<b>Тренд 1h:</b> Неопределён"
            
            # Расчет процентов риска и прибыли
            risk_percent = ((sl - entry_price) / entry_price) * 100
            reward_percent = ((entry_price - tp) / entry_price) * 100
            rr_ratio = reward_percent / risk_percent if risk_percent > 0 else 0
            move_percent = ((tp - entry_price) / entry_price) * 100
            move_note = f"\n<b>Потенциальное движение:</b> {move_percent:+.2f}%"  # всегда с знаком
            
            # Определяем качество сигнала
            signal_quality = "🟢 СИЛЬНЫЙ" if len(confirmations) >= 3 else "🟡 СРЕДНИЙ" if len(confirmations) >= 2 else "🔴 СЛАБЫЙ"
            
            confirmation_text = "\n".join([f"- {conf}" for conf in confirmations]) if confirmations else "- Нет подтверждений"
            warning_text = "\n".join([f"- {warn}" for warn in warnings]) if warnings else ""
            
            # Определяем направление сделки
            direction = "<b>ШОРТ (SHORT)</b>"  # для SELL
            
            signal = (
                f" 2️⃣ 📉 {direction} <b>СИГНАЛ НА ПРОДАЖУ</b> ({signal_quality})\n"
                f"🔹 Пара: <b>{self.symbol}</b>\n"
                f"⏱ Таймфрейм: {TIMEFRAME}\n"
                f"🕒 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{time_note}\n\n"
                f"💰 <b>Цена входа</b>: {entry_price:.6f}\n"
                f"🛑 <b>Стоп-лосс</b>: {sl:.6f} (+{risk_percent:.2f}%)\n"
                f"🎯 <b>Тейк-профит</b>: {tp:.6f} ({move_percent:+.2f}%)\n"  # всегда с плюсом/минусом
                f"⚡️ {move_note}\n"
                f"⚖️ <b>R/R соотношение</b>: 1:{rr_ratio:.2f}\n\n"
                f"📊 <b>Техническое обоснование</b>:\n"
                f"- MACD пересечение сигнальной линии ↘️\n"
                f"- EMA9 ниже EMA21 (нисходящий тренд)\n"
                f"- Динамический расчет SL/TP\n"
                f"{trend_note}{volatility_note}\n\n"
                f"✅ <b>Подтверждения</b>:\n{confirmation_text}\n"
                + (f"\n⚠️ <b>Предупреждения</b>:\n{warning_text}\n" if warnings else "") +
                f"\n#SELL #{self.symbol.replace('/', '')}"
            )

        # Проверка на новый сигнал
        if signal and self.last_signal != signal:
            self.last_signal = signal
            return signal
        return None

# ===== АСИНХРОННАЯ ФУНКЦИЯ ОТПРАВКИ В TELEGRAM =====
async def send_telegram_message(message):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        print("Сообщение успешно отправлено в Telegram")
    except TelegramError as e:
        print(f"Ошибка отправки в Telegram: {e}")
    except Exception as e:
        print(f"Неизвестная ошибка при отправке: {e}")

# ===== БЕЗОПАСНЫЙ ВЫЗОВ send_telegram_message ИЗ ПОТОКОВ =====
def send_telegram_message_safe(message):
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_telegram_message(message), loop
        )
        future.result(timeout=10)
    except Exception as e:
        print(f"Ошибка безопасной отправки в Telegram: {e}")

# Функция для сохранения сигнала в базу данных
def save_signal_to_db(symbol, direction, entry_price, stop_loss, take_profit, risk_percent, reward_percent, rr_ratio, move_percent, signal_quality, confirmations, warnings, signal_time):
    """Сохраняет сигнал в базу данных Postgres. Если таблицы нет — создаёт её."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        # Проверяем, существует ли таблица
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name = 'signals_sl_tp'
            )
        """)
        exists = cur.fetchone()[0]
        if not exists:
            cur.execute('''
                CREATE TABLE signals_sl_tp (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(32),
                    direction VARCHAR(8),
                    entry_price NUMERIC,
                    stop_loss NUMERIC,
                    take_profit NUMERIC,
                    risk_percent NUMERIC,
                    reward_percent NUMERIC,
                    rr_ratio NUMERIC,
                    move_percent NUMERIC,
                    signal_quality VARCHAR(16),
                    confirmations TEXT,
                    warnings TEXT,
                    signal_time TIMESTAMP
                )
            ''')
            conn.commit()
        cur.execute('''
            INSERT INTO signals_sl_tp (
                symbol, direction, entry_price, stop_loss, take_profit, risk_percent, reward_percent, rr_ratio, move_percent, signal_quality, confirmations, warnings, signal_time
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            symbol, direction, entry_price, stop_loss, take_profit, risk_percent, reward_percent, rr_ratio, move_percent, signal_quality, confirmations, warnings, signal_time
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Ошибка сохранения сигнала в БД: {e}")

# ===== ФУНКЦИЯ МОНИТОРИНГА ПАРЫ =====
def monitor_symbol(symbol, output_lock):
    analyzer = SymbolAnalyzer(symbol)
    while True:
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Мониторинг {symbol}...", flush=True)
            signal = analyzer.analyze()
            if signal:
                with output_lock:
                    print("\n" + "="*80)
                    print(signal.replace('<b>', '').replace('</b>', ''))
                    print("="*80 + "\n")
                    send_telegram_message_safe(signal)
                    # --- Сохраняем сигнал в БД ---
                    # Парсим основные параметры из analyzer и текста сигнала
                    direction = 'LONG' if 'ЛОНГ' in signal else 'SHORT'
                    entry_price = analyzer.last_price
                    stop_loss = analyzer.stop_loss
                    take_profit = analyzer.take_profit
                    # Для risk_percent, reward_percent, rr_ratio, move_percent, signal_quality, confirmations, warnings
                    # используем значения из analyze (их можно возвращать из analyze как кортеж, но сейчас парсим из текста)
                    # Для простоты: ищем по ключевым словам
                    import re
                    def extract_percent(pattern, text):
                        m = re.search(pattern, text)
                        return float(m.group(1).replace(',', '.')) if m else None
                    risk_percent = extract_percent(r'Стоп-лосс.*?([\-\+]?[0-9.]+)%', signal)
                    reward_percent = extract_percent(r'Тейк-профит.*?([\-\+]?[0-9.]+)%', signal)
                    rr_ratio = extract_percent(r'R/R соотношение.*?1:([0-9.]+)', signal)
                    move_percent = extract_percent(r'Потенциальное движение:</b> ?([\-\+]?[0-9.]+)%', signal)
                    signal_quality = 'СИЛЬНЫЙ' if 'СИЛЬНЫЙ' in signal else ('СРЕДНИЙ' if 'СРЕДНИЙ' in signal else 'СЛАБЫЙ')
                    # Подтверждения и предупреждения
                    confirmations = ''
                    warnings = ''
                    conf_match = re.search(r'Подтверждения</b>:\n(.*?)(?:\n⚠️|\n#|$)', signal, re.DOTALL)
                    if conf_match:
                        confirmations = conf_match.group(1).strip()
                    warn_match = re.search(r'Предупреждения</b>:\n(.*?)(?:\n#|$)', signal, re.DOTALL)
                    if warn_match:
                        warnings = warn_match.group(1).strip()
                    signal_time = datetime.now(timezone.utc)
                    save_signal_to_db(
                        symbol, direction, entry_price, stop_loss, take_profit,
                        risk_percent, reward_percent, rr_ratio, move_percent,
                        signal_quality, confirmations, warnings, signal_time
                    )
                time.sleep(CHECK_INTERVAL * 3)  # Увеличена пауза после сигнала
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"Ошибка в {symbol}: {e}")
            time.sleep(10)

# ===== ОСНОВНАЯ ФУНКЦИЯ =====
def main():
    global loop, executor
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    executor = concurrent.futures.ThreadPoolExecutor()
    
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    
    print(f"Запуск улучшенного мониторинга для {len(SYMBOLS)} пар на таймфрейме {TIMEFRAME}...")
    print(f"Минимальный SL: {MIN_SL_PERCENT*100}%, минимальный TP: {MIN_TP_PERCENT*100}%")
    print(f"ATR множитель: {SL_METHODS['atr_multiplier']}, R/R: {TP_METHODS['risk_reward_ratio']}")

    # Стартовое сообщение
    send_telegram_message_safe(
        f"🤖 <b>Торговый бот запущен!</b>\n"
        f"📊 Мониторинг: {len(SYMBOLS)} пар на {TIMEFRAME}\n"
        f"🎯 Мин. SL/TP: {MIN_SL_PERCENT*100}%/{MIN_TP_PERCENT*100}%\n"
        f"⚖️ R/R соотношение: 1:{TP_METHODS['risk_reward_ratio']}\n"
        f"🔧 Динамический расчет уровней\n"
        f"✅ Дополнительные фильтры активны"
    )

    output_lock = threading.Lock()
    threads = []

    for symbol in SYMBOLS:
        t = threading.Thread(target=monitor_symbol, args=(symbol, output_lock))
        t.daemon = True
        threads.append(t)
        t.start()

    def signal_handler(sig, frame):
        print("\nПолучен сигнал завершения...")
        send_telegram_message_safe("🔴 Торговый бот остановлен")
        loop.call_soon_threadsafe(loop.stop)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)

if __name__ == "__main__":
    main()

# Глобальное логирование средней волатильности по рынку
import threading
import time

def log_market_volatility():
    while True:
        atrs = []
        prices = []
        for symbol in SYMBOLS:
            try:
                analyzer = SymbolAnalyzer(symbol)
                df = analyzer.fetch_ohlcv()
                if df is not None and len(df) > 100:
                    atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]
                    price = df['close'].iloc[-1]
                    atrs.append(atr)
                    prices.append(price)
            except Exception as e:
                print(f"[Волатильность] Ошибка по {symbol}: {e}")
        if atrs and prices:
            mean_vol = np.mean([a/p for a,p in zip(atrs, prices)]) * 100
            print(f"[Волатильность] Средняя ATR/цена по рынку: {mean_vol:.3f}%")
        else:
            print("[Волатильность] Недостаточно данных для расчёта средней волатильности.")
        time.sleep(3600)  # раз в час

# Запуск логирования волатильности в отдельном потоке
vol_thread = threading.Thread(target=log_market_volatility, daemon=True)
vol_thread.start()