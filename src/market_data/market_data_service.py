import asyncio
import logging
from typing import List, Dict, Any
from datetime import datetime
import json
import websockets
from tenacity import retry, wait_exponential, stop_after_attempt

from src.database.repositories.base import BaseRepository
from src.database.models.ohlc import OHLCData

logger = logging.getLogger(__name__)

class MarketDataService:
    def __init__(
        self, 
        broker_client: Any, # Will be replaced with abstract broker
        ohlc_repository: BaseRepository[OHLCData],
        redis_client: Any = None
    ):
        self.broker = broker_client
        self.ohlc_repository = ohlc_repository
        self.redis_client = redis_client
        self.active_subscriptions: set = set()
        self._ws_connection = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def load_historical_data(self, symbol: str, from_date: datetime, to_date: datetime, timeframe: str = "1m"):
        """Download and persist historical data."""
        logger.info(f"Fetching historical data for {symbol} from {from_date} to {to_date}")
        
        try:
            # Assuming broker returns a list of dictionaries with open, high, low, close, volume
            historical_records = await self.broker.get_historical_data(
                symbol=symbol, 
                from_date=from_date, 
                to_date=to_date, 
                timeframe=timeframe
            )
            
            for record in historical_records:
                obj_in = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_timestamp": record["timestamp"],
                    "open": record["open"],
                    "high": record["high"],
                    "low": record["low"],
                    "close": record["close"],
                    "volume": record.get("volume", 0),
                    "open_interest": record.get("open_interest", 0)
                }
                await self.ohlc_repository.create(obj_in)
                
            logger.info(f"Successfully loaded {len(historical_records)} records for {symbol}")
            return len(historical_records)
            
        except Exception as e:
            logger.error(f"Failed to fetch historical data for {symbol}: {e}")
            raise

    async def subscribe_live_feed(self, symbols: List[str]):
        """Subscribe to live websocket feed."""
        for symbol in symbols:
            self.active_subscriptions.add(symbol)
        
        if self._ws_connection is None:
            asyncio.create_task(self._connect_websocket())
        else:
            await self._update_subscriptions()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _connect_websocket(self):
        """Connect to broker websocket with auto-reconnect."""
        ws_url = self.broker.get_websocket_url()
        logger.info(f"Connecting to WebSocket: {ws_url}")
        
        try:
            async with websockets.connect(ws_url) as ws:
                self._ws_connection = ws
                await self._update_subscriptions()
                
                async for message in ws:
                    await self._process_tick(message)
                    
        except Exception as e:
            logger.error(f"WebSocket connection dropped: {e}")
            self._ws_connection = None
            raise # Let tenacity handle the retry

    async def _update_subscriptions(self):
        if self._ws_connection and self.active_subscriptions:
            # Abstract broker specific logic here
            payload = self.broker.build_subscribe_payload(list(self.active_subscriptions))
            await self._ws_connection.send(json.dumps(payload))

    async def _process_tick(self, message: str):
        """Process incoming ticks and build OHLC data."""
        data = json.loads(message)
        # 1. Update latest price in Redis for fast access by risk/strategy engines
        if self.redis_client:
            await self.redis_client.set(f"tick:{data['symbol']}", json.dumps(data))
            
        # 2. Add to OHLC builder / aggregator
        await self.save_ticks(data)

    async def save_ticks(self, tick_data: Dict[str, Any]):
        """Persist raw ticks (optional, mostly for 30-day retention)."""
        pass # To be implemented based on actual raw tick storage needs

    async def save_ohlc(self, ohlc_data: Dict[str, Any]):
        """Persist aggregated OHLC candle."""
        await self.ohlc_repository.create(ohlc_data)