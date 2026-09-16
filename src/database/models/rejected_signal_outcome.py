from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Float, Integer, Boolean, Index, text
from src.database.base import Base


class RejectedSignalOutcome(Base):
    """
    Counterfactual tracking for a REJECTED signal_decision_trace row -- what
    did the underlying's price actually do afterward? Added 2026-09-16
    ("Trade Quality Layer" v1 of 3, external review round 2 Part 2).

    A row is written the moment _process_signal() rejects a real BUY/SELL
    candidate (see LiveTradingEngine._maybe_record_rejected_outcome(), called
    from _record_signal_trace()'s REJECTED branch), recording the underlying
    close at rejection time. LiveTradingEngine._backfill_rejected_outcomes()
    (scheduled every 5 min, see src/core/scheduler.py) then scans rows where
    outcome_complete is False and fills in whichever of close_5m/15m/30m/60m
    is now due, using the same live tick cache _get_market_data() itself
    reads (no new data dependency).

    Purpose (per the PDF): build an evidence base for whether a given gate's
    rejections are actually costing real, profitable trades -- e.g.
    discovering "MTF opposition SELL trades actually work 58% of the time" --
    BEFORE tuning any threshold based on a guess. Purely observational; does
    not feed back into any live trading decision by itself.
    """
    __tablename__ = "rejected_signal_outcome"
    __table_args__ = (
        Index("idx_rso_pending", "outcome_complete", "timestamp"),
        Index("idx_rso_strategy_gate", "strategy_name", "rejected_at_gate"),
    )

    id                  = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp           = Column(TIMESTAMP, nullable=False)
    strategy_name       = Column(String(50), nullable=False)
    symbol              = Column(String(30), nullable=False)
    signal              = Column(String(10), nullable=False)   # BUY / SELL
    rejected_at_gate    = Column(String(50), nullable=True)
    quality_score       = Column(Integer, nullable=True)
    close_at_rejection  = Column(Float, nullable=False)
    close_5m            = Column(Float, nullable=True)
    close_15m           = Column(Float, nullable=True)
    close_30m           = Column(Float, nullable=True)
    close_60m           = Column(Float, nullable=True)
    # False until all four snapshots are filled OR the row has aged past the
    # 60m window without a live tick ever being available (gives up rather
    # than being scanned forever -- see _backfill_rejected_outcomes()).
    outcome_complete    = Column(Boolean, nullable=False, server_default=text("0"))
