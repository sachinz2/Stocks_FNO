"""
PortfolioAnalyzer's high_beta_alert reachability.

Fixed 2026-08-06: PORTFOLIO_BETA_LIMIT was 5.0, but portfolio_beta is a
notional-weighted AVERAGE of SYMBOL_BETAS (weighted_beta / total_notional),
which by definition can never exceed the single highest beta in the
portfolio -- the max value anywhere in SYMBOL_BETAS is ~1.40. A threshold
of 5.0 could never be reached at any concentration level, so the alert was
structurally dead code.
"""
from src.risk.portfolio_analyzer import PortfolioAnalyzer, SYMBOL_BETAS, PORTFOLIO_BETA_LIMIT


def test_max_possible_beta_exceeds_the_threshold():
    max_beta = max(SYMBOL_BETAS.values())
    assert max_beta < 5.0, "sanity: confirms the OLD threshold really was unreachable"
    assert max_beta > PORTFOLIO_BETA_LIMIT, (
        f"max possible beta ({max_beta}) must exceed the threshold ({PORTFOLIO_BETA_LIMIT}) "
        "for the alert to ever be reachable"
    )


def test_high_beta_concentrated_book_triggers_alert():
    analyzer = PortfolioAnalyzer()
    positions = [
        {"symbol": "SBIN26AUG800PE", "quantity": -100, "avg_price": 20.0},       # SBIN beta 1.30
        {"symbol": "BAJFINANCE26AUG7000CE", "quantity": -50, "avg_price": 80.0},  # BAJFINANCE beta 1.35
    ]
    report = analyzer.get_report(positions)
    assert report["beta_exposure"]["high_beta_alert"] is True


def test_diversified_low_beta_book_does_not_trigger_alert():
    analyzer = PortfolioAnalyzer()
    positions = [
        {"symbol": "NESTLEIND26AUG25000PE", "quantity": -10, "avg_price": 50.0},  # beta 0.55
        {"symbol": "HINDUNILVR26AUG2500PE", "quantity": -20, "avg_price": 30.0},  # beta 0.60
    ]
    report = analyzer.get_report(positions)
    assert report["beta_exposure"]["high_beta_alert"] is False
