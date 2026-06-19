from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Integer, Float, Index, text
from src.database.base import Base


class TradeJournal(Base):
    """
    Detailed log of every closed trade/structure.
    Includes regime context at entry so we can measure what's working.
    Analytics endpoint reads from this table.
    """
    __tablename__ = "trade_journal"
    __table_args__ = (
        Index("idx_tj_strategy",   "strategy_name"),
        Index("idx_tj_symbol",     "underlying"),
        Index("idx_tj_entry_time", "entry_time"),
        Index("idx_tj_structure",  "structure_type"),
    )

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name   = Column(String(50),  nullable=False)
    underlying      = Column(String(30),  nullable=False)
    structure_type  = Column(String(30),  nullable=False)  # SINGLE_LEG / BULL_PUT_SPREAD / BEAR_CALL_SPREAD / IRON_CONDOR
    contracts       = Column(String(500), nullable=True)   # JSON list of contract symbols

    # Entry context
    entry_time      = Column(TIMESTAMP,  nullable=False)
    entry_price     = Column(Numeric(18, 4))               # net credit or debit
    quantity        = Column(Integer,    nullable=False)
    regime_atr_pct  = Column(Float,      nullable=True)    # ATR% at entry
    ema_spread_pct  = Column(Float,      nullable=True)    # EMA spread% at entry
    iv_rank         = Column(Float,      nullable=True)    # 0–1, None if unknown
    vix_at_entry    = Column(Float,      nullable=True)    # India VIX at entry

    # Entry timing (for day-of-week / hour-of-day analytics)
    day_of_week     = Column(Integer,    nullable=True)    # 0=Monday … 4=Friday
    hour_of_day     = Column(Integer,    nullable=True)    # 9–15 IST

    # Exit context
    exit_time           = Column(TIMESTAMP,  nullable=True)
    exit_price          = Column(Numeric(18, 4), nullable=True)
    exit_reason         = Column(String(200), nullable=True)
    pnl                 = Column(Numeric(18, 4), nullable=True)
    hold_days           = Column(Integer,    nullable=True)
    atr_at_exit         = Column(Float,      nullable=True)   # ATR14 at close time
    vix_at_exit         = Column(Float,      nullable=True)   # India VIX at close time
    regime_label        = Column(String(30), nullable=True)   # TRENDING / RANGE_BOUND / VOLATILE
    total_slippage_pts  = Column(Float,      nullable=True)   # sum |slippage| across all legs × lot
    slippage            = Column(Float,      nullable=True)   # legacy alias (kept for compatibility)

    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"))
