def quote(shipment):
    base, step = {"economy": (180, 40), "express": (350, 75)}[shipment.service]
    return base + ((shipment.weight_grams - 1) // 500) * step
