from .pricing import amount_due
class Invoice:
    def __init__(self, lines):
        self.lines = list(lines)
    def checkout(self, percent=0, tax_percent=0):
        return amount_due(self.lines, percent, tax_percent)
