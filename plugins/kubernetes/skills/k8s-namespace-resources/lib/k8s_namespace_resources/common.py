"""Quantity, bounds and selector helpers shared by the k8s-namespace-resources modules.

Pure functions over already-parsed Kubernetes JSON: no kubectl, no clock.
"""

import re
from decimal import Decimal
from fractions import Fraction

# Bounds on what a single entry carries.
MAX_NESTED = 50
MAX_TEXT = 1024


def trunc(text):
    """(text cut to MAX_TEXT characters, whether it was cut). None stays None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:MAX_TEXT], True) if len(text) > MAX_TEXT else (text, False)


def nested(items):
    """(first MAX_NESTED items, whether any were dropped)."""
    items = list(items)
    return items[:MAX_NESTED], len(items) > MAX_NESTED


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


_QUANTITY = re.compile(
    r"^([+-]?(?:\d+\.?\d*|\.\d+))(?:([eE][+-]?\d+)|(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E))?$"
)
_SUFFIX = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 1000),
    "k": Fraction(1000),
    "M": Fraction(1000**2),
    "G": Fraction(1000**3),
    "T": Fraction(1000**4),
    "P": Fraction(1000**5),
    "E": Fraction(1000**6),
    "Ki": Fraction(1024),
    "Mi": Fraction(1024**2),
    "Gi": Fraction(1024**3),
    "Ti": Fraction(1024**4),
    "Pi": Fraction(1024**5),
    "Ei": Fraction(1024**6),
}


def parse_quantity(text):
    """Kubernetes resource quantity ('500m', '1Gi', '2', '1e3', '123456n') -> Fraction.

    An unknown suffix raises instead of being read as a plain number: the
    classic check converted only Gi/Mi and m/cores and read the rest as zero.
    """
    m = _QUANTITY.match(str(text).strip())
    if not m:
        raise ValueError(f"not a resource quantity: {text!r}")
    number, exponent, suffix = m.groups()
    value = Fraction(Decimal(number))
    if exponent:
        value *= Fraction(10) ** int(exponent[1:])
    elif suffix:
        value *= _SUFFIX[suffix]
    return value


def millicores(text):
    """A CPU quantity in millicores (float), None when absent."""
    return None if text is None else round(float(parse_quantity(text) * 1000), 3)


def bytes_of(text):
    """A memory quantity in bytes (int, rounded), None when absent."""
    return None if text is None else round(parse_quantity(text))


def percent(part, whole):
    """part / whole as a percentage rounded to one decimal; None when there is no usable whole."""
    if part is None or not whole:
        return None
    return round(part * 100 / whole, 1)
