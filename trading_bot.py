"""
trading_bot.py — Python 3.14 + python-telegram-bot 21.x
Arquitectura:
  asyncio.run(main()) como punto de entrada unico.
  Application de PTB 21 corre en el loop principal via updater.start_polling().
  Flask corre en un thread daemon (no bloquea el loop).
  APScheduler usa BackgroundScheduler (su propio thread).
  send_telegram() es thread-safe via run_coroutine_threadsafe().
"""

import asyncio
import threading
import json
import os
import logging
from datetime import datetime, timedelta

import pytz
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ── Configuracion ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8738002791:AAGDU8Jjxe-z8XsuPXNVF-EQhF16_XOfsPw")
CHAT_ID   = os.getenv("CHAT_ID",   "8772999810")
TIMEZONE  = "Europe/Lisbon"

MLL_TRAILING_PCT = 0.06   # 6% trailing sobre balance maximo
RETIRO_PCT       = 0.40   # 40% retiro mensual
RIESGO_POR_TRADE = 15.0

CUENTA_FILE    = "cuenta.json"
HISTORIAL_FILE = "historial.json"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

tz        = pytz.timezone(TIMEZONE)
flask_app = Flask(__name__)

# ── Referencias globales (asignadas en main() antes de arrancar Flask) ─────────
# Nunca las uses antes de que main() las inicialice.
_main_loop:   asyncio.AbstractEventLoop | None = None
_application: Application | None               = None

# ── Envio de mensajes thread-safe ──────────────────────────────────────────────

def send_telegram(text: str) -> None:
    """Envia un mensaje a Telegram desde cualquier thread (Flask, APScheduler)."""
    if _main_loop is None or _application is None:
        logger.warning("Bot no inicializado, mensaje descartado: %.60s", text)
        return
    future = asyncio.run_coroutine_threadsafe(
        _application.bot.send_message(chat_id=CHAT_ID, text=text),
        _main_loop,
    )
    try:
        future.result(timeout=10)
    except Exception as exc:
        logger.error("Error send_telegram: %s", exc)

# ── Capa de datos ───────────────────────────────────────────────────────────────

def load_cuenta() -> dict:
    default = {
        "balance": 2000.0,
        "equity": 2000.0,
        "balance_maximo": 2000.0,
        "profit_hoy": 0.0,
        "profit_mes": 0.0,
        "trades_semana": [],
        "trades_mes": [],
        "ultima_actualizacion": "",
    }
    if os.path.exists(CUENTA_FILE):
        with open(CUENTA_FILE, "r", encoding="utf-8") as f:
            default.update(json.load(f))
    return default


