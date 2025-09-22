from telegram import Update
from telegram.ext import CallbackContext
# bot.py
import os
import time
import json
import threading

import logging

# Логирование в файл matches.log
logging.basicConfig(
    filename="matches.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s"
)

# Функция для записи проверки матча
def log_match(match_name, status="checked"):
    logging.info(f"{match_name} - {status}")

# Команда /checked для просмотра последних матчей
def checked(update: Update, context: CallbackContext):
    try:
        with open("matches.log", "r", encoding="utf-8") as f:
            lines = f.readlines()[-10:]  # последние 10 записей
        if not lines:
            update.message.reply_text("Пока нет проверенных матчей.")
        else:
            update.message.reply_text("Последние проверки:\n" + "".join(lines))
    except FileNotFoundError:
        update.message.reply_text("Лог-файл ещё не создан.")

import asyncio
import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Optional

import aiohttp
import telebot
from flask import Flask, request

# ------------ Настройки / переменные окружения ------------
TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # пример https://telegram-bot-m5l2.onrender.com
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в environment variables")

bot = telebot.TeleBot(TOKEN)
server = Flask(__name__)

# ------------ Асинхронный loop в отдельном потоке ------------
_async_loop = asyncio.new_event_loop()


def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=_start_loop, args=(_async_loop,), daemon=True).start()


def run_coro(coro):
    """Запланировать корутину в фоновом loop'е"""
    return asyncio.run_coroutine_threadsafe(coro, _async_loop)


# ------------ База для логов сигналов ------------
DB_PATH = "signals.db"
_conn = None
_conn_lock = threading.Lock()


