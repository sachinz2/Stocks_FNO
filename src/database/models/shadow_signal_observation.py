from sqlalchemy import Column, BigInteger, Integer, String, TIMESTAMP, Index
from src.database.base import Base


class ShadowSignalObservation(Base):
    """
    Records a real BUY/SELL signal from a regime-paused, shadow-eligible
    strategy's generate_signal() on a symbol currently holding a sustained
    RS-rank streak -- i.e. "this WOULD have been a real candidate if the
    regime gate weren't excluding this strategy right now."

    Added 2026-09-18 (user-authorized: "safest place to loosen the gates" ->
    observe-only trial before any real capital risk). The regime gate
    excludes ema_crossover_v1/momentum_v1 whenever the NIFTY INDEX isn't
    trending -- structurally blind to a genuine single-stock trend like
    PAYTM's real Sep-2026 run (RS rank #1 in ~100% of cycles for 7 straight
    trading days, confirmed live). A backtest against PAYTM's real
    historical bars showed ema_crossover_v1 would have traded it profitably
    (18 trades, 50% win rate) -- this table is the LIVE validation of that
    backtest, using real current market data instead of replayed history,
    before ever letting the strategy place a real order outside its normal
    regime eligibility.

    Deliberately minimal: recorded the moment generate_signal() fires,
    BEFORE the is_active check in LiveTradingEngine._process_signal() --
    see _maybe_record_shadow_candidate()'s docstring for why this stops
    here rather than replicating the full downstream gate pipeline (RVOL/
    RS/MTF/lot size/contract resolution/option quality). No order is ever
    placed, no capital is ever reserved, and no state-mutating engine method
    (e.g. _close_option_positions(), a real reversal-exit order call reached
    later in the real pipeline) is ever touched by this path.
    """
    __tablename__ = "shadow_signal_observation"
    __table_args__ = (
        Index("idx_sso_strategy_symbol_time", "strategy_name", "symbol", "timestamp"),
    )

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp       = Column(TIMESTAMP, nullable=False)
    strategy_name   = Column(String(50), nullable=False)
    symbol          = Column(String(30), nullable=False)
    signal          = Column(String(10), nullable=False)   # BUY / SELL
    regime          = Column(String(20), nullable=True)
    rs_streak_days  = Column(Integer, nullable=False)
    rs_streak_side  = Column(String(10), nullable=False)    # top / bottom
