import os
import json
import logging
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score, precision_score, recall_score
    import xgboost as xgb
    import lightgbm as lgb
    import joblib
except ImportError:
    logger = logging.getLogger(__name__)
    logger.warning("ML libraries not found. Run: pip install xgboost lightgbm scikit-learn joblib")

logger = logging.getLogger(__name__)

class ModelTrainer:
    """
    ML training framework supporting Cross Validation, Walk Forward Testing, and Model Registry.
    """
    def __init__(self, registry_path: str = "models/registry"):
        self.registry_path = registry_path
        os.makedirs(self.registry_path, exist_ok=True)

    def walk_forward_validation(self, X: pd.DataFrame, y: pd.Series, model, n_splits: int = 5) -> Dict[str, float]:
        """
        Time-series safe cross-validation (Walk-Forward).
        """
        tscv = TimeSeriesSplit(n_splits=n_splits)
        metrics = {"accuracy": [], "precision": [], "recall": []}

        for train_index, test_index in tscv.split(X):
            X_train, X_test = X.iloc[train_index], X.iloc[test_index]
            y_train, y_test = y.iloc[train_index], y.iloc[test_index]

            model.fit(X_train, y_train)
            preds = model.predict(X_test)

            metrics["accuracy"].append(accuracy_score(y_test, preds))
            metrics["precision"].append(precision_score(y_test, preds, zero_division=0))
            metrics["recall"].append(recall_score(y_test, preds, zero_division=0))

        return {k: float(np.mean(v)) for k, v in metrics.items()}

    def train_xgboost(self, X: pd.DataFrame, y: pd.Series) -> Tuple[Any, Dict[str, float]]:
        logger.info("Training XGBoost model...")
        model = xgb.XGBClassifier(n_estimators=100, learning_rate=0.05, max_depth=5, random_state=42)
        metrics = self.walk_forward_validation(X, y, model)
        
        # Train on full dataset after CV
        model.fit(X, y)
        logger.info(f"XGBoost training complete. CV Metrics: {metrics}")
        return model, metrics

    def train_lightgbm(self, X: pd.DataFrame, y: pd.Series) -> Tuple[Any, Dict[str, float]]:
        logger.info("Training LightGBM model...")
        model = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.05, max_depth=5, random_state=42)
        metrics = self.walk_forward_validation(X, y, model)
        
        model.fit(X, y)
        logger.info(f"LightGBM training complete. CV Metrics: {metrics}")
        return model, metrics

    def save_model(self, model: Any, model_name: str, metrics: Dict[str, float], features: list):
        """
        Model Registry: Saves model and metadata.
        """
        version = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_dir = os.path.join(self.registry_path, f"{model_name}_{version}")
        os.makedirs(model_dir, exist_ok=True)

        model_path = os.path.join(model_dir, "model.joblib")
        joblib.dump(model, model_path)

        metadata = {
            "model_name": model_name,
            "version": version,
            "metrics": metrics,
            "features": features,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        with open(os.path.join(model_dir, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=4)

        logger.info(f"Model {model_name} version {version} saved to registry.")
        return version

from datetime import datetime
