//+------------------------------------------------------------------+
//|                                        TelegramNotifier.mq5     |
//|   EA de solo lectura: envia datos de cuenta al bot Flask.        |
//|   NO abre ni cierra posiciones. Solo monitoriza y notifica.      |
//+------------------------------------------------------------------+
#property copyright "TradingBot XAUUSD H1"
#property version   "1.00"
#property strict

//--- Parametros editables desde MetaTrader 5
input string BOT_URL   = "https://pendiente.railway.app";
input string BOT_TOKEN = "8738002791:AAGDU8Jjxe-z8XsuPXNVF-EQhF16_XOfsPw";
input string CHAT_ID   = "8772999810";
input int    INTERVALO = 1;   // Intervalo en horas para enviar balance a Flask

//--- Variable global interna para el balance maximo historico
double g_balance_maximo = 0.0;

//--- Anti-duplicados: guardan el ultimo deal_ticket ya notificado
//    OnTradeTransaction se dispara varias veces por el mismo deal;
//    ignoramos el ticket si ya fue enviado.
ulong g_ultimo_ticket_abierto = 0;
ulong g_ultimo_ticket_cerrado = 0;

//+------------------------------------------------------------------+
//| Inicializacion: configura timer y carga balance maximo           |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Temporizador cada INTERVALO horas (en segundos)
   EventSetTimer(3600 * INTERVALO);

   //--- Recuperar balance maximo guardado entre sesiones de MT5
   if(!GlobalVariableGet("bal_max", g_balance_maximo) || g_balance_maximo <= 0.0)
   {
      g_balance_maximo = AccountInfoDouble(ACCOUNT_BALANCE);
      GlobalVariableSet("bal_max", g_balance_maximo);
   }

   Print("[TelegramNotifier] Iniciado | Balance maximo: ", DoubleToString(g_balance_maximo, 2));
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Desinicializacion: destruye el timer                             |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[TelegramNotifier] Detenido. Motivo: ", reason);
}

//+------------------------------------------------------------------+
//| Timer: envia datos de balance al bot cada INTERVALO horas        |
//+------------------------------------------------------------------+
void OnTimer()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   //--- Actualizar maximo historico si el balance actual lo supera
   if(balance > g_balance_maximo)
   {
      g_balance_maximo = balance;
      GlobalVariableSet("bal_max", g_balance_maximo);
   }

   double profit_hoy = CalcularProfitHoy();
   double profit_mes = CalcularProfitMes();

   //--- Hora del servidor MT5
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   string hora = StringFormat("%02d:%02d", dt.hour, dt.min);

   //--- Construir body del POST
   string body = StringFormat(
      "balance=%.2f&equity=%.2f&balance_maximo=%.2f&profit_hoy=%.2f&profit_mes=%.2f&hora=%s",
      balance, equity, g_balance_maximo, profit_hoy, profit_mes, hora
   );

   EnviarPost("/balance", body);
}

