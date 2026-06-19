from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class StrategyBase(ABC):
    def __init__(self, name: str, parameters: Dict[str, Any] = None):
        self.name = name
        self.parameters = parameters or {}
        self.is_active = False

    @abstractmethod
    def initialize(self):
        """Initialize strategy state and indicators."""
        pass

    @abstractmethod
    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """Evaluate data and generate a signal (BUY, SELL, EXIT, HOLD)."""
        pass

    @abstractmethod
    def manage_position(self, current_position: Dict[str, Any], current_price: float) -> Optional[str]:
        """Manage open positions (e.g., trailing stop loss, take profit)."""
        pass

    @abstractmethod
    def shutdown(self):
        """Cleanup resources on shutdown."""
        pass

class StrategyRegistry:
    """Registry for dynamic strategy loading and management."""
    _strategies: Dict[str, type] = {}
    _active_instances: Dict[str, StrategyBase] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a strategy class."""
        def wrapper(strategy_class):
            cls._strategies[name] = strategy_class
            logger.info(f"Registered strategy plugin: {name}")
            return strategy_class
        return wrapper

    @classmethod
    def get_strategy_class(cls, name: str) -> Optional[type]:
        return cls._strategies.get(name)

    @classmethod
    def load_strategy(cls, name: str, instance_id: str, parameters: Dict[str, Any] = None) -> StrategyBase:
        strategy_class = cls.get_strategy_class(name)
        if not strategy_class:
            raise ValueError(f"Strategy {name} not found in registry.")
        
        instance = strategy_class(instance_id, parameters)
        cls._active_instances[instance_id] = instance
        instance.initialize()
        instance.is_active = True
        return instance

    @classmethod
    def unload_strategy(cls, instance_id: str):
        instance = cls._active_instances.pop(instance_id, None)
        if instance:
            instance.is_active = False
            instance.shutdown()
            logger.info(f"Unloaded strategy instance: {instance_id}")

    @classmethod
    def get_active_strategies(cls) -> Dict[str, StrategyBase]:
        return cls._active_instances

    @classmethod
    def pause_strategy(cls, instance_id: str) -> bool:
        """Disable a running strategy (blocks new entries; exits still run)."""
        instance = cls._active_instances.get(instance_id)
        if not instance:
            logger.warning(f"pause_strategy: {instance_id} not found")
            return False
        instance.is_active = False
        logger.warning(f"Strategy PAUSED by monitor: {instance_id}")
        return True

    @classmethod
    def resume_strategy(cls, instance_id: str) -> bool:
        """Re-enable a previously paused strategy."""
        instance = cls._active_instances.get(instance_id)
        if not instance:
            logger.warning(f"resume_strategy: {instance_id} not found")
            return False
        instance.is_active = True
        logger.info(f"Strategy RESUMED: {instance_id}")
        return True