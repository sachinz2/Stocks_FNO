import pytest
import pandas as pd
import numpy as np
from src.ml.feature_store import FeatureStore

@pytest.fixture
def sample_ohlcv():
    dates = pd.date_range("2026-01-01", periods=10, freq="D")
    data = {
        "open": np.random.uniform(100, 110, 10),
        "high": np.random.uniform(110, 120, 10),
        "low": np.random.uniform(90, 100, 10),
        "close": np.random.uniform(95, 115, 10),
        "volume": np.random.uniform(1000, 5000, 10),
        "open_interest": np.random.uniform(500, 1000, 10),
        "vwap": np.random.uniform(100, 110, 10),
        "atr14": np.random.uniform(2, 5, 10),
        "rsi14": np.random.uniform(30, 70, 10)
    }
    return pd.DataFrame(data, index=dates)

def test_generate_features(sample_ohlcv):
    features = FeatureStore.generate_features(sample_ohlcv)
    
    assert not features.empty
    assert "returns" in features.columns
    assert "vwap_distance" in features.columns
    assert "target_class" in features.columns
    
    # Check that there are no NaNs in the final generated set
    assert features.isna().sum().sum() == 0

def test_validate_features(sample_ohlcv):
    features = FeatureStore.generate_features(sample_ohlcv)
    
    # Remove target columns to match purely expected input features
    features_input = features.drop(columns=["target_return", "target_class"])
    
    is_valid = FeatureStore.validate_features(features_input)
    assert is_valid is True

    # Intentionally corrupt a feature
    features_input.drop(columns=["returns"], inplace=True)
    is_valid = FeatureStore.validate_features(features_input)
    assert is_valid is False
