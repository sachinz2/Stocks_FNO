# VOLUME 4

# API CONTRACTS (OPENAPI SPECIFICATION)
Version: 1.0

Base URL: `/api/v1`
Authentication: JWT Token
Header: `Authorization: Bearer <token>`

---

# HEALTH API
`GET /health`
Response:
```json
{
"status":"UP",
"database":"UP",
"redis":"UP"
}
```

---

# STOCKS API
`GET /stocks`
Description: Returns all active F&O stocks.
Response:
```json
[
{
"symbol":"SBIN",
"sector":"BANKING",
"lot_size":750
}
]
```

`GET /stocks/{symbol}`
Response:
```json
{
"symbol":"SBIN",
"name":"State Bank of India",
"lot_size":750,
"active":true
}
```

---

# MARKET DATA API
`GET /market-data/{symbol}`
Parameters: symbol, timeframe, from_date, to_date
Example: `GET /market-data/SBIN?timeframe=5m`
Response:
```json
[
{
"timestamp":"2026-06-01T09:15:00",
"open":810.5,
"high":812.0,
"low":809.8,
"close":811.4,
"volume":15000
}
]
```

`POST /market-data/load`
Description: Load historical data.
Request:
```json
{
"symbol":"SBIN",
"from":"2023-01-01",
"to":"2026-01-01"
}
```
Response:
```json
{
"status":"accepted"
}
```

---

# INDICATORS API
`GET /indicators/{symbol}`
Response:
```json
{
"ema20":810.55,
"ema50":807.22,
"rsi14":61.3,
"atr14":11.8,
"vwap":809.4
}
```

---

# SIGNAL API
`GET /signals`
Response:
```json
[
{
"symbol":"SBIN",
"signal":"BUY",
"confidence":0.82
}
]
```

`POST /signals/generate`
Request:
```json
{
"strategy":"VWAP_REVERSION"
}
```
Response:
```json
{
"generated":23
}
```

---

# STRATEGY API
`GET /strategies`
Response:
```json
[
{
"name":"VWAP_REVERSION",
"active":true
}
]
```

`POST /strategies/activate`
```json
{
"strategy":"VWAP_REVERSION"
}
```

`POST /strategies/deactivate`
```json
{
"strategy":"VWAP_REVERSION"
}
```

---

# BACKTEST API
`POST /backtest/run`
Request:
```json
{
"strategy":"VWAP_REVERSION",
"symbol":"SBIN",
"start_date":"2023-01-01",
"end_date":"2026-01-01"
}
```
Response:
```json
{
"run_id":12345
}
```

`GET /backtest/{run_id}`
Response:
```json
{
"status":"COMPLETED",
"profit_factor":1.78,
"drawdown":8.2,
"win_rate":56.1
}
```

---

# PAPER TRADING API
`POST /paper-trading/start`
Request:
```json
{
"strategy":"VWAP_REVERSION"
}
```
Response:
```json
{
"status":"RUNNING"
}
```

`POST /paper-trading/stop`
Response:
```json
{
"status":"STOPPED"
}
```

---

# ORDER API
`POST /orders`
Request:
```json
{
"symbol":"SBIN",
"side":"BUY",
"quantity":750,
"price":810.50
}
```
Response:
```json
{
"order_id":"123456"
}
```

`GET /orders`
`GET /orders/{order_id}`
`DELETE /orders/{order_id}`

---

# POSITION API
`GET /positions`
Response:
```json
[
{
"symbol":"SBIN",
"quantity":750,
"avg_price":810.20,
"pnl":2350
}
]
```

---

# RISK API
`GET /risk/rules`
`POST /risk/rules`
`PUT /risk/rules/{id}`
`DELETE /risk/rules/{id}`

---

# REPORT API
`GET /reports/daily`
`GET /reports/monthly`
`GET /reports/strategy`

---

# ML API
`POST /ml/train`
`POST /ml/predict`
`GET /ml/models`
`GET /ml/features`

---

# HTTP Status Codes
* 200 Success
* 201 Created
* 400 Bad Request
* 401 Unauthorized
* 403 Forbidden
* 404 Not Found
* 500 Internal Error

---

# OpenAPI Requirement
Every endpoint must:
* Have Request DTO
* Have Response DTO
* Have Validation
* Have Swagger Documentation