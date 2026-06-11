import os
import json
import logging
import pandas as pd
from typing import Dict, Any, Optional

try:
    import joblib
except ImportError:
    pass

logger = logging.getLogger(__name__)

class PredictionService:
    """
    Prediction service for serving ML model inferences.
    """
    def __init__(self, registry_path: str = "models/registry"):
        self.registry_path = registry_path
        self.model = None
        self.metadata = None
        self.load_latest_model()

    def load_latest_model(self, model_prefix: str = "xgboost"):
        """Loads the most recently trained model from the registry."""
        if not os.path.exists(self.registry_path):
            logger.warning("Registry path does not exist. No model loaded.")
            return

        dirs = [d for d in os.listdir(self.registry_path) if d.startswith(model_prefix)]
        if not dirs:
            logger.warning(f"No models found with prefix {model_prefix}")
            return

        # Sort by version timestamp
        latest_dir = sorted(dirs)[-1]
        model_dir = os.path.join(self.registry_path, latest_dir)

        try:
            self.model = joblib.load(os.path.join(model_dir, "model.joblib"))
            with open(os.path.join(model_dir, "metadata.json"), "r") as f:
                self.metadata = json.load(f)
            logger.info(f"Successfully loaded model: {self.metadata['model_name']} v{self.metadata['version']}")
        except Exception as e:
            logger.error(f"Failed to load model from {model_dir}: {e}")

    def predict(self, features: pd.DataFrame) -> Optional[int]:
        """
        Generates a prediction for a single row or batch of features.
        Returns 1 (BUY expected) or 0 (SELL/HOLD expected).
        """
        if self.model is None or self.metadata is None:
            logger.error("Prediction failed: No model loaded.")
            return None

        expected_features = self.metadata.get("features", [])
        
        # Ensure we only pass the exact features the model was trained on
        try:
            X = features[expected_features]
            # Get prediction for the last row
            prediction = self.model.predict(X.iloc[[-1]])
            return int(prediction[0])
        except KeyError as e:
            logger.error(f"Missing required features for prediction: {e}")
            return None
        except Exception as e:
            logger.error(f"Error during prediction: {e}")
            return None
