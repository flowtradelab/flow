# Histórico IV-DI B3

Coleta a curva DI1 e calcula IV, IV Rank e IV Percentil de opções brasileiras.

## Atualização histórica local

```powershell
py scripts\update_di_curve.py --sessions 250 --end-date 2026-09-30
py scripts\update_iv_di_history.py --all-existing --sessions 250 --end-date 2026-09-30
py scripts\build_iv_ranking.py
```

## Atualização incremental

Depois da carga inicial, processe somente o pregão mais recente:

```powershell
py scripts\update_di_curve.py --sessions 250 --end-date 2026-10-01
py scripts\update_iv_di_history.py --all-existing --sessions 250 --end-date 2026-10-01 --latest-only
py scripts\build_iv_ranking.py
```

O workflow `.github/workflows/update-iv-di.yml` executa essa sequência às 23h30
de São Paulo, de segunda a sexta, e também aceita execução manual com uma data.
O cache bruto em `.b3-cache/` não é versionado.
