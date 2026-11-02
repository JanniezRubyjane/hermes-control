from dataclasses import dataclass
@dataclass(frozen=True)
class Shipment:
    service: str
    weight_grams: int
    def __post_init__(self):
        if self.service not in {"economy", "express"} or self.weight_grams <= 0:
            raise ValueError("invalid shipment")
