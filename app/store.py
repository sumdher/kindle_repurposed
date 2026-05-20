from collections import deque
from dataclasses import dataclass
from datetime import datetime
import threading


@dataclass
class WeatherSample:
    timestamp: datetime
    pressure_hpa: float
    wind_speed_kmh: float


class WeatherStore:
    """Thread-safe rolling deque for 48h of weather samples (24 × 2h)."""

    def __init__(self, maxlen: int = 24):
        self._lock = threading.Lock()
        self.samples: deque[WeatherSample] = deque(maxlen=maxlen)

    def append(self, sample: WeatherSample) -> None:
        with self._lock:
            self.samples.append(sample)

    def get_all(self) -> list[WeatherSample]:
        with self._lock:
            return list(self.samples)


store = WeatherStore(maxlen=24)