def save_cuenta(data: dict) -> None:
    with open(CUENTA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_historial() -> list:
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_historial(data: list) -> None:
    with open(HISTORIAL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Logica de negocio ───────────────────────────────────────────────────────────

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def calcular_colchon(cuenta: dict) -> float:
    mll = cuenta["balance_maximo"] * (1 - MLL_TRAILING_PCT)
    return round(cuenta["balance"] - mll, 2)


def get_semaforo(colchon: float) -> str:
    if colchon >= 80:
        return "VERDE"
    if colchon >= 50:
        return "AMARILLO"
    return "ROJO"


_ultima_alerta_amarilla: datetime | None = None


def check_alertas(cuenta: dict) -> None:
    global _ultima_alerta_amarilla
    colchon = calcular_colchon(cuenta)
    ahora   = datetime.now(tz)

    if colchon < 20:
        send_telegram(
            "ALERTA CRITICA\n"
            f"Colchon: ${colchon:.2f} | Balance: ${cuenta['balance']:.2f}\n"
            "PARA EL EA INMEDIATAMENTE.\n"
            "El balance esta a punto de tocar el MLL."
        )
    elif colchon < 50:
        send_telegram(
            "ALERTA ROJA URGENTE\n"
            f"Colchon: ${colchon:.2f} | Balance: ${cuenta['balance']:.2f}\n"
            "Riesgo alto de tocar el MLL. Revisa ahora."
        )
    elif colchon < 80:
        puede = (
            _ultima_alerta_amarilla is None
            or (ahora - _ultima_alerta_amarilla).total_seconds() > 86400
        )
        if puede:
            _ultima_alerta_amarilla = ahora
            send_telegram(
                "ALERTA AMARILLA\n"
                f"Colchon: ${colchon:.2f} | Balance: ${cuenta['balance']:.2f}\n"
                "El colchon esta por debajo de $80. Monitorea de cerca."
            )


def check_meses_negativos() -> None:
    historial = load_historial()
    if len(historial) < 2:
        return
    negativos = 0
    for h in reversed(historial):
        if h.get("profit_mes", 0) <= 0:
            negativos += 1
        else:
            break
    if negativos >= 3:
        send_telegram(
            "AVISO PREVENTIVO\n"
            "3 meses negativos consecutivos detectados.\n"
            "Se recomienda pausar el EA y revisar la estrategia."
        )
    elif negativos == 2:
        send_telegram(
            "AVISO PREVENTIVO\n"
            "2 meses negativos consecutivos detectados.\n"
            "Monitorea de cerca el proximo mes."
        )

# ── Endpoints Flask ────────────────────────────────────────────────────────────

@flask_app.route("/balance", methods=["POST"])
def endpoint_balance():
    """Recibe datos de balance cada hora desde MT5. No envia mensaje; solo alerta si hay problemas."""
    data   = request.form
    cuenta = load_cuenta()

    cuenta["balance"]    = float(data.get("balance",    cuenta["balance"]))
    cuenta["equity"]     = float(data.get("equity",     cuenta["equity"]))
    cuenta["profit_hoy"] = float(data.get("profit_hoy", cuenta["profit_hoy"]))
    cuenta["profit_mes"] = float(data.get("profit_mes", cuenta["profit_mes"]))

    bal_max_nuevo = float(data.get("balance_maximo", cuenta["balance_maximo"]))
    if bal_max_nuevo > cuenta["balance_maximo"]:
        cuenta["balance_maximo"] = bal_max_nuevo

    cuenta["ultima_actualizacion"] = datetime.now(tz).isoformat()
    save_cuenta(cuenta)
    check_alertas(cuenta)
    return jsonify({"ok": True})


@flask_app.route("/trade_abierto", methods=["POST"])
def endpoint_trade_abierto():
    """Recibe apertura de trade desde MT5 y envia notificacion inmediata."""
    data   = request.form
    cuenta = load_cuenta()

    par     = data.get("par",    "XAUUSD")
    tipo    = data.get("tipo",   "BUY")
    lotes   = data.get("lotes",  "0.01")
    precio  = float(data.get("precio", 0))
    sl      = float(data.get("sl",     0))
    tp      = float(data.get("tp",     0))
    balance = float(data.get("balance", cuenta["balance"]))

    cuenta["balance"] = balance
    save_cuenta(cuenta)

    colchon  = calcular_colchon(cuenta)
    semaforo = get_semaforo(colchon)
    hora_lx  = datetime.now(tz).strftime("%H:%M")

    riesgo_sl   = RIESGO_POR_TRADE
    ganancia_tp = round(RIESGO_POR_TRADE * 1.5, 2)
    if precio > 0:
        if sl > 0:
            riesgo_sl   = round(abs(precio - sl) * float(lotes) * 100, 2)
        if tp > 0:
            ganancia_tp = round(abs(tp - precio) * float(lotes) * 100, 2)

    send_telegram(
        f"[!] TRADE ABIERTO\n"
        f"Par: {par} | Tipo: {tipo}\n"
        f"Lote: {lotes} | Precio: {precio:.2f}\n"
        f"SL: {sl:.2f} (-${riesgo_sl:.2f}) | TP: {tp:.2f} (+${ganancia_tp:.2f})\n"
        f"Hora: {hora_lx} Lisboa\n"
        f"Balance: ${balance:.2f} | Colchon: ${colchon:.2f} [{semaforo}]"
    )
    return jsonify({"ok": True})


@flask_app.route("/trade_cerrado", methods=["POST"])
def endpoint_trade_cerrado():
    """Recibe cierre de trade desde MT5 y envia notificacion inmediata."""
    data   = request.form
    cuenta = load_cuenta()

    par             = data.get("par",             "XAUUSD")
    resultado       = float(data.get("resultado",       0))
    precio_apertura = float(data.get("precio_apertura", 0))
    precio_cierre   = float(data.get("precio_cierre",   0))
    duracion_min    = int(float(data.get("duracion_min", 0)))
    balance_nuevo   = float(data.get("balance_nuevo",   cuenta["balance"]))

    cuenta["balance"] = balance_nuevo
    if balance_nuevo > cuenta["balance_maximo"]:
        cuenta["balance_maximo"] = balance_nuevo

    cuenta["profit_hoy"] = round(cuenta.get("profit_hoy", 0) + resultado, 2)
    cuenta["profit_mes"] = round(cuenta.get("profit_mes", 0) + resultado, 2)

    trade_info = {
        "par":       par,
        "resultado": resultado,
        "fecha":     datetime.now(tz).isoformat(),
    }
    cuenta.setdefault("trades_semana", []).append(trade_info)
    cuenta.setdefault("trades_mes",    []).append(trade_info)
    save_cuenta(cuenta)

    colchon  = calcular_colchon(cuenta)
    semaforo = get_semaforo(colchon)
    horas    = duracion_min // 60
    mins     = duracion_min % 60
    es_gan   = resultado > 0
    estado   = "[OK] TRADE CERRADO - GANADOR" if es_gan else "[X] TRADE CERRADO - PERDEDOR"
    res_str  = f"+${resultado:.2f}" if es_gan else f"-${abs(resultado):.2f}"
    acum     = cuenta["profit_hoy"]
    acum_str = f"+${acum:.2f}" if acum >= 0 else f"-${abs(acum):.2f}"

    send_telegram(
        f"{estado}\n"
        f"Par: {par} | Resultado: {res_str}\n"
        f"Apertura: {precio_apertura:.2f} -> Cierre: {precio_cierre:.2f} | Duracion: {horas}h {mins}min\n"
        f"Balance nuevo: ${balance_nuevo:.2f}\n"
        f"Colchon nuevo: ${colchon:.2f} [{semaforo}]\n"
        f"Acumulado hoy: {acum_str}"
    )
    check_alertas(cuenta)
    return jsonify({"ok": True})


@flask_app.route("/alerta", methods=["POST"])
def endpoint_alerta():
    """Recibe alerta manual desde MT5 cuando detecta peligro."""
    data    = request.form
    nivel   = data.get("nivel",   "amarillo")
    colchon = float(data.get("colchon", 0))
    balance = float(data.get("balance", 0))

    mensajes = {
        "critico": (
            "ALERTA CRITICA desde MT5\n"
            f"Balance: ${balance:.2f} | Colchon: ${colchon:.2f}\n"
            "PARA EL EA INMEDIATAMENTE."
        ),
        "rojo": (
            "ALERTA ROJA desde MT5\n"
            f"Balance: ${balance:.2f} | Colchon: ${colchon:.2f}\n"
            "Riesgo alto de tocar el MLL."
        ),
        "amarillo": (
            "ALERTA AMARILLA desde MT5\n"
            f"Balance: ${balance:.2f} | Colchon: ${colchon:.2f}\n"
            "Monitorea de cerca."
        ),
    }
    send_telegram(mensajes.get(nivel, mensajes["amarillo"]))
    return jsonify({"ok": True})


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": datetime.now(tz).isoformat()})

# ── Informes automaticos (llamados desde APScheduler, thread distinto) ─────────

def generar_informe_mensual() -> None:
    cuenta     = load_cuenta()
    ahora      = datetime.now(tz)
    mes_nombre = MESES_ES[ahora.month - 1]
    ano        = ahora.year

    balance    = cuenta["balance"]
    bal_max    = cuenta["balance_maximo"]
    mll        = round(bal_max * (1 - MLL_TRAILING_PCT), 2)
    colchon    = round(balance - mll, 2)
    profit_mes = cuenta.get("profit_mes", 0)

    retiro       = round(profit_mes * RETIRO_PCT, 2) if profit_mes > 0 else 0
    colchon_post = round(colchon - retiro, 2)
    queda        = round(balance - retiro, 2)

    if colchon_post >= 80 and profit_mes > 0:
        color, veredicto, puede_retirar = (
            "VERDE", "Mes positivo con colchon solido.", True
        )
        recomendacion = "Sigue con el plan actual.\nConsidera retirar el 40% de las ganancias."
    elif 50 <= colchon_post < 80 and profit_mes > 0:
        color, veredicto, puede_retirar = (
            "AMARILLO", "Mes positivo pero colchon ajustado.", True
        )
        recomendacion = "Retira con precaucion.\nMonitorea el colchon el proximo mes."
    else:
        color, veredicto, puede_retirar = (
            "ROJO", "Mes negativo o colchon insuficiente.", False
        )
        recomendacion = "No retires este mes.\nDeja que el balance se recupere primero."

    p_str   = f"+${profit_mes:.2f}" if profit_mes >= 0 else f"-${abs(profit_mes):.2f}"
    ret_txt = "[OK] Puedes retirar" if puede_retirar else "[X] NO retires este mes"

    send_telegram(
        f"--- INFORME {mes_nombre.upper()} {ano} ---\n"
        f"Balance:        ${balance:.2f}\n"
        f"MLL actual:     ${mll:.2f}  (= ${bal_max:.2f} x 0.94)\n"
        f"Colchon actual: ${colchon:.2f}\n"
        f"Resultado {mes_nombre}: {p_str}\n"
        f"\n[{color}] VEREDICTO: {veredicto}\n"
        f"{ret_txt}\n"
        f"Maximo a retirar:     ${retiro:.2f}\n"
        f"Se queda en cuenta:   ${queda:.2f}\n"
        f"Colchon tras retirar: ${colchon_post:.2f}\n"
        f"\nRECOMENDACION: {recomendacion}\n"
        f"[!] ALERTAS: Ninguna."
    )

    historial = load_historial()
    historial.append({
        "mes": mes_nombre, "ano": ano,
        "balance": balance, "profit_mes": profit_mes,
        "colchon": colchon, "retiro": retiro,
        "fecha": ahora.isoformat(),
    })
    save_historial(historial[-12:])
    check_meses_negativos()

    cuenta["profit_mes"] = 0.0
    cuenta["trades_mes"] = []
    save_cuenta(cuenta)


def generar_informe_semanal() -> None:
    cuenta     = load_cuenta()
    ahora      = datetime.now(tz)
    semana_num = ahora.isocalendar()[1]
    mes_nombre = MESES_ES[ahora.month - 1]

    lunes      = ahora - timedelta(days=ahora.weekday())
    inicio_str = lunes.strftime("%d/%m")
    fin_str    = ahora.strftime("%d/%m/%Y")

    balance  = cuenta["balance"]
    colchon  = calcular_colchon(cuenta)
    semaforo = get_semaforo(colchon)

    trades     = cuenta.get("trades_semana", [])
    total      = len(trades)
    ganadores  = [t for t in trades if t["resultado"] > 0]
    perdedores = [t for t in trades if t["resultado"] <= 0]
    profit_sem = sum(t["resultado"] for t in trades)
    profit_mes = cuenta.get("profit_mes", 0)
    pct        = (len(ganadores) / total * 100) if total > 0 else 0
    max_g      = max((t["resultado"] for t in ganadores),  default=0)
    max_p      = abs(min((t["resultado"] for t in perdedores), default=0))
    ps_str     = f"+${profit_sem:.2f}" if profit_sem >= 0 else f"-${abs(profit_sem):.2f}"
    pm_str     = f"+${profit_mes:.2f}" if profit_mes >= 0 else f"-${abs(profit_mes):.2f}"
    est_txt    = (
        "Colchon saludable." if colchon >= 80
        else "Colchon ajustado. Monitorea." if colchon >= 50
        else "Colchon bajo. Precaucion."
    )

    send_telegram(
        f"--- RESUMEN SEMANA {semana_num} {mes_nombre.upper()} ---\n"
        f"Del {inicio_str} al {fin_str}\n"
        f"Balance: ${balance:.2f} | Colchon: ${colchon:.2f}\n"
        f"Semana: {ps_str}\n"
        f"Acumulado {mes_nombre}: {pm_str}\n"
        f"Trades: {total} | Ganadores: {len(ganadores)} ({pct:.0f}%) | Perdedores: {len(perdedores)}\n"
        f"Mayor ganancia: +${max_g:.2f} | Mayor perdida: -${max_p:.2f}\n"
        f"Estado colchon: [{semaforo}] {est_txt}"
    )

    cuenta["trades_semana"] = []
    save_cuenta(cuenta)

# ── Comandos Telegram (coroutines — se ejecutan en el loop principal) ──────────

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cuenta  = load_cuenta()
    balance = cuenta["balance"]
    bal_max = cuenta["balance_maximo"]
    mll     = round(bal_max * (1 - MLL_TRAILING_PCT), 2)
    colchon = round(balance - mll, 2)
    await update.message.reply_text(
        f"ESTADO ACTUAL\n"
        f"Balance: ${balance:.2f}\n"
        f"MLL: ${mll:.2f}\n"
        f"Colchon: ${colchon:.2f}\n"
        f"Semaforo: [{get_semaforo(colchon)}]\n"
        f"Ultima actualizacion: {cuenta.get('ultima_actualizacion', 'N/A')}"
    )


async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hist = load_historial()
    if not hist:
        await update.message.reply_text("No hay historial disponible aun.")
        return
    msg = "ULTIMOS INFORMES MENSUALES\n\n"
    for h in reversed(hist[-6:]):
        p     = h["profit_mes"]
        p_str = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
        msg  += f"{h['mes']} {h['ano']}: ${h['balance']:.2f} | P/L: {p_str}\n"
    await update.message.reply_text(msg)


async def cmd_reglas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "LAS 5 REGLAS DE ORO\n\n"
        "1. NUNCA arriesgues mas de $15 por trade.\n"
        "2. Si el colchon cae bajo $50, PARA el EA.\n"
        "3. Retira solo el 40% de las ganancias mensuales.\n"
        "4. El MLL trailing es del 6% sobre el balance maximo historico.\n"
        "5. Con 3 meses negativos seguidos, pausa el EA y revisa la estrategia."
    )


async def cmd_actualizar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cuenta    = load_cuenta()
    args_text = " ".join(context.args)
    for part in args_text.split():
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                val = float(v.strip())
            except ValueError:
                continue
            k = k.strip().lower()
            if k == "balance":
                cuenta["balance"] = val
            elif k == "maximo":
                cuenta["balance_maximo"] = val
            elif k == "ganancia":
                cuenta["profit_mes"] = val
    cuenta["ultima_actualizacion"] = datetime.now(tz).isoformat()
    save_cuenta(cuenta)
    await update.message.reply_text("Datos actualizados manualmente.")

# ── Punto de entrada async ─────────────────────────────────────────────────────

async def main() -> None:
    global _main_loop, _application

    # 1. Capturar el loop de asyncio.run() — unica fuente de verdad
    _main_loop = asyncio.get_running_loop()

    # 2. Construir Application de PTB 21 y registrar handlers
    _application = Application.builder().token(BOT_TOKEN).build()
    _application.add_handler(CommandHandler("estado",     cmd_estado))
    _application.add_handler(CommandHandler("historial",  cmd_historial))
    _application.add_handler(CommandHandler("reglas",     cmd_reglas))
    _application.add_handler(CommandHandler("actualizar", cmd_actualizar))

    # 3. Inicializar el bot (resuelve el objeto Bot interno sin arrancar polling)
    await _application.initialize()
    logger.info("Bot inicializado: @%s", _application.bot.username)

    # 4. Arrancar Flask en un thread daemon (ahora _main_loop y _application estan listos)
    port = int(os.getenv("PORT", 5000))
    flask_thread = threading.Thread(
        target=flask_app.run,
        kwargs={"host": "0.0.0.0", "port": port, "use_reloader": False},
        daemon=True,
        name="flask",
    )
    flask_thread.start()
    logger.info("Flask escuchando en puerto %d", port)

    # 5. Arrancar APScheduler en su propio thread
    scheduler = BackgroundScheduler(timezone=tz)
    # Informe mensual: ultimo dia del mes a las 21:30 hora Lisboa
    scheduler.add_job(
        generar_informe_mensual,
        CronTrigger(day="last", hour=21, minute=30, timezone=tz),
        id="informe_mensual",
    )
    # Informe semanal: viernes a las 21:00 hora Lisboa
    scheduler.add_job(
        generar_informe_semanal,
        CronTrigger(day_of_week="fri", hour=21, minute=0, timezone=tz),
        id="informe_semanal",
    )
    scheduler.start()
    logger.info("APScheduler iniciado")

    # 6. Arrancar el polling de Telegram (dentro del loop principal)
    await _application.start()
    await _application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Polling Telegram activo — sistema completo en marcha")

    # 7. Mantener el loop vivo hasta Ctrl-C o SIGTERM
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()          # espera indefinida; cancela con CancelledError
    except asyncio.CancelledError:
        logger.info("Senal de apagado recibida")
    finally:
        logger.info("Apagando componentes...")
        scheduler.shutdown(wait=False)
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
        logger.info("Apagado completado")


if __name__ == "__main__":
    asyncio.run(main())
