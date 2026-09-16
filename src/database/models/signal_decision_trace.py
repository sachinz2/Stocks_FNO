from sqlalchemy import Column, BigInteger, Integer, String, TIMESTAMP, Index
from src.database.base import Base


class SignalDecisionTrace(Base):
    """
    Per-candidate diagnostic record -- WHY a specific (strategy, symbol) pair
    entered a trade or got rejected THIS cycle, distinct from
    GateAuditSnapshot's cumulative per-strategy pass counts (which answer
    "how many candidates reached gate X today" but not "what happened to
    THIS symbol, and what was the last gate it reached").

    Added 2026-09-15 (external review, "SignalDecisionTrace" recommendation):
    derived by diffing LiveTradingEngine._signal_gate_stats[strategy] before
    and after each _process_signal() call -- NOT by threading a trace object
    through _process_signal's internals, which would have meant touching
    every one of its ~12 early-return gate checks in a large, already
    carefully-hardened live entry-gate function. Since each strategy's gate
    pipeline is strictly linear (a candidate can only reach gate N after
    passing gate N-1 -- see LiveTradingEngine._CANONICAL_GATE_ORDER), the set
    of gates whose count incremented during one call IS that candidate's
    passed-gate prefix, with zero risk of altering which trades get taken.

    Trade-off: the diff alone gives the STAGE a candidate died at (e.g.
    "rvol_passed" never incremented -> died at/before RVOL), not the
    specific numeric reason. Fixed 2026-09-16 (external review round 2,
    "make SignalDecisionTrace more granular"): for the four gates with a
    real value-vs-threshold comparison (rvol_passed/adx_passed/rs_passed/
    mtf_passed), the check itself now sets
    LiveTradingEngine._last_gate_rejection {gate, value, threshold, reason}
    immediately before its own `return` (additive only -- no condition or
    control flow changed), which _record_signal_trace() folds into `detail`
    -- e.g. "MTF_STRONG_OPPOSITION value=0.42 threshold=0.3". DTE/lot/
    contract/margin rejections still stay stage-only; that detail still
    requires grepping the adjacent log line. final_decision is one of
    NO_SIGNAL (strategy didn't act -- HOLD, inactive, or missing data),
    REJECTED (a real BUY/SELL/spread/condor signal died at rejected_at_gate),
    ENTERED, or ERROR (an exception during processing).
    """
    __tablename__ = "signal_decision_trace"
    __table_args__ = (
        Index("idx_sdt_strategy_symbol_time", "strategy_name", "symbol", "timestamp"),
    )

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp        = Column(TIMESTAMP, nullable=False)
    strategy_name    = Column(String(50), nullable=False)
    symbol           = Column(String(30), nullable=False)
    regime           = Column(String(20), nullable=True)
    final_decision   = Column(String(20), nullable=False)   # NO_SIGNAL / REJECTED / ENTERED / ERROR
    last_gate_reached = Column(String(50), nullable=True)   # None for NO_SIGNAL
    detail           = Column(String(255), nullable=True)   # exception/rejection detail, else None
    # Added 2026-09-16 ("Trade Quality Layer" v1, external review round 2
    # Part 2): 0-100 diagnostic score, only populated for REJECTED/ENTERED
    # (a real BUY/SELL candidate). See LiveTradingEngine.
    # _compute_trade_quality_score() for the component breakdown -- purely
    # observational, does NOT gate any trade.
    quality_score    = Column(Integer, nullable=True)
