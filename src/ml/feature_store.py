import pandas as pd
import numpy as np
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class FeatureStore:
    """
    Feature Store for ML models. 
    Handles generation, versioning, and validation of features.
    """
    VERSION = "1.0.0"
    
    EXPECTED_COLUMNS = [
        "returns", "volume_delta", "atr_14", "rsi_14", "vwap_distance", "oi_change"
    ]

    @classmethod
    def generate_features(cls, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates ML features from raw OHLCV + basic indicator data.
        Assumes df contains: open, high, low, close, volume, open_interest, vwap, atr14, rsi14
        """
        logger.info(f"Generating features (Version {cls.VERSION})")
        features = pd.DataFrame(index=df.index)

        # 1. Returns (Log returns for stationarity)
        features['returns'] = np.log(df['close'] / df['close'].shift(1))

        # 2. Volume Delta
        features['volume_delta'] = df['volume'].pct_change()

        # 3. Indicator pass-throughs or derivatives
        features['atr_14'] = df.get('atr14', np.nan)
        features['rsi_14'] = df.get('rsi14', np.nan)

        # 4. VWAP Distance (Percentage distance from VWAP)
        if 'vwap' in df.columns:
            features['vwap_distance'] = (df['close'] - df['vwap']) / df['vwap']
        else:
            features['vwap_distance'] = np.nan

        # 5. Open Interest Change
        if 'open_interest' in df.columns:
            features['oi_change'] = df['open_interest'].pct_change()
        else:
            features['oi_change'] = 0.0

        # Target Variable (e.g., Next period return for supervised learning)
        features['target_return'] = features['returns'].shift(-1)
        
        # Classification Target: 1 if return > 0, else 0
        features['target_class'] = (features['target_return'] > 0).astype(int)

        # Drop NaN rows caused by shifts/rolling
        features = features.dropna()
        return features

    @classmethod
    def validate_features(cls, features: pd.DataFrame) -> bool:
        """
        Validates that all expected features are present and contain no NaNs.
        """
        missing_cols = [col for col in cls.EXPECTED_COLUMNS if col not in features.columns]
        if missing_cols:
            logger.error(f"Feature validation failed. Missing columns: {missing_cols}")
            return False

        nan_counts = features[cls.EXPECTED_COLUMNS].isna().sum()
        if nan_counts.sum() > 0:
            logger.error(f"Feature validation failed. NaNs found:\n{nan_counts[nan_counts > 0]}")
            return False

        logger.info("Feature validation passed.")
        return True