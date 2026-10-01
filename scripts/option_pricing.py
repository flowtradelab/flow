"""Black-Scholes europeu usando o fator de desconto interpolado da curva DI1."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

from di_curve import DEFAULT_HISTORY, DiCurve


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def black_scholes(
    spot: float,
    strike: float,
    du: int,
    volatility: float,
    discount_factor: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> dict:
    """Preço e gregas de opção europeia; vol e dividend yield são decimais."""
    option_type = option_type.upper()
    if option_type not in {"C", "P"}:
        raise ValueError("option_type deve ser C ou P")
    if spot <= 0 or strike <= 0 or du <= 0 or volatility <= 0:
        raise ValueError("Spot, strike, DU e volatilidade devem ser positivos")
    if not 0 < discount_factor <= 1.5:
        raise ValueError("Fator de desconto inválido")

    t = du / 252.0
    sqrt_t = math.sqrt(t)
    r_cont = -math.log(discount_factor) / t
    dividend_df = math.exp(-dividend_yield * t)
    d1 = (
        math.log(spot / strike)
        + (r_cont - dividend_yield + 0.5 * volatility**2) * t
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t

    if option_type == "C":
        price = spot * dividend_df * normal_cdf(d1) - strike * discount_factor * normal_cdf(d2)
        delta = dividend_df * normal_cdf(d1)
        theta_year = (
            -spot * dividend_df * normal_pdf(d1) * volatility / (2.0 * sqrt_t)
            - r_cont * strike * discount_factor * normal_cdf(d2)
            + dividend_yield * spot * dividend_df * normal_cdf(d1)
        )
        rho = strike * t * discount_factor * normal_cdf(d2) / 100.0
    else:
        price = strike * discount_factor * normal_cdf(-d2) - spot * dividend_df * normal_cdf(-d1)
        delta = dividend_df * (normal_cdf(d1) - 1.0)
        theta_year = (
            -spot * dividend_df * normal_pdf(d1) * volatility / (2.0 * sqrt_t)
            + r_cont * strike * discount_factor * normal_cdf(-d2)
            - dividend_yield * spot * dividend_df * normal_cdf(-d1)
        )
        rho = -strike * t * discount_factor * normal_cdf(-d2) / 100.0

    gamma = dividend_df * normal_pdf(d1) / (spot * volatility * sqrt_t)
    vega = spot * dividend_df * normal_pdf(d1) * sqrt_t / 100.0
    annual_rate = math.expm1(r_cont)
    return {
        "preco": price,
        "delta": delta,
        "gamma": gamma,
        "theta_por_du": theta_year / 252.0,
        "vega_por_1pct": vega,
        "rho_por_1pct": rho,
        "d1": d1,
        "d2": d2,
        "taxa_efetiva_anual": annual_rate,
        "fator_desconto": discount_factor,
        "du": du,
    }


def implied_volatility(
    premium: float,
    spot: float,
    strike: float,
    du: int,
    discount_factor: float,
    option_type: str,
    dividend_yield: float = 0.0,
    tolerance: float = 1e-8,
) -> float:
    if premium <= 0:
        raise ValueError("Prêmio deve ser positivo")
    low, high = 1e-6, 5.0
    low_price = black_scholes(spot, strike, du, low, discount_factor, option_type, dividend_yield)["preco"]
    high_price = black_scholes(spot, strike, du, high, discount_factor, option_type, dividend_yield)["preco"]
    if not low_price - tolerance <= premium <= high_price + tolerance:
        raise ValueError("Prêmio fora dos limites do modelo")
    for _ in range(120):
        middle = (low + high) / 2.0
        price = black_scholes(
            spot, strike, du, middle, discount_factor, option_type, dividend_yield
        )["preco"]
        if abs(price - premium) <= tolerance:
            return middle
        if price < premium:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def price_with_curve(
    curve: DiCurve,
    expiry: date,
    spot: float,
    strike: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> dict:
    point = curve.for_expiry(expiry)
    result = black_scholes(
        spot, strike, point.du, volatility, point.discount_factor,
        option_type, dividend_yield,
    )
    result.update({
        "data_curva": curve.session_date.isoformat(),
        "vencimento": expiry.isoformat(),
        "taxa_di_pct": point.annual_rate_pct,
        "metodo_curva": point.interpolation,
        "volatilidade": volatility,
    })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spot", type=float, required=True)
    parser.add_argument("--strike", type=float, required=True)
    parser.add_argument("--expiry", type=date.fromisoformat, required=True)
    parser.add_argument("--type", choices=("C", "P"), required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vol", type=float, help="Volatilidade decimal, ex.: 0.25")
    group.add_argument("--premium", type=float, help="Prêmio para calcular IV")
    parser.add_argument("--dividend-yield", type=float, default=0.0)
    parser.add_argument("--curve-date", type=date.fromisoformat)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    args = parser.parse_args()

    curve = DiCurve.from_history(args.history, args.curve_date)
    point = curve.for_expiry(args.expiry)
    volatility = args.vol
    if volatility is None:
        volatility = implied_volatility(
            args.premium, args.spot, args.strike, point.du,
            point.discount_factor, args.type, args.dividend_yield,
        )
    result = price_with_curve(
        curve, args.expiry, args.spot, args.strike, volatility,
        args.type, args.dividend_yield,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