//+------------------------------------------------------------------+
//| OnTradeTransaction: detecta apertura y cierre de trades          |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{
   //--- Solo procesamos cuando se registra un nuevo deal en el historial
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;

   ulong deal_ticket = trans.deal;

   //--- El deal debe estar disponible en el historial
   if(!HistoryDealSelect(deal_ticket))
      return;

   long entry_type = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);

   // ===== TRADE ABIERTO (entrada nueva) =====
   if(entry_type == DEAL_ENTRY_IN)
   {
      if(deal_ticket == g_ultimo_ticket_abierto)
      {
         Print("[TelegramNotifier] Apertura duplicada ignorada | ticket: ", deal_ticket);
         return;
      }
      g_ultimo_ticket_abierto = deal_ticket;

      string symbol  = HistoryDealGetString(deal_ticket,  DEAL_SYMBOL);
      long   d_type  = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
      double lotes   = HistoryDealGetDouble(deal_ticket,  DEAL_VOLUME);
      double precio  = HistoryDealGetDouble(deal_ticket,  DEAL_PRICE);
      long   magic   = HistoryDealGetInteger(deal_ticket, DEAL_MAGIC);
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      string tipo    = (d_type == DEAL_TYPE_BUY) ? "BUY" : "SELL";

      //--- Buscar SL y TP en posiciones abiertas actuales
      double sl = 0.0, tp = 0.0;
      for(int i = 0; i < PositionsTotal(); i++)
      {
         ulong pos_ticket = PositionGetTicket(i);
         if(pos_ticket == 0) continue;
         if(!PositionSelectByTicket(pos_ticket)) continue;
         if(PositionGetString(POSITION_SYMBOL)  == symbol &&
            PositionGetInteger(POSITION_MAGIC)  == magic)
         {
            sl = PositionGetDouble(POSITION_SL);
            tp = PositionGetDouble(POSITION_TP);
            break;
         }
      }

      string body = StringFormat(
         "par=%s&tipo=%s&lotes=%.2f&precio=%.5f&sl=%.5f&tp=%.5f&balance=%.2f&magic=%d",
         symbol, tipo, lotes, precio, sl, tp, balance, (int)magic
      );
      EnviarPost("/trade_abierto", body);
   }

   // ===== TRADE CERRADO (salida de posicion) =====
   if(entry_type == DEAL_ENTRY_OUT || entry_type == DEAL_ENTRY_INOUT)
   {
      if(deal_ticket == g_ultimo_ticket_cerrado)
      {
         Print("[TelegramNotifier] Cierre duplicado ignorado | ticket: ", deal_ticket);
         return;
      }
      g_ultimo_ticket_cerrado = deal_ticket;

      string   symbol        = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
      double   resultado     = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT)
                             + HistoryDealGetDouble(deal_ticket, DEAL_SWAP)
                             + HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
      double   precio_cierre = HistoryDealGetDouble(deal_ticket,  DEAL_PRICE);
      double   balance_nuevo = AccountInfoDouble(ACCOUNT_BALANCE);
      datetime tiempo_cierre = (datetime)HistoryDealGetInteger(deal_ticket, DEAL_TIME);

      //--- Actualizar maximo al cerrar trade si corresponde
      if(balance_nuevo > g_balance_maximo)
      {
         g_balance_maximo = balance_nuevo;
         GlobalVariableSet("bal_max", g_balance_maximo);
      }

      //--- Buscar deal de apertura para precio inicial y duracion
      double   precio_apertura = 0.0;
      int      duracion_min    = 0;
      ulong    pos_id          = (ulong)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
      datetime tiempo_apertura = 0;

      if(HistorySelectByPosition(pos_id))
      {
         int n = HistoryDealsTotal();
         for(int i = 0; i < n; i++)
         {
            ulong t = HistoryDealGetTicket(i);
            if(t == 0) continue;
            if(HistoryDealGetInteger(t, DEAL_ENTRY) == DEAL_ENTRY_IN)
            {
               precio_apertura = HistoryDealGetDouble(t,  DEAL_PRICE);
               tiempo_apertura = (datetime)HistoryDealGetInteger(t, DEAL_TIME);
               break;
            }
         }
      }

      if(tiempo_apertura > 0)
         duracion_min = (int)((tiempo_cierre - tiempo_apertura) / 60);

      string body = StringFormat(
         "par=%s&resultado=%.2f&precio_apertura=%.5f&precio_cierre=%.5f&duracion_min=%d&balance_nuevo=%.2f",
         symbol, resultado, precio_apertura, precio_cierre, duracion_min, balance_nuevo
      );
      EnviarPost("/trade_cerrado", body);
   }
}

//+------------------------------------------------------------------+
//| Calcula el profit total de los trades cerrados hoy               |
//+------------------------------------------------------------------+
double CalcularProfitHoy()
{
   double profit = 0.0;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   datetime inicio_hoy = StructToTime(dt);

   if(!HistorySelect(inicio_hoy, TimeCurrent()))
      return 0.0;

   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT)
         profit += HistoryDealGetDouble(ticket, DEAL_PROFIT);
   }
   return profit;
}

//+------------------------------------------------------------------+
//| Calcula el profit total de los trades cerrados este mes          |
//+------------------------------------------------------------------+
double CalcularProfitMes()
{
   double profit = 0.0;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.day = 1; dt.hour = 0; dt.min = 0; dt.sec = 0;
   datetime inicio_mes = StructToTime(dt);

   if(!HistorySelect(inicio_mes, TimeCurrent()))
      return 0.0;

   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT)
      {
         profit += HistoryDealGetDouble(ticket, DEAL_PROFIT);
         profit += HistoryDealGetDouble(ticket, DEAL_SWAP);
         profit += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      }
   }
   return profit;
}

//+------------------------------------------------------------------+
//| Envia peticion HTTP POST al bot Flask                            |
//+------------------------------------------------------------------+
void EnviarPost(const string endpoint, const string body)
{
   string url     = BOT_URL + endpoint;
   string headers = "Content-Type: application/x-www-form-urlencoded\r\n";
   char   data[];
   char   response[];
   string resp_headers;

   //--- Convertir body a bytes (sin terminador nulo al final)
   int body_len = StringLen(body);
   ArrayResize(data, body_len);
   StringToCharArray(body, data, 0, body_len);

   //--- POST con timeout de 5000 ms
   int res = WebRequest(
      "POST",
      url,
      headers,
      5000,
      data,
      response,
      resp_headers
   );

   if(res == -1)
   {
      Print("[TelegramNotifier] ERROR WebRequest (", GetLastError(), ") -> ", url);
      Print("SOLUCION: Herramientas > Opciones > Asesores Expertos > Agregar URL: ", BOT_URL);
   }
   else
   {
      Print("[TelegramNotifier] OK POST ", endpoint, " | HTTP ", res);
   }
}
//+------------------------------------------------------------------+
