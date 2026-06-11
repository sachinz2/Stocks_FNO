from abc import ABC, abstractmethod

class AbstractBroker(ABC):
    @abstractmethod
    def place_order(self, symbol: str, side: str, quantity: int, price: float):
        pass

    @abstractmethod
    def cancel_order(self, order_id: str):
        pass

    @abstractmethod
    def modify_order(self, order_id: str, new_price: float, new_quantity: int):
        pass

    @abstractmethod
    def get_positions(self):
        pass

    @abstractmethod
    def get_orders(self):
        pass
