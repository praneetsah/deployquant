"""The `AlgorithmImports` star-import module LEAN code expects.

`from AlgorithmImports import *` is the first line of every LEAN algorithm;
register_algorithm_imports() makes this module importable under that name."""
import math
import sys
from collections import deque
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from .algorithm import QCAlgorithm
from .bars import Slice, TradeBar
from .enums import (AccountType, BrokerageName, DataNormalizationMode,
                    DayOfWeek, MovingAverageType, OrderDirection,
                    OrderStatus, OrderType, Resolution, UpdateOrderFields)
from .errors import UnsupportedApiError
from .models import (ConstantFeeModel, ConstantSlippageModel, NullFeeModel,
                     NullSlippageModel)
from .orders import OrderEvent, OrderTicket
from .portfolio_view import PortfolioTarget
from .scheduling import DateRules, TimeRules
from .symbol import Security, Symbol

__all__ = [
    "QCAlgorithm", "Resolution", "OrderType", "OrderStatus", "OrderDirection",
    "BrokerageName", "AccountType", "DataNormalizationMode", "DayOfWeek",
    "MovingAverageType", "UpdateOrderFields",
    "ConstantSlippageModel", "NullSlippageModel", "ConstantFeeModel",
    "NullFeeModel", "Symbol", "Security", "PortfolioTarget", "TradeBar",
    "Slice", "OrderEvent",
    "OrderTicket", "DateRules", "TimeRules", "UnsupportedApiError",
    "date", "datetime", "time", "timedelta", "math", "np", "pd", "deque",
]


def register_algorithm_imports():
    sys.modules.setdefault("AlgorithmImports", sys.modules[__name__])
