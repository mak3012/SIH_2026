"""Small statistics used by the hazard roadmap: the Beta distribution's CDF and quantiles, the Kish
effective sample size, weighted quantiles. Pure Python (the API image carries no scipy)."""

from math import exp, lgamma, log


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a, b) = P(X <= x) for X ~ Beta(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of Beta(a, b) by bisection on the CDF (relative precision 1e-12)."""
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0, 1]")
    lo, hi = 0.0, 1.0
    # Relative, not absolute, stopping: with a < 1 the CDF is steep near 0 and small quantiles
    # (1e-6 and below) need many more significant digits than 1e-10 gives.
    for _ in range(300):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(hi, 1e-300):
            break
    return (lo + hi) / 2


def kish_n_eff(weights: list[float]) -> float:
    """Kish effective sample size (sum w)^2 / sum w^2: how many equally weighted wells the
    weighted set is worth. 0 for no weight."""
    s, s2 = sum(weights), sum(w * w for w in weights)
    return s * s / s2 if s2 > 0 else 0.0


def weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    """Weighted quantile with linear interpolation of the weighted empirical CDF."""
    pairs = sorted((v, w) for v, w in zip(values, weights, strict=True) if w > 0)
    if not pairs:
        raise ValueError("no weighted values")
    total = sum(w for _, w in pairs)
    cum, prev_v, prev_c = 0.0, pairs[0][0], 0.0
    for v, w in pairs:
        mid = (cum + w / 2) / total
        if mid >= q:
            if mid == prev_c:
                return v
            f = (q - prev_c) / (mid - prev_c) if mid > prev_c else 1.0
            return prev_v + max(0.0, min(1.0, f)) * (v - prev_v)
        prev_v, prev_c = v, mid
        cum += w
    return pairs[-1][0]
