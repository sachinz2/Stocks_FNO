class ApplicationException(Exception):
    pass


class ConfigurationError(ApplicationException):
    pass


class BrokerException(ApplicationException):
    pass


class AuthenticationError(BrokerException):
    pass


class OrderException(ApplicationException):
    pass


class InsufficientFundsError(OrderException):
    pass


class RiskViolationError(ApplicationException):
    pass


class KillSwitchError(RiskViolationError):
    pass


class DataException(ApplicationException):
    pass


class StrategyException(ApplicationException):
    pass


class SchedulerException(ApplicationException):
    pass
