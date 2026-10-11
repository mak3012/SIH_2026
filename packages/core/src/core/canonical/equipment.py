"""Standard casing and bit sizes, casing grades. Used by extraction rules and the program linter,
so a size the linter accepts is one the extraction rules accept too."""

from fractions import Fraction

# API 5CT casing outside diameters, inches.
CASING_OD = {"4-1/2", "5", "5-1/2", "6-5/8", "7", "7-5/8", "8-5/8", "9-5/8", "10-3/4", "11-3/4",
             "13-3/8", "16", "18-5/8", "20", "24", "30"}  # fmt: skip
# Common bit / hole sizes, inches.
HOLE_SIZES = {"6", "6-1/8", "6-1/2", "8-1/2", "8-3/4", "9-7/8", "10-5/8", "12-1/4", "14-3/4",
              "17-1/2", "22", "26", "36"}  # fmt: skip
GRADES = {"H40", "J55", "K55", "N80", "L80", "C90", "T95", "C95", "P110", "Q125"}


def size_inches(s: str) -> float:
    """'13-3/8' -> 13.375."""
    whole, _, frac = s.partition("-")
    return float(int(whole) + (Fraction(frac) if frac else 0))


def size_label(inches: float) -> str:
    """13.375 -> '13-3/8' (nearest 1/8 in)."""
    whole = int(inches)
    frac = Fraction(round((inches - whole) * 8), 8)
    if frac == 1:
        return str(whole + 1)
    return f"{whole}-{frac}" if frac else str(whole)
