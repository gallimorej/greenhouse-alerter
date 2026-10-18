from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Reading:
    """One sample from a sensor. ``observed_at`` is timezone-aware."""

    sensor_id: str
    observed_at: datetime
    temperature_f: float
    humidity: float | None = None
    pressure: float | None = None
