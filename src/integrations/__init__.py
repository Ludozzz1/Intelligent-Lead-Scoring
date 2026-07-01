"""Mocked external boundaries (queue, monolith callback, channels, calendar,
inventory, trade-in). Each sits behind an interface so a real implementation can
be swapped in."""

from src.integrations.calendar import Calendar, MockCalendar
from src.integrations.channels import Channel, MockChannel
from src.integrations.inventory import Inventory, MockInventory
from src.integrations.monolith_callback import MockMonolithCallback, MonolithCallback
from src.integrations.queue import (
    DeadLetterQueue,
    FileQueue,
    InMemoryQueue,
    Queue,
    consume_all,
)
from src.integrations.trade_in import MockTradeIn, TradeInEstimator

__all__ = [
    "Channel",
    "MockChannel",
    "Calendar",
    "MockCalendar",
    "Inventory",
    "MockInventory",
    "TradeInEstimator",
    "MockTradeIn",
    "MonolithCallback",
    "MockMonolithCallback",
    "Queue",
    "InMemoryQueue",
    "FileQueue",
    "DeadLetterQueue",
    "consume_all",
]
