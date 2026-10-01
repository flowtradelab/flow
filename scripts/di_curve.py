"""Consulta e interpola a curva DI1 histórica gerada por update_di_curve.py."""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from update_di_curve import business_days, load_holidays


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = ROOT / "data" / "di_curve" / "history.json"


@dataclass(frozen=True)
class CurvePoint:
    du: int
    discount_factor: float
    annual_rate: float
    interpolation: str

    @property
    def annual_rate_pct(self) -> float:
        return self.annual_rate * 100.0


class DiCurve:
    """Curva zero DI1 de uma sessão, com taxas em formato decimal."""

    def __init__(self, session: dict):
        self.session_date = date.fromisoformat(session["data"])
        nodes = sorted(session["vertices"], key=lambda row: row["du"])
        if len(nodes) < 2:
            raise ValueError("A curva DI1 precisa de ao menos dois vértices")

        self._dus: list[int] = []
        self._dfs: list[float] = []
        for row in nodes:
            du = int(row["du"])
            df = float(row["fator_desconto"])
            if du <= 0 or not 0 < df <= 1.5:
                raise ValueError(f"Vértice inválido: DU={du}, fator={df}")
            if self._dus and du <= self._dus[-1]:
                raise ValueError("Os DUs da curva devem ser únicos e crescentes")
            self._dus.append(du)
            self._dfs.append(df)

    @classmethod
    def from_history(
        cls,
        path: Path = DEFAULT_HISTORY,
        session_date: date | None = None,
    ) -> "DiCurve":
        payload = json.loads(path.read_text(encoding="utf-8"))
        sessions = payload.get("historico", [])
        if not sessions:
            raise ValueError("Histórico DI1 vazio")
        if session_date is None:
            selected = sessions[-1]
        else:
            eligible = [row for row in sessions if date.fromisoformat(row["data"]) <= session_date]
            if not eligible:
                raise ValueError(f"Não há curva DI1 em ou antes de {session_date}")
            selected = eligible[-1]
        return cls(selected)

    def point(self, du: int) -> CurvePoint:
        if du < 0:
            raise ValueError("DU não pode ser negativo")
        if du == 0:
            return CurvePoint(0, 1.0, 0.0, "hoje")

        index = bisect_left(self._dus, du)
        if index < len(self._dus) and self._dus[index] == du:
            df = self._dfs[index]
            method = "vertice"
        elif index == 0:
            df = self._dfs[0] ** (du / self._dus[0])
            method = "extrapolacao_curta_taxa_zero_constante"
        elif index == len(self._dus):
            df = self._dfs[-1] ** (du / self._dus[-1])
            method = "extrapolacao_longa_taxa_zero_constante"
        else:
            left_du, right_du = self._dus[index - 1], self._dus[index]
            weight = (du - left_du) / (right_du - left_du)
            log_df = math.log(self._dfs[index - 1]) + weight * (
                math.log(self._dfs[index]) - math.log(self._dfs[index - 1])
            )
            df = math.exp(log_df)
            method = "interpolacao_log_fator_desconto"

        annual_rate = df ** (-252.0 / du) - 1.0
        return CurvePoint(du, df, annual_rate, method)

    def for_expiry(self, expiry: date, holidays: set[date] | None = None) -> CurvePoint:
        if expiry < self.session_date:
            raise ValueError("Vencimento anterior à data da curva")
        calendar = load_holidays() if holidays is None else holidays
        return self.point(business_days(self.session_date, expiry, calendar))

