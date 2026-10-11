"""Unit conversions. Internal canonical length unit is the metre."""

M_PER_FT = 0.3048  # international foot, exact


def ft_to_m(ft: float) -> float:
    return ft * M_PER_FT


def m_to_ft(m: float) -> float:
    return m / M_PER_FT
