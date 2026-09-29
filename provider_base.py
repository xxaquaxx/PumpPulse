import asyncio
from abc import ABC, abstractmethod


class MarketDataProvider(ABC):
    """Contract between the API/UI layer and ANY data source
    (mock, Helius, Birdeye, Bitquery...). Swap providers without touching the UI."""

    @abstractmethod
    async def start(self): ...

    @abstractmethod
    def snapshot(self) -> dict: ...

    @abstractmethod
    def history(self, mint: str, tf: str) -> list: ...

    @abstractmethod
    def subscribe(self) -> asyncio.Queue: ...

    @abstractmethod
    def unsubscribe(self, q: asyncio.Queue) -> None: ...