def init_db():
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            cur = _conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    event_id INTEGER,
                    league TEXT,
                    home TEXT,
                    away TEXT,
                    quarter TEXT,
                    line REAL,
                    recommendation TEXT,
                    status TEXT,
                    points_in_quarter INTEGER
                )"""
            )
            _conn.commit()


def save_signal_log(event_id, league, home, away, quarter, line, recommendation, status, points):
    init_db()
    ts = datetime.utcnow().isoformat()
    with _conn_lock:
        cur = _conn.cursor()
        cur.execute(
            "INSERT INTO signals (ts,event_id,league,home,away,quarter,line,recommendation,status,points_in_quarter) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, event_id, league, home, away, quarter, line, recommendation, status, points),
        )
        _conn.commit()


# ------------ Состояние анализа ------------
analyzing = False
monitor_task_future = None
# Отправленные сигналы: event_id -> {quarter: True}
sent_signals: Dict[int, Dict[int, Dict[str, Any]]] = {}

# ------------ HTTP клиента (aiohttp) ------------
aio_session: Optional[aiohttp.ClientSession] = None


async def get_aio_session():
    global aio_session
    if aio_session is None:
        aio_session = aiohttp.ClientSession()
    return aio_session


# ------------ Получение списка live матчей (SofaScore) ------------
async def get_live_events() -> List[Dict[str, Any]]:
    """
    Возвращает список текущих live-событий (basketball).
    Возвращаемый формат — список объектов с по крайней мере полями:
      id, homeTeam.name, awayTeam.name, status (period, description), homeScore.current, awayScore.current
    """
    try:
        session = await get_aio_session()
        url = "https://api.sofascore.com/api/v1/sport/basketball/events/live"
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            events = data.get("events") or data.get("events", [])
            return events
    except Exception:
        # если SofaScore недоступен — возвращаем пустой список
        return []


# ------------ Получение подробностей матча (summary / incidents) ------------
async def get_event_summary(event_id: int) -> Dict[str, Any]:
    """
    Возвращает подробности матча: периодные очки, фолы, состав, play-by-play.
    Попробуем несколько эндпойнтов SofaScore.
    """
    session = await get_aio_session()
    base = "https://api.sofascore.com/api/v1/event"
    result = {}
    try:
        # match summary содержит периодные счёты и базовую статистику
        url_summary = f"{base}/{event_id}/match-summary"
        async with session.get(url_summary, timeout=10) as r:
            if r.status == 200:
                result["summary"] = await r.json()
    except Exception:
        pass

    try:
        # incidents — play-by-play (замены, фолы, штрафные)
        url_inc = f"{base}/{event_id}/incidents"
        async with session.get(url_inc, timeout=10) as r2:
            if r2.status == 200:
                result["incidents"] = await r2.json()
    except Exception:
        pass

    return result


# ------------ Утилиты для парсинга данных ------------
def safe_get(d: Dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def parse_period_scores(summary_json: Dict[str, Any]):
    """
    Попытка извлечь очки по четвертям из match-summary или event->home/away periodScores.
    Вернёт список кортежей: [(h1,a1),(h2,a2),...]
    """
    periods = []
    # вариант: summary_json может иметь "periods" или "home"->"periodScores"
    try:
        if not summary_json:
            return periods
        # попытка 1: summary_json['periods']
        p = summary_json.get("periods")
        if p and isinstance(p, list):
            for item in p:
                # item может содержать "homeScore" и "awayScore"
                hs = item.get("homeScore") or item.get("home")
                ascore = item.get("awayScore") or item.get("away")
                if hs is not None and ascore is not None:
                    periods.append((int(hs), int(ascore)))
            if periods:
                return periods
        # попытка 2: summary_json['homeTeam']['periods'] и ['awayTeam']['periods']
        home = summary_json.get("homeTeam")
        away = summary_json.get("awayTeam")
        if home and away:
            # найти periodScores
            h_periods = home.get("periodScores") or home.get("periods")
            a_periods = away.get("periodScores") or away.get("periods")
            if h_periods and a_periods and len(h_periods) == len(a_periods):
                for i in range(len(h_periods)):
                    periods.append((int(h_periods[i].get("score", 0)), int(a_periods[i].get("score", 0))))
                return periods
    except Exception:
        return periods
    return periods


def get_current_period_and_clock(event_obj: Dict[str, Any]):
    """
    Попытка взять номер четверти и оставшееся/прошедшее время (строка)
    """
    try:
        status = event_obj.get("status", {})
        period = status.get("period") or status.get("currentPeriod") or 0
        desc = status.get("description") or status.get("time") or ""
        return int(period), str(desc)
    except Exception:
        return 0, ""


# ------------ Правила (стратегии) ------------
def evaluate_strategy_1(event, summary) -> Optional[Dict[str, Any]]:
    """
    Стратегия 1 — 3Q
    Возвращаем dict с данными сигнала, или None
    """
    # получаем текущую четверть
    period, clock = get_current_period_and_clock(event)
    if period != 3:
        return None

    # счёт
    home = safe_get(event, "homeTeam", "name", default="Home")
    away = safe_get(event, "awayTeam", "name", default="Away")
    home_score = safe_get(event, "homeScore", "current", default=safe_get(event, "homeScore", "period1"))
    away_score = safe_get(event, "awayScore", "current", default=safe_get(event, "awayScore", "period1"))
    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except Exception:
        return None

    # периодные очки
    periods = []
    if summary and "summary" in summary:
        periods = parse_period_scores(summary["summary"])

    # очки в 3-й четверти
    points_3q = None
    if len(periods) >= 3:
        h1, a1 = periods[0]
        h2, a2 = periods[1]
        h3, a3 = periods[2]
        points_3q = h3 + a3
    else:
        # fallback: оценим по разнице общего и суммы первых двух (если присутствуют)
        if len(periods) >= 2:
            s = sum([p[0] + p[1] for p in periods[:2]])
            points_3q = (home_score + away_score) - s

    # статистика фолов и темпа (из summary или incidents)
    fouls_home = fouls_away = None
    tempo_text = "неизвестно"
    lineup_info = "unknown"
    if summary:
        # попытка взять командные фолы из summary
        try:
            sumj = summary.get("summary") or {}
            teamStats = sumj.get("teamStats") or {}
            # иногда в другом формате — игнорируем, а используем incidents
        except Exception:
            pass
    # incidents — можно парсить play-by-play чтобы посчитать фолы
    if summary and "incidents" in summary:
        try:
            inc = summary["incidents"]
            # считаем фолы за текущую четверть
            fouls_home = fouls_away = 0
            for it in inc.get("incidents", []) if isinstance(inc, dict) else (inc or []):
                typ = it.get("type")
                team = it.get("team", {}).get("id")
                # немного эвристики: тип "foul" или "personalFoul"
                if typ and "foul" in str(typ).lower():
                    if team == safe_get(event, "homeTeam", "id"):
                        fouls_home += 1
                    else:
                        fouls_away += 1
        except Exception:
            fouls_home = fouls_away = None

    # Простая логика: если в 3Q уже набрано >= 12 очков (за 5 минут/половину четверти)
    # или points_3q >= 12 и tempo/фолы позволяют — даём сигнал.
    if points_3q is None:
        return None
    # thresholds — регулируемые
    if points_3q >= 12:
        line_estimate = 37.5  # пример, можно вычислять динамически
        return {
            "strategy": "3Q",
            "reason": f"Points in 3Q = {points_3q}",
            "home": home,
            "away": away,
            "points_in_quarter": points_3q,
            "line": line_estimate,
            "fouls": (fouls_home, fouls_away),
            "tempo": tempo_text,
            "line_type": "ТБ",
            "quarter": 3,
            "clock": clock,
        }
    return None


def evaluate_strategy_2(event, summary) -> Optional[Dict[str, Any]]:
    """
    Стратегия 2 — 4Q
    Возвращаем dict с данными сигнала, или None
    """
    period, clock = get_current_period_and_clock(event)
    if period != 4:
        return None

    home = safe_get(event, "homeTeam", "name", default="Home")
    away = safe_get(event, "awayTeam", "name", default="Away")
    try:
        home_score = int(safe_get(event, "homeScore", "current", default=0))
        away_score = int(safe_get(event, "awayScore", "current", default=0))
    except Exception:
        return None

    diff = abs(home_score - away_score)

    # quick thresholds
    if diff <= 7:
        # check fouls/time/tempo via summary/incidents (best-effort)
        fouls_home = fouls_away = None
        if summary and "incidents" in summary:
            try:
                inc = summary["incidents"]
                fouls_home = fouls_away = 0
                for it in inc.get("incidents", []) if isinstance(inc, dict) else (inc or []):
                    if "foul" in str(it.get("type", "")).lower():
                        if it.get("team", {}).get("id") == safe_get(event, "homeTeam", "id"):
                            fouls_home += 1
                        else:
                            fouls_away += 1
            except Exception:
                fouls_home = fouls_away = None

        line_estimate = 39.5
        return {
            "strategy": "4Q",
            "reason": f"Diff={diff}",
            "home": home,
            "away": away,
            "current_score": f"{home_score}:{away_score}",
            "line": line_estimate,
            "fouls": (fouls_home, fouls_away),
            "quarter": 4,
            "clock": clock,
            "line_type": "ТБ",
            "recommendation_type": "оптимальный",
        }
    return None


# ------------ Отправка сигнала и отметка в sent_signals ------------
def mark_signal_sent(event_id: int, quarter: int, payload: Dict[str, Any]):
    sent_signals.setdefault(event_id, {})
    sent_signals[event_id][quarter] = payload


# ------------ Формирование текстов сообщений ------------
def format_signal_message(event_id: int, signal: Dict[str, Any]):
    # Универсальный формат
    if signal["strategy"] == "3Q":
        return (
            f"🏀 Сигнал [3Q]\n"
            f"Матч: {signal.get('home')} – {signal.get('away')}\n"
            f"Текущий счёт: {safe_get(signal, 'current_score', default='-')}\n"
            f"Время: {signal.get('clock')}\n\n"
            f"📊 Данные:\n"
            f"— Очки в 3Q: {signal.get('points_in_quarter')} \n"
            f"— Фолы: {signal.get('fouls')}\n"
            f"— Темп: {signal.get('tempo')}\n"
            f"— Состав: {signal.get('line_type')}\n\n"
            f"💡 Рекомендация: {signal.get('line_type')} {signal.get('line')}\n"
            f"🎯 Вход: оптимальный\n"
            f"Причина: {signal.get('reason')}"
        )
    else:
        return (
            f"🏀 Сигнал [4Q]\n"
            f"Матч: {signal.get('home')} – {signal.get('away')}\n"
            f"Текущий счёт: {signal.get('current_score', '-')}\n"
            f"Время: {signal.get('clock')}\n\n"
            f"📊 Данные:\n"
            f"— Фолы: {signal.get('fouls')}\n"
            f"— Рекомендация: {signal.get('recommendation_type')}\n\n"
            f"💡 Рекомендация: {signal.get('line_type')} {signal.get('line')}\n"
            f"🎯 Вход: оптимальный\n"
            f"Причина: {signal.get('reason')}"
        )


def format_result_message(event_id: int, signal_payload: Dict[str, Any], points_in_quarter: int):
    line = signal_payload.get("line", 0)
    passed = (points_in_quarter > line)  # мы ставим ТБ
    mark = "✅ Прошла" if passed else "❌ Не прошла"
    return (
        f"{'✅' if passed else '❌'} Итог [{signal_payload.get('quarter')}Q]\n"
        f"Матч: {signal_payload.get('home')} – {signal_payload.get('away')}\n"
        f"Ставка: {signal_payload.get('line_type')} {line}\n"
        f"Очки в {signal_payload.get('quarter')}Q: {points_in_quarter} → {mark}"
    ), passed


# ------------ Анализ одного события (game) ------------
async def analyze_single_event(event: Dict[str, Any], chat_id: int):
    """
    Берёт event (как в live events), достаёт summary, проверяет стратегии и отправляет сигналы.
    А также отслеживает окончание четверти и посылает результат по ранее отправленным сигналам.
    """
    event_id = event.get("id")
    # берем summary/incidents
    summary = await get_event_summary(event_id)

    # parse some fields
    home = safe_get(event, "homeTeam", "name", default="Home")
    away = safe_get(event, "awayTeam", "name", default="Away")
    period, clock = get_current_period_and_clock(event)

    # Check Strategy 1 (3Q)
    sig1 = evaluate_strategy_1(event, summary)
    if sig1:
        # не шлём повторно для одной и той же четверти
        if not (sent_signals.get(event_id) and sent_signals[event_id].get(3)):
            msg = format_signal_message(event_id, sig1)
            try:
                bot.send_message(chat_id, msg)
            except Exception:
                pass
            mark_signal_sent(event_id, 3, sig1)
            # сохраняем лог с pending статус (будем обновлять после окончания четверти)
            save_signal_log(event_id, safe_get(event, "tournament", "name", default=""), home, away, "3Q", sig1.get("line", 0), "оптимальный", "PENDING", sig1.get("points_in_quarter"))

    # Check Strategy 2 (4Q)
    sig2 = evaluate_strategy_2(event, summary)
    if sig2:
        if not (sent_signals.get(event_id) and sent_signals[event_id].get(4)):
            msg = format_signal_message(event_id, sig2)
            try:
                bot.send_message(chat_id, msg)
            except Exception:
                pass
            mark_signal_sent(event_id, 4, sig2)
            save_signal_log(event_id, safe_get(event, "tournament", "name", default=""), home, away, "4Q", sig2.get("line", 0), "оптимальный", "PENDING", 0)

    # Проверка: если четверть только что завершилась, и у нас есть отправленный сигнал для этой четверти — отправляем итог
    # Попытаемся распарсить period scores и понять очки в последней завершенной
    periods = []
    try:
        if summary and "summary" in summary:
            periods = parse_period_scores(summary["summary"])
    except Exception:
        periods = []
    # если в periods появилась запись для законченной четверти и у нас есть сигнал
    if periods:
        # если в event period == n и periods length >= n -> проверить предыдущую как завершённую
        try:
            current_period_event = int(period)
        except Exception:
            current_period_event = current_period_event = len(periods) + 1 if periods else 0

        # Если есть сигнал по предыдущей четверти
        # i = previous finished quarter index (1-based)
        prev_q = current_period_event - 1
        if prev_q >= 1:
            # index in list is prev_q-1
            if prev_q <= len(periods):
                pts = periods[prev_q - 1][0] + periods[prev_q - 1][1]
                # проверяем, есть ли у нас сигнал на prev_q
                sent = sent_signals.get(event_id, {}).get(prev_q)
                if sent and sent.get("reported_result") is None:
                    # сформируем итог
                    res_msg, passed = format_result_message(event_id, sent, pts)
                    try:
                        bot.send_message(chat_id, res_msg)
                    except Exception:
                        pass
                    # пометим что отправлено итоговое сообщение
                    sent_signals[event_id][prev_q]["reported_result"] = True
                    # обновим запись в БД: status = PASSED/FAILED
                    status = "PASSED" if passed else "FAILED"
                    save_signal_log(event_id, safe_get(event, "tournament", "name", default=""), home, away, f"{prev_q}Q", sent.get("line", 0), "оптимальный", status, pts)


# ------------ Фоновый монитор всех live матчей ------------
async def monitor_all_games(chat_id: int):
    """
    Основной цикл: каждую секунду берём список live игр и анализируем каждую.
    """
    global analyzing
    try:
        while analyzing:
            events = await get_live_events()
            if not events:
                # ничего не играется
                await asyncio.sleep(1)
                continue
            # events может быть списком объектов с полем 'id'
            tasks = []
            for ev in events:
                # асинхронно анализируем каждую игру, но не блокируем основной цикл
                tasks.append(analyze_single_event(ev, chat_id))
            # выполняем параллельно (ограничим время выполнения)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(1)
    except Exception:
        # если ошибка в цикле — логируем и делаем паузу
        try:
            bot.send_message(chat_id, "⚠️ Ошибка в мониторинге; пытаюсь восстановить через 5 сек.")
        except Exception:
            pass
        await asyncio.sleep(5)
        # loop продолжит работу, если analyzing True


# ------------ Telegram команды (webhook mode) ------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(message, "Бот запущен. Используй /start_analiz чтобы включить анализ, /stop_analiz чтобы выключить.")


@bot.message_handler(commands=["start_analiz"])
def cmd_start_analiz(message):
    global analyzing, monitor_task_future
    if analyzing:
        bot.reply_to(message, "Анализ уже запущен.")
        return
    analyzing = True
    bot.reply_to(message, "✅ Запускаю мониторинг live-матчей. Бот будет анализировать каждую секунду.")
    # запускаем фоновой таск
    monitor_task_future = run_coro(monitor_all_games(message.chat.id))


@bot.message_handler(commands=["stop_analiz"])
def cmd_stop_analiz(message):
    global analyzing, monitor_task_future
    if not analyzing:
        bot.reply_to(message, "Анализ уже остановлен.")
        return
    analyzing = False
    # отмена таска (по возможности)
    if monitor_task_future:
        try:
            monitor_task_future.cancel()
        except Exception:
            pass
    bot.reply_to(message, "⛔ Анализ остановлен. Бот больше не будет опрашивать матчи.")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    bot.reply_to(message, f"Анализ запущен: {analyzing}")


# ------------ Webhook (Render) ------------
@server.route(f"/{TOKEN}", methods=["POST"])
def getMessage():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception:
        pass
    return "OK", 200


@server.route("/")
def set_webhook_route():
    # устанавливаем webhook на Render url
    try:
        bot.remove_webhook()
        url = RENDER_EXTERNAL_URL.rstrip("/") + "/" + TOKEN
        bot.set_webhook(url=url)
        return "Webhook установлен", 200
    except Exception as e:
        return f"Error setting webhook: {e}", 500


# ------------ Запуск (локальный режим — для отладки) ------------
if __name__ == "__main__":
    init_db()
    # если запускаешь локально без webhook, можно включить polling (не рекомендуем на Render)
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
