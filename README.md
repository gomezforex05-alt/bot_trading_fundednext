# Bot Trading XAUUSD — FundedNext

Sistema autonomo de notificaciones para MT5 via Telegram.
Capital: $2,000 | MLL Trailing: 6% | Riesgo por trade: $15

---

## Archivos del proyecto

| Archivo | Descripcion |
|---|---|
| `trading_bot.py` | Bot principal: Flask + Telegram + APScheduler |
| `TelegramNotifier.mq5` | EA para MT5 (solo lectura, solo notifica) |
| `requirements.txt` | Dependencias Python |
| `cuenta.json` | Estado actual de la cuenta |
| `historial.json` | Historial de informes mensuales |
| `.env` | Variables de entorno (BOT_TOKEN, CHAT_ID) |

---

## PASO 1 — Despliegue en Railway

### 1.1 Subir el proyecto a GitHub

```bash
git init
git add trading_bot.py requirements.txt cuenta.json historial.json .env
git commit -m "bot trading inicial"
git remote add origin https://github.com/TU_USUARIO/bot_trading_fundednext.git
git push -u origin main
```

### 1.2 Crear proyecto en Railway

1. Ve a [railway.app](https://railway.app) y haz login.
2. Haz clic en **New Project > Deploy from GitHub repo**.
3. Selecciona el repositorio `bot_trading_fundednext`.
4. Railway detecta Python automaticamente.

### 1.3 Configurar variables de entorno en Railway

En el panel del proyecto: **Variables > Add Variable**

```
BOT_TOKEN   = 8738002791:AAGDU8Jjxe-z8XsuPXNVF-EQhF16_XOfsPw
CHAT_ID     = 8772999810
PORT        = 5000
```

### 1.4 Configurar el comando de arranque

En **Settings > Deploy > Start Command**:

```
python trading_bot.py
```

### 1.5 Obtener la URL publica de Railway

Despues del primer deploy, Railway genera una URL tipo:
`https://bot-trading-fundednext-production.up.railway.app`

**Copia esa URL** — la necesitaras en el EA de MT5.

### 1.6 Persistencia de archivos JSON en Railway

Railway no tiene disco persistente por defecto en el plan gratuito.
Para persistir `cuenta.json` e `historial.json` entre reinicios:

**Opcion A (recomendada):** Upgrade a Railway Hobby ($5/mes) y usa Volumes.
- En el panel: **Add Volume > Mount Path: /app**
- Cambia en `trading_bot.py`: `CUENTA_FILE = "/app/cuenta.json"` y `HISTORIAL_FILE = "/app/historial.json"`

**Opcion B (gratuita):** Usa un servicio externo como JSONBin.io o una base de datos Railway (PostgreSQL).

---

## PASO 2 — Configurar el EA en MetaTrader 5

### 2.1 Copiar el EA a MetaTrader 5

1. Abre **MetaEditor** desde MT5 (F4 o menu Herramientas > MetaEditor).
2. En MetaEditor: **Archivo > Abrir** y selecciona `TelegramNotifier.mq5`.
   - O copia el archivo a: `C:\Users\TU_USUARIO\AppData\Roaming\MetaQuotes\Terminal\<ID>\MQL5\Experts\`
3. Presiona **F7** para compilar. Debe mostrar: `0 errores, 0 advertencias`.

### 2.2 Actualizar la URL del bot en el EA

Antes de compilar, edita el parametro `BOT_URL` con la URL de Railway:

```mql5
input string BOT_URL = "https://bot-trading-fundednext-production.up.railway.app";
```

### 2.3 Permitir WebRequest en MT5

**OBLIGATORIO** — sin esto el EA no puede conectar con el bot:

1. En MT5: **Herramientas > Opciones > Asesores Expertos**
2. Marca: `Permitir solicitudes WebRequest para las siguientes URL`
3. Agrega la URL de Railway: `https://bot-trading-fundednext-production.up.railway.app`
4. Haz clic en **Aceptar**.

### 2.4 Adjuntar el EA al grafico

1. En MT5, abre el grafico de **XAUUSD H1**.
2. En el Navegador (panel izquierdo): **Asesores Expertos > TelegramNotifier**.
3. Arrastra el EA sobre el grafico XAUUSD H1.
4. En la ventana de configuracion:
   - `BOT_URL`: URL de Railway
   - `BOT_TOKEN`: el token del bot
   - `CHAT_ID`: tu chat ID
   - `INTERVALO`: 1 (envia balance cada hora)
5. Marca: **Permitir trading** y **Permitir solicitudes DLL** (aunque este EA no las usa).
6. Haz clic en **Aceptar**.

El EA aparecera en la esquina superior derecha del grafico.

---

## PASO 3 — Verificar que todo funciona

### 3.1 Test del endpoint de salud

Abre en el navegador:
```
https://TU-URL.railway.app/health
```
Debe devolver: `{"status": "ok", "ts": "2025-..."}`

### 3.2 Test manual con curl (opcional)

```bash
# Simular envio de balance desde MT5
curl -X POST https://TU-URL.railway.app/balance \
  -d "balance=2000&equity=2000&balance_maximo=2000&profit_hoy=0&profit_mes=0&hora=10:00"

# Simular trade abierto
curl -X POST https://TU-URL.railway.app/trade_abierto \
  -d "par=XAUUSD&tipo=BUY&lotes=0.01&precio=2350.50&sl=2340.00&tp=2365.00&balance=2000"

# Simular trade cerrado con ganancia
curl -X POST https://TU-URL.railway.app/trade_cerrado \
  -d "par=XAUUSD&resultado=15.00&precio_apertura=2350.50&precio_cierre=2365.00&duracion_min=90&balance_nuevo=2015.00"
```

### 3.3 Comandos en Telegram

Escribe al bot desde Telegram:

| Comando | Descripcion |
|---|---|
| `/estado` | Balance, MLL, colchon y semaforo actuales |
| `/historial` | Ultimos 6 informes mensuales |
| `/reglas` | Las 5 reglas de oro del sistema |
| `/actualizar balance=2050 maximo=2050 ganancia=50` | Actualizacion manual de datos |

---

## Logica del sistema

### MLL Trailing (6%)
```
MLL = balance_maximo x 0.94
Colchon = balance_actual - MLL
```
Si el balance sube, el MLL sube. Nunca baja.

### Semaforo del colchon
| Estado | Colchon | Accion |
|---|---|---|
| VERDE | >= $80 | Todo OK |
| AMARILLO | $50 - $79 | Monitorear |
| ROJO | < $50 | Parar EA |
| CRITICO | < $20 | Parar EA INMEDIATAMENTE |

### Regla de retiro mensual
```
Retiro maximo = profit_mes x 40%  (solo si profit_mes > 0)
Colchon post-retiro debe quedar >= $50
```

### Informes automaticos
- **Semanal**: Viernes 21:00h Lisboa
- **Mensual**: Ultimo dia del mes 21:30h Lisboa

---

## Estructura de datos

### cuenta.json
```json
{
  "balance": 2000.0,
  "equity": 2000.0,
  "balance_maximo": 2000.0,
  "profit_hoy": 0.0,
  "profit_mes": 0.0,
  "trades_semana": [],
  "trades_mes": [],
  "ultima_actualizacion": ""
}
```

### historial.json
Lista de objetos con el resumen de cada mes cerrado (maximo 12 meses).

---

## Solucion de problemas

**El EA no envia datos:**
- Verifica que `BOT_URL` en el EA coincide exactamente con la URL de Railway.
- Verifica que la URL esta en la lista de URLs permitidas en MT5.
- Revisa el Journal de MT5 para ver errores del EA.

**El bot no responde en Telegram:**
- Verifica que `BOT_TOKEN` y `CHAT_ID` son correctos.
- Comprueba los logs de Railway (panel > Deployments > View Logs).

**Error de compilacion en MetaEditor:**
- Asegurate de compilar con MetaEditor de MT5, no MT4.
- La funcion `WebRequest` requiere que el EA tenga permiso de red.

**Los JSON se pierden al reiniciar Railway:**
- Esto es normal en el plan gratuito (sin disco persistente).
- Usa `/actualizar` en Telegram para restablecer los valores despues de un reinicio.
- O configura un Volume en Railway (plan Hobby).
