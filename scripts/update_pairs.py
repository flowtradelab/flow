import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import * as echarts from "echarts";
import { usePersistedState } from "../hooks/usePersistedState.js";
import { usePnT } from "../providers/PnTProvider.jsx";
import { FormSpreadMaker } from "../pages/Robo.jsx";
// ── Augmented Dickey-Fuller (ADF) — implementação JS puro ────────────────────
// Retorna: { stat, pValue, isStationary, lag, n }
function adfTest(series, maxLag = 1) {
  const n = series.length;
  if (n < 20) return null;

  // Primeira diferença
  const dy = series.slice(1).map((v, i) => v - series[i]);
  // Série defasada
  const yLag = series.slice(0, n - 1);

  // Regressão OLS: dy = alpha + beta*yLag + gamma_1*dy_lag1 + ... + epsilon
  // Monta matriz X e vetor Y
  const lag = Math.min(maxLag, Math.floor((n - 1) / 4));
  const startIdx = lag;
  const T = dy.length - startIdx;

  const Y = dy.slice(startIdx);                          // T x 1
  const X = Y.map((_, i) => {                           // T x (2 + lag)
    const row = [1, yLag[startIdx + i]];
    for (let l = 1; l <= lag; l++) row.push(dy[startIdx + i - l]);
    return row;
  });

  // OLS: beta = (X'X)^-1 X'Y  — via Cholesky simplificado
  const k = X[0].length;
  // X'X
  const XtX = Array.from({ length: k }, () => Array(k).fill(0));
  const XtY = Array(k).fill(0);
  for (let i = 0; i < T; i++) {
    for (let r = 0; r < k; r++) {
      XtY[r] += X[i][r] * Y[i];
      for (let c = 0; c < k; c++) XtX[r][c] += X[i][r] * X[i][c];
    }
  }
  // Inversão por eliminação de Gauss
  const aug = XtX.map((row, r) => [...row, ...Array(k).fill(0).map((_, c) => c === r ? 1 : 0)]);
  for (let col = 0; col < k; col++) {
    let maxRow = col;
    for (let row = col + 1; row < k; row++) if (Math.abs(aug[row][col]) > Math.abs(aug[maxRow][col])) maxRow = row;
    [aug[col], aug[maxRow]] = [aug[maxRow], aug[col]];
    const pivot = aug[col][col];
    if (Math.abs(pivot) < 1e-12) return null;
    for (let j = col; j < 2 * k; j++) aug[col][j] /= pivot;
    for (let row = 0; row < k; row++) {
      if (row === col) continue;
      const factor = aug[row][col];
      for (let j = col; j < 2 * k; j++) aug[row][j] -= factor * aug[col][j];
    }
  }
  const XtXinv = aug.map(row => row.slice(k));
  // betas = XtXinv * XtY
  const betas = Array(k).fill(0).map((_, r) => XtXinv[r].reduce((s, v, c) => s + v * XtY[c], 0));

  // Resíduos e SE
  const resids = Y.map((y, i) => y - X[i].reduce((s, v, c) => s + v * betas[c], 0));
  const sigma2 = resids.reduce((s, e) => s + e * e, 0) / (T - k);
  const seBeta1 = Math.sqrt(sigma2 * XtXinv[1][1]);

  const stat = seBeta1 > 0 ? betas[1] / seBeta1 : 0;

  // Valores críticos MacKinnon (com constante, n→∞ ajustado)
  // tau_c: 1%=-3.43, 5%=-2.86, 10%=-2.57
  const adjFactor = 1 + (n < 100 ? 2.5 / n : 0);
  const cv1  = -3.43 * adjFactor;
  const cv5  = -2.86 * adjFactor;
  const cv10 = -2.57 * adjFactor;

  // p-value aproximado via interpolação linear nos valores críticos
  let pValue;
  if      (stat <= cv1)  pValue = 0.01;
  else if (stat <= cv5)  pValue = 0.01 + (stat - cv1) / (cv5  - cv1) * 0.04;
  else if (stat <= cv10) pValue = 0.05 + (stat - cv5) / (cv10 - cv5) * 0.05;
  else                   pValue = 0.10 + Math.min(0.90, (stat - cv10) / 3 * 0.90);

  return {
    stat: +stat.toFixed(3),
    pValue: +Math.min(0.999, Math.max(0.001, pValue)).toFixed(3),
    isStationary: stat <= cv5,
    isWeakStationary: stat <= cv10,
    cv1: +cv1.toFixed(2),
    cv5: +cv5.toFixed(2),
    cv10: +cv10.toFixed(2),
    lag,
    n,
  };
}

// ── ECharts Spread Chart ─────────────────────────────────────────────────────
function EChartsSpreadChart({ data, mean, std, asset1, asset2, C, currentSpread, currentZ, spreadIsLive = true }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !data?.length) return;
    const chart = echarts.init(ref.current, null, { renderer: "canvas" });
    chartRef.current = chart;

    const spreads  = data.map(d => d.spread);
    const plus2    = mean + 2 * std;
    const minus2   = mean - 2 * std;
    const plus1    = mean + std;
    const minus1   = mean - std;
    const labels   = data.map((d, i) => d.date
      ? d.date.slice(8,10) + '/' + d.date.slice(5,7) + '/' + d.date.slice(2,4)
      : `Dia ${i + 1}`);

    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        backgroundColor: "#0d1421",
        borderColor: "#233047",
        textStyle: { color: "#e8eaf6", fontSize: 11, fontFamily: "JetBrains Mono" },
        formatter: params => {
          const p = params[0];
          if (!p) return "";
          const z = ((p.value - mean) / std).toFixed(2);
          const color = Math.abs(z) > 2 ? "#ff4d6a" : Math.abs(z) > 1 ? "#ffd700" : "#00e676";
          return `<div style="font-family:JetBrains Mono;font-size:11px">
            <div style="color:#7f8ea3;margin-bottom:4px">${p.name}</div>
            <div>Spread: <b style="color:#00d8ff">${p.value?.toFixed(4)}</b></div>
            <div>Z-Score: <b style="color:${color}">${z}σ</b></div>
          </div>`;
        }
      },
      grid: { left: 58, right: 20, top: 20, bottom: 72 },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { color: "#546e7a", fontSize: 10, fontFamily: "JetBrains Mono",
          showMaxLabel: true, interval: Math.floor(data.length / 8) },
        axisLine: { lineStyle: { color: "#1e2d40" } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        // Range inclui o marcador "Agora" (currentSpread) para a linha
        // dourada nunca ficar cortada fora do gráfico
        min: v => {
          const lo = Number.isFinite(currentSpread) ? Math.min(v.min, currentSpread) : v.min;
          const hi = Number.isFinite(currentSpread) ? Math.max(v.max, currentSpread) : v.max;
          return +(lo - (hi - lo) * 0.25).toFixed(4);
        },
        max: v => {
          const lo = Number.isFinite(currentSpread) ? Math.min(v.min, currentSpread) : v.min;
          const hi = Number.isFinite(currentSpread) ? Math.max(v.max, currentSpread) : v.max;
          return +(hi + (hi - lo) * 0.25).toFixed(4);
        },
        axisLabel: { color: "#546e7a", fontSize: 10, fontFamily: "JetBrains Mono",
          formatter: v => v.toFixed(3) },
        splitLine: { lineStyle: { color: "#1a2535", type: "dashed" } },
        axisLine: { show: false },
      },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, zoomOnMouseWheel: true },
        { type: "slider", xAxisIndex: 0, height: 18, bottom: 4,
          fillerColor: "rgba(0,216,255,0.08)", borderColor: "#233047",
          handleStyle: { color: "#00d8ff" }, textStyle: { color: "#546e7a", fontSize: 9 } },
      ],
      series: [
        // Área do spread
        {
          name: "Spread",
          type: "line",
          data: spreads,
          smooth: true,
          lineStyle: { color: "#00d8ff", width: 2 },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(0,216,255,0.25)" },
                { offset: 1, color: "rgba(0,216,255,0.02)" },
              ]
            }
          },
          symbol: "circle", symbolSize: 4,
          showSymbol: false,
          emphasis: { scale: true, itemStyle: { color: "#00d8ff", borderColor: "#fff", borderWidth: 2 } },
          z: 10,
          // Marcador do spread AO VIVO — conecta a cotação atual ao histórico
          ...(Number.isFinite(currentSpread) ? {
            markLine: {
              silent: true,
              symbol: "none",
              data: [{ yAxis: currentSpread }],
              lineStyle: { color: "#ffd700", type: "solid", width: 1.5, opacity: 0.85 },
              label: {
                show: true,
                position: "insideEndTop",
                formatter: `${spreadIsLive === false ? "Últ. fech." : "Agora"} ${currentSpread.toFixed(4)}${Number.isFinite(currentZ) ? ` (${currentZ.toFixed(2)}σ)` : ""}`,
                color: "#ffd700",
                fontSize: 10,
                fontFamily: "JetBrains Mono",
                fontWeight: 700,
              },
            },
          } : {}),
        },
        // +2σ
        {
          name: "+2σ",
          type: "line",
          data: spreads.map(() => plus2),
          lineStyle: { color: "#ff4d6a", type: "dashed", width: 1.5 },
          symbol: "none", z: 5,
          tooltip: { show: false },
        },
        // +1σ
        {
          name: "+1σ",
          type: "line",
          data: spreads.map(() => plus1),
          lineStyle: { color: "#ff9800", type: "dashed", width: 1, opacity: 0.6 },
          symbol: "none", z: 4,
          tooltip: { show: false },
        },
        // Média
        {
          name: "Média",
          type: "line",
          data: spreads.map(() => mean),
          lineStyle: { color: "#bfc6d1", type: "dashed", width: 1.5 },
          symbol: "none", z: 5,
          tooltip: { show: false },
        },
        // -1σ
        {
          name: "-1σ",
          type: "line",
          data: spreads.map(() => minus1),
          lineStyle: { color: "#ff9800", type: "dashed", width: 1, opacity: 0.6 },
          symbol: "none", z: 4,
          tooltip: { show: false },
        },
        // -2σ
        {
          name: "-2σ",
          type: "line",
          data: spreads.map(() => minus2),
          lineStyle: { color: "#00ff9a", type: "dashed", width: 1.5 },
          symbol: "none", z: 5,
          tooltip: { show: false },
        },
        // Pontos de entrada (cruzamento para fora de ±2σ)
        {
          name: "Entrada",
          type: "scatter",
          data: spreads
            .map((v, i) => {
              if (i === 0) return null; // sem z anterior — evita falso sinal
              const z     = (v - mean) / std;
              const prevZ = (spreads[i - 1] - mean) / std;
              if (Math.abs(z) > 2 && Math.abs(prevZ) <= 2) return { value: [i, v], zScore: z };
              return null;
            })
            .filter(Boolean),
          symbolSize: 11,
          itemStyle: {
            borderColor: "#fff",
            borderWidth: 1.5,
            color: params => params.data.zScore > 0 ? "#ff4d6a" : "#00ff9a",
          },
          z: 20,
          tooltip: {
            formatter: p => {
              const z = ((p.data.value[1] - mean) / std).toFixed(2);
              const lado = z > 0
                ? `Short ${asset1} / Long ${asset2}`
                : `Long ${asset1} / Short ${asset2}`;
              const cor = z > 0 ? "#ff4d6a" : "#00ff9a";
              return `<div style="font-family:JetBrains Mono;font-size:11px">
                <div style="color:${cor};font-weight:700;margin-bottom:4px">● ENTRADA</div>
                <div>Z-Score: <b style="color:${cor}">${z}σ</b></div>
                <div style="color:#7f8ea3;margin-top:3px">${lado}</div>
              </div>`;
            }
          },
        },
        // Pontos de saída (retorno à zona neutra ±0.5σ)
        {
          name: "Saída",
          type: "scatter",
          data: spreads
            .map((v, i) => {
              if (i === 0) return null;
              const z     = (v - mean) / std;
              const prevZ = (spreads[i - 1] - mean) / std;
              if (Math.abs(z) < 0.5 && Math.abs(prevZ) >= 0.5) return [i, v];
              return null;
            })
            .filter(Boolean),
          symbol: "diamond",
          symbolSize: 10,
          itemStyle: { color: "#ffd700", borderColor: "#fff", borderWidth: 1.5 },
          z: 20,
          tooltip: {
            formatter: p => {
              const z = ((p.value[1] - mean) / std).toFixed(2);
              return `<div style="font-family:JetBrains Mono;font-size:11px">
                <div style="color:#ffd700;font-weight:700;margin-bottom:4px">◆ SAÍDA</div>
                <div>Z-Score: <b style="color:#ffd700">${z}σ</b></div>
                <div style="color:#7f8ea3;margin-top:3px">Retorno à média</div>
              </div>`;
            }
          },
        },
      ],
      legend: {
        data: ["+2σ", "+1σ", "Média", "-1σ", "-2σ", "Entrada", "Saída"],
        bottom: 28, left: "center",
        textStyle: { color: "#546e7a", fontSize: 10, fontFamily: "JetBrains Mono" },
        itemWidth: 16, itemHeight: 2, itemGap: 16,
        orient: "horizontal",
      },
    });

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => { ro.disconnect(); chart.dispose(); };
  }, [data, mean, std, asset1, asset2, currentSpread, currentZ, spreadIsLive]);

  return (
    <div ref={ref} style={{ width: "100%", height: 320 }} />
  );
}

// ── Calculadora de Neutralidade Financeira (hedge ratio) ─────────────────────
// Para spread log(PA) − β·log(PB): financeiro_B = β × financeiro_A.
// qtyB = qtyA × PA × β / PB, arredondado ao lote padrão de 100.
function LSNeutralityCalc({ a, b, priceA, priceB, beta = 1, C }) {
  const [qtyA, setQtyA] = useState(100);
  if (!priceA || !priceB) return null;

  const safeBeta  = Number.isFinite(beta) && beta > 0 ? beta : 1;
  const finA      = qtyA * priceA;
  const qtyBExact = (finA * safeBeta) / priceB;
  const qtyBLote  = Math.max(100, Math.round(qtyBExact / 100) * 100);
  const finB      = qtyBLote * priceB;
  const diff      = finB - finA * safeBeta;
  const diffPct   = finA > 0 ? (diff / (finA * safeBeta)) * 100 : 0;

  const fmt = v => v.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
  const mono = { fontFamily: "JetBrains Mono" };

  return (
    <div style={{ background: "rgba(0,216,255,.04)", border: "1px solid rgba(0,216,255,.18)",
      borderRadius: 8, padding: "10px 14px", marginBottom: 16 }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase",
        color: "#00d8ff", marginBottom: 8 }}>
        ⚖ Neutralidade Financeira {safeBeta !== 1 && <span style={{ color: "#7f8ea3", fontWeight: 500 }}>· β = {safeBeta.toFixed(3)}</span>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ fontSize: 9, color: "#7f8ea3", textTransform: "uppercase" }}>Qtd {a} (compra)</span>
          <input
            type="number" min={100} step={100} value={qtyA}
            onChange={e => setQtyA(Math.max(0, +e.target.value || 0))}
            style={{ ...mono, width: 90, padding: "4px 8px", fontSize: 12, fontWeight: 700,
              background: "#0d1421", border: "1px solid #233047", borderRadius: 6, color: "#fbbf24" }}
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ fontSize: 9, color: "#7f8ea3", textTransform: "uppercase" }}>Financeiro {a}</span>
          <span style={{ ...mono, fontSize: 12, fontWeight: 700, color: "#fbbf24" }}>{fmt(finA)}</span>
        </div>
        <span style={{ fontSize: 16, color: "#546e7a" }}>→</span>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ fontSize: 9, color: "#7f8ea3", textTransform: "uppercase" }}>Qtd {b} sugerida (venda)</span>
          <span style={{ ...mono, fontSize: 13, fontWeight: 700, color: "#00e676" }}>
            {qtyBLote.toLocaleString("pt-BR")}
            <span style={{ fontSize: 9, color: "#546e7a", fontWeight: 500 }}> (exato: {qtyBExact.toFixed(0)})</span>
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ fontSize: 9, color: "#7f8ea3", textTransform: "uppercase" }}>Financeiro {b}</span>
          <span style={{ ...mono, fontSize: 12, fontWeight: 700, color: "#00e676" }}>{fmt(finB)}</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={{ fontSize: 9, color: "#7f8ea3", textTransform: "uppercase" }}>Desbalanceio</span>
          <span style={{ ...mono, fontSize: 11, fontWeight: 700,
            color: Math.abs(diffPct) < 2 ? "#00e676" : Math.abs(diffPct) < 5 ? "#ffb700" : "#ff4757" }}>
            {diff >= 0 ? "+" : ""}{fmt(diff)} ({diffPct >= 0 ? "+" : ""}{diffPct.toFixed(1)}%)
          </span>
        </div>
      </div>
      <div style={{ fontSize: 9, color: "#546e7a", marginTop: 6 }}>
        Preços: {a} R$ {priceA.toFixed(2)} · {b} R$ {priceB.toFixed(2)} — lote padrão de 100 ações
      </div>
    </div>
  );
}



const WATCHLIST_BASE = {
  PETR4: { price: 38.42, changePct: 3.31, close: 37.19, bid: 38.40, ask: 38.44 },
  VALE3: { price: 62.18, changePct: -1.38, close: 63.05, bid: 62.15, ask: 62.20 },
  ITUB4: { price: 34.75, changePct: 1.31, close: 34.30, bid: 34.73, ask: 34.77 },
  BBDC4: { price: 15.92, changePct: -1.42, close: 16.15, bid: 15.90, ask: 15.94 },
  MGLU3: { price: 8.34, changePct: 1.46, close: 8.22, bid: 8.33, ask: 8.35 },
  WEGE3: { price: 47.82, changePct: 0.65, close: 47.51, bid: 47.80, ask: 47.84 },
  ABEV3: { price: 12.44, changePct: -0.48, close: 12.50, bid: 12.43, ask: 12.45 },
  RENT3: { price: 53.20, changePct: 2.10, close: 52.10, bid: 53.18, ask: 53.22 },
  RAIL3: { price: 20.15, changePct: 0.25, close: 20.10, bid: 20.13, ask: 20.17 },
  SUZB3: { price: 58.90, changePct: -0.85, close: 59.40, bid: 58.88, ask: 58.92 },
};


// ── GitHub pair-trading data ─────────────────────────────────────────────────
const GITHUB_BASE = "https://raw.githubusercontent.com/flowtradelab/flow/main/pair-trading";
const PERIOD_FILES = {
  "2M": "pairs_2m.json",
  "3M": "pairs_3m.json",
  "6M": "pairs_6m.json",
  "1A": "pairs_1y.json",
  "2A": "pairs_2y.json",
  "3A": "pairs_3y.json",
};

const TICKERS_EXTENDED = [
  "ABEV3","AGRO3","AMER3","AURE3","BBAS3","BBDC4","BBSE3","BEEF3",
  "BPAC11","BRAP4","BRSR6","CAML3","CEAB3","CMIG4","CPFE3","CSAN3",
  "CVCB3","CXSE3","CYRE3","DASA3","DIRR3","DXCO3","ECOR3","EGIE3",
  "ENEV3","ENGI11","EVEN3","EZTC3","FESA4","FLRY3","GGBR4","GOAU4",
  "HAPV3","HYPE3","IGTI11","IRBR3","ISAE4","ITUB4","KLBN11","LAVV3",
  "LREN3","MGLU3","MDIA3","MOVI3","MRVE3","MULT3","ODPV3","PETR3",
  "PETR4","PNVL3","PRIO3","PSSA3","RADL3","RAIL3","RAIZ4","RDOR3",
  "RECV3","RENT3","SANB11","SBSP3","SLCE3","SMTO3","SUZB3","TAEE11",
  "TIMS3","UGPA3","USIM5","VALE3","VAMO3","VIVA3","VIVT3","WEGE3",
];

function generateScannerPairs() {
  const pairs = [];
  const used = new Set();
  // Generate 55 pairs
  const candidates = [
    ["PETR4","VALE3"],["ITUB4","BBDC4"],["PETR4","PRIO3"],["VALE3","CSNA3"],["MGLU3","RENT3"],
    ["WEGE3","EMBR3"],["ABEV3","BRFS3"],["RAIL3","SUZB3"],["GGBR4","CSNA3"],["UGPA3","CSAN3"],
    ["RADL3","HAPV3"],["HAPV3","GNDI3"],["VBBR3","PETR4"],["AZUL4","GOLL4"],["ENEV3","TAEE11"],
    ["CMIG4","ELET3"],["SAPR11","SBSP3"],["ITUB4","BPAC11"],["VALE3","PRIO3"],["PETR3","VBBR3"],
    ["GGBR4","VALE3"],["BBDC4","BPAC11"],["WEGE3","RAIL3"],["RENT3","AZUL4"],["SUZB3","UGPA3"],
    ["PETR4","CSAN3"],["EMBR3","AZUL4"],["CSNA3","GGBR4"],["BRFS3","ABEV3"],["PRIO3","CSAN3"],
    ["TAEE11","CMIG4"],["ELET3","ENEV3"],["SBSP3","SAPR11"],["HAPV3","RADL3"],["GNDI3","HAPV3"],
    ["BPAC11","ITUB4"],["MGLU3","VIIA3"],["WEGE3","ENBR3"],["PETR4","GGBR4"],["VALE3","EMBR3"],
    ["ITUB4","SANB11"],["BBDC4","ITUB4"],["PETR4","UGPA3"],["RAIL3","EMBR3"],["ABEV3","BRFS3"],
    ["AZUL4","EMBR3"],["SUZB3","GGBR4"],["PRIO3","VBBR3"],["WEGE3","CSAN3"],["RENT3","MGLU3"],
    ["TAEE11","SAPR11"],["ENEV3","CMIG4"],["VALE3","CSAN3"],["PETR4","BRFS3"],["ELET3","TAEE11"],
  ];
  candidates.forEach(([a, b], i) => {
    const key = `${a}_${b}`;
    if (used.has(key)) return;
    used.add(key);
    const corr = 0.50 + Math.random() * 0.45;
    const z = (Math.random() - 0.5) * 6;
    const liq = Math.floor(5 + Math.random() * 95);
    const risk = (1 + Math.random() * 4).toFixed(2);
    const signal = Math.abs(z) > 2.5 ? (z > 0 ? "Short A/Long B" : "Long A/Short B") : Math.abs(z) > 1.5 ? "Observar" : "Neutro";
    pairs.push({ id: i + 1, a, b, corr: corr.toFixed(3), z: z.toFixed(2), liq, risk, signal });
  });
  return pairs;
}

const SCANNER_PAIRS = generateScannerPairs();

// ─── PAIRS GRID ───────────────────────────────────────────────────────────────
function PairsGrid({ C, pairs, onSelectPair, compact = false, gfs = 12, viewId = "default" }) {
  const [sortKey, setSortKey] = useState("z");
  const [sortDir, setSortDir] = useState("desc");
  const [filterSignal, setFilterSignal] = useState("all");
  const [search, setSearch] = useState("");

  const handleSort = key => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir("desc"); }
  };

  const filtered = pairs
    .filter(p => {
      if (filterSignal !== "all" && p.signal !== filterSignal) return false;
      if (search && !`${p.a}${p.b}`.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    })
    .sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (!isNaN(+va) && !isNaN(+vb)) { va = +va; vb = +vb; }
      if (va < vb) return sortDir === "asc" ? -1 : 1;
      if (va > vb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });

  const SortIcon = ({ col }) => (
    <span style={{ marginLeft: 3, opacity: sortKey === col ? 1 : 0.3, fontSize: 9 }}>
      {sortKey === col ? (sortDir === "asc" ? "▲" : "▼") : "⇅"}
    </span>
  );

  const signalColor = s => s === "Short A/Long B" || s === "Long A/Short B" ? C.accent : s === "Observar" ? C.gold : C.muted;
  const signalBg = s => s === "Short A/Long B" || s === "Long A/Short B" ? `rgba(0,212,255,.1)` : s === "Observar" ? `rgba(255,215,0,.08)` : "transparent";

  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}`, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flex: 1 }}>
          <div className="live-dot" />
          <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: 1.5, textTransform: "uppercase", color: C.muted }}>
            {compact ? "Pares Relacionados" : `Pairs Scanner · ${filtered.length} pares`}
          </span>
        </div>
        <input className="inp" placeholder="Buscar par…" value={search} onChange={e => setSearch(e.target.value)} style={{ width: 140, padding: "5px 10px", fontSize: 12 }} />
        <div style={{ display: "flex", gap: 3 }}>
          {["all", "Short A/Long B", "Long A/Short B", "Observar", "Neutro"].map(s => (
            <button key={s} onClick={() => setFilterSignal(s)} style={{ padding: "4px 9px", borderRadius: 6, border: `1px solid ${filterSignal === s ? C.accent : C.border}`, background: filterSignal === s ? `rgba(0,212,255,.12)` : "transparent", color: filterSignal === s ? C.accent : C.muted, fontSize: 10, cursor: "pointer", fontWeight: 600, whiteSpace: "nowrap" }}>
              {s === "all" ? "Todos" : s}
            </button>
          ))}
        </div>
      </div>

      <div className="scroll-modern" style={{ overflowX: "auto", maxHeight: compact ? 320 : 520, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: gfs }}>
          <thead style={{ position: "sticky", top: 0, zIndex: 2 }}>
            <tr style={{ background: C.surface }}>
              {[["#","id",60],["Par","pair",160],["Correlação","corr",110],["Z-Score","z",110],["Liquidez Conjunta","liq",140],["Risco Ajustado","risk",130],["Sinal","signal",160]].map(([lbl, key, w]) => (
                <th key={key} onClick={() => handleSort(key)} style={{ padding: "9px 14px", textAlign: key === "pair" || key === "signal" ? "left" : "right", fontSize: gfs * 0.8, letterSpacing: 1, textTransform: "uppercase", color: C.muted, fontWeight: 600, borderBottom: `1px solid ${C.border}`, cursor: "pointer", whiteSpace: "nowrap", minWidth: w, userSelect: "none" }}>
                  {lbl}<SortIcon col={key} />
                </th>
              ))}
              {onSelectPair && <th style={{ padding: "9px 14px", borderBottom: `1px solid ${C.border}` }} />}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr><td colSpan={8} style={{ textAlign:"center", padding:"32px 0", color:C.muted, fontSize:12, fontStyle:"italic" }}>Nenhum par encontrado. Adicione pares ou ajuste os filtros.</td></tr>
            )}
            {filtered.map((p, i) => {
              const z = +p.z;
              const zColor = z > 2 ? C.red : z > 1 ? "#ffa726" : z < -2 ? C.green : z < -1 ? "#66bb6a" : C.muted;
              const zBg = z > 2.5 ? "rgba(255,23,68,.06)" : z < -2.5 ? "rgba(0,230,118,.06)" : "transparent";
              const corrVal = +p.corr;
              const corrColor = corrVal > 0.85 ? C.green : corrVal > 0.7 ? C.accent : corrVal > 0.55 ? C.gold : C.red;
              const liqPct = p.liq;
              const riskVal = +p.risk;
              const riskColor = riskVal < 1.5 ? C.green : riskVal < 2.5 ? C.gold : C.red;
              return (
                <tr key={i} onClick={() => onSelectPair && onSelectPair(p.a, p.b)}
                  style={{ background: i % 2 === 0 ? "transparent" : `${C.surface}88`, cursor: onSelectPair ? "pointer" : "default", transition: "background .12s" }}
                  onMouseEnter={e => { e.currentTarget.style.background = `rgba(0,212,255,.04)`; }}
                  onMouseLeave={e => { e.currentTarget.style.background = i % 2 === 0 ? "transparent" : `${C.surface}88`; }}>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40`, color: C.muted, fontFamily: "JetBrains Mono", fontSize: gfs, textAlign: "right" }}>{p.id}</td>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40` }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontWeight: 700, color: C.accent, fontFamily: "JetBrains Mono", fontSize: gfs }}>{p.a}</span>
                      <span style={{ color: C.border, fontSize: gfs }}>÷</span>
                      <span style={{ fontWeight: 700, color: C.text, fontFamily: "JetBrains Mono", fontSize: gfs }}>{p.b}</span>
                    </div>
                  </td>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40`, textAlign: "right" }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 6 }}>
                      <div style={{ width: 36, height: 3, background: C.border, borderRadius: 2 }}>
                        <div style={{ width: `${corrVal * 100}%`, height: "100%", background: corrColor, borderRadius: 2 }} />
                      </div>
                      <span style={{ fontFamily: "JetBrains Mono", color: corrColor, fontSize: gfs }}>{p.corr}</span>
                    </div>
                  </td>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40`, textAlign: "right", background: zBg }}>
                    <span style={{ fontFamily: "JetBrains Mono", fontWeight: 700, fontSize: gfs, color: zColor }}>
                      {z > 0 ? "+" : ""}{p.z}σ
                    </span>
                  </td>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40`, textAlign: "right" }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 6 }}>
                      <div style={{ width: 44, height: 4, background: C.border, borderRadius: 2 }}>
                        <div style={{ width: `${liqPct}%`, height: "100%", background: liqPct > 70 ? C.green : liqPct > 40 ? C.gold : C.red, borderRadius: 2 }} />
                      </div>
                      <span style={{ fontFamily: "JetBrains Mono", color: C.text, fontSize: 11 }}>{liqPct}%</span>
                    </div>
                  </td>
                  <td style={{ padding: "9px 14px", borderBottom: `1px solid ${C.border}40`, textAlign: "right" }}>
                    <span style={{ fontFamily: "JetBrains Mono", color: riskColor, fontSize: 12 }}>{p.risk}</span>
                  </td>
                  <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40` }}>
                    <span style={{ display: "inline-flex", alignItems: "center", padding: "3px 10px", borderRadius: 5, background: signalBg(p.signal), color: signalColor(p.signal), fontWeight: 600, fontSize: gfs * 0.9, whiteSpace: "nowrap" }}>
                      {p.signal === "Short A/Long B" ? "↓ Short A / Long B" : p.signal === "Long A/Short B" ? "↑ Long A / Short B" : p.signal === "Observar" ? "◉ Observar" : "– Neutro"}
                    </span>
                  </td>
                  {onSelectPair && (
                    <td style={{ padding: "8px 14px", borderBottom: `1px solid ${C.border}40`, textAlign: "right" }}>
                      <button style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: 6, padding: "3px 9px", color: C.accent, fontSize: gfs * 0.9, cursor: "pointer", fontFamily: "JetBrains Mono" }}>Analisar →</button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── PL SIMULATOR ─────────────────────────────────────────────────────────────
function PLSimulator({ C, onClose, prefillPair }) {
  const [entrada, setEntrada] = useState(prefillPair ? String(+(prefillPair.p1/prefillPair.p2).toFixed(4)) : "");
  const [stop, setStop]       = useState("");
  const [alvo, setAlvo]       = useState("");
  const [capital, setCapital] = useState("10000");
  const [winRate, setWinRate] = useState("55");
  const [lado, setLado]       = useState("long"); // long | short

  const entradaV = parseFloat(entrada) || 0;
  const stopV    = parseFloat(stop)    || 0;
  const alvoV    = parseFloat(alvo)    || 0;
  const capitalV = parseFloat(capital) || 0;
  const winRateV = Math.min(99, Math.max(1, parseFloat(winRate) || 50)) / 100;

  const valid = entradaV > 0 && stopV > 0 && alvoV > 0 && capitalV > 0;

  const risco   = Math.abs(entradaV - stopV);
  const retorno = Math.abs(alvoV - entradaV);
  const rr      = risco > 0 ? retorno / risco : 0;
  const rrOk    = rr >= 1.5;

  // Kelly fraction: f = (bp − q) / b where b=rr, p=winRate, q=1−winRate
  const kelly   = rr > 0 ? Math.max(0, (rr * winRateV - (1 - winRateV)) / rr) : 0;
  const halfKelly = kelly / 2;

  // Position sizing (% capital = half-kelly)
  const posSize  = capitalV * halfKelly;
  const qtdUnits = risco > 0 ? Math.floor(posSize / risco) : 0;

  const plProfit = qtdUnits * retorno;
  const plLoss   = qtdUnits * risco;
  const expValue = plProfit * winRateV - plLoss * (1 - winRateV);

  // Payoff bar data
  const prices = Array.from({length:120}, (_,i) => entradaV > 0 ? entradaV * 0.85 + i * (entradaV * 0.3 / 119) : i);
  const W = 480, H = 160;
  const PAD = {t:14,r:12,b:28,l:54};
  const cW = W-PAD.l-PAD.r, cH = H-PAD.t-PAD.b;

  const calcPL = p => {
    if (!valid) return 0;
    if (lado === "long") {
      if (p <= stopV) return -plLoss;
      if (p >= alvoV) return plProfit;
      return (p - entradaV) * qtdUnits;
    } else {
      if (p >= stopV) return -plLoss;
      if (p <= alvoV) return plProfit;
      return (entradaV - p) * qtdUnits;
    }
  };

  const pls  = prices.map(p => calcPL(p));
  const minV = Math.min(...pls, -plLoss * 1.2);
  const maxV = Math.max(...pls, plProfit * 1.2);
  const sy   = v => PAD.t + cH - ((v - minV) / (maxV - minV || 1)) * cH;
  const sx   = i => PAD.l + (i / (prices.length-1)) * cW;
  const zeroY = sy(0);

  const pts = prices.map((_,i) => [sx(i), sy(pls[i])]);
  let line = `M${pts[0][0]},${pts[0][1]}`;
  for (let i=1;i<pts.length;i++) line += ` L${pts[i][0]},${pts[i][1]}`;
  const fill = line + ` L${sx(prices.length-1)},${zeroY} L${PAD.l},${zeroY} Z`;

  const xTicks = valid ? [stopV, entradaV, alvoV].filter(v=>v>0).sort((a,b)=>a-b) : [];

  return (
    <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,.7)", backdropFilter:"blur(8px)", zIndex:300, display:"flex", alignItems:"center", justifyContent:"center", padding:16 }}
      onClick={onClose}>
      <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:16, width:760, maxWidth:"96vw", maxHeight:"90vh", overflowY:"auto", padding:0 }}
        className="scroll-modern" onClick={e=>e.stopPropagation()}>

        {/* Header */}
        <div style={{ padding:"16px 22px", borderBottom:`1px solid ${C.border}`, display:"flex", alignItems:"center", justifyContent:"space-between" }}>
          <div>
            <div style={{ fontSize:15, fontWeight:800, color:C.text }}>Simulador de Entrada / Saída</div>
            <div style={{ fontSize:10, color:C.muted, marginTop:2 }}>P&L esperado · Risco/Retorno · Kelly Fraction</div>
          </div>
          <button onClick={onClose} style={{ background:"none", border:`1px solid ${C.border}`, borderRadius:8, width:30, height:30, cursor:"pointer", color:C.muted, fontSize:15 }}>✕</button>
        </div>

        <div style={{ padding:"18px 22px", display:"flex", flexDirection:"column", gap:16 }}>
          {/* Inputs */}
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr 1fr 1fr auto", gap:10 }}>
            {[
              { label:"Preço Entrada", val:entrada, set:setEntrada, hint:"ex: 0.6180" },
              { label:"Stop Loss",     val:stop,    set:setStop,    hint:"ex: 0.5900" },
              { label:"Alvo",          val:alvo,    set:setAlvo,    hint:"ex: 0.6700" },
              { label:"Capital (R$)",  val:capital, set:setCapital, hint:"ex: 10000"  },
              { label:"Win Rate (%)",  val:winRate, set:setWinRate, hint:"ex: 55"     },
            ].map((f,i) => (
              <div key={i}>
                <label style={{ fontSize:9, color:C.muted, letterSpacing:1, textTransform:"uppercase", display:"block", marginBottom:5 }}>{f.label}</label>
                <input value={f.val} onChange={e=>f.set(e.target.value)} placeholder={f.hint}
                  className="inp" style={{ padding:"7px 10px", fontSize:12, width:"100%" }} />
              </div>
            ))}
            <div>
              <label style={{ fontSize:9, color:C.muted, letterSpacing:1, textTransform:"uppercase", display:"block", marginBottom:5 }}>Lado</label>
              <div style={{ display:"flex", gap:4 }}>
                {["long","short"].map(l => (
                  <button key={l} onClick={()=>setLado(l)}
                    style={{ flex:1, padding:"7px 0", borderRadius:8, border:`1px solid ${lado===l?(l==="long"?C.green:C.red):C.border}`, background:lado===l?(l==="long"?"rgba(0,230,118,.12)":"rgba(255,23,68,.12)"):"transparent", color:lado===l?(l==="long"?C.green:C.red):C.muted, fontSize:11, fontWeight:700, cursor:"pointer", textTransform:"uppercase" }}>
                    {l}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Results grid */}
          {valid && (
            <div style={{ display:"grid", gridTemplateColumns:"repeat(5,1fr)", gap:10 }}>
              {[
                { label:"Risco/Retorno", value:`1 : ${rr.toFixed(2)}`, color:rrOk?C.green:C.red, sub:rrOk?"✓ Favorável":"✗ < 1.5 : 1" },
                { label:"Kelly Fraction", value:`${(kelly*100).toFixed(1)}%`, color:C.accent, sub:`½ Kelly: ${(halfKelly*100).toFixed(1)}%` },
                { label:"Posição (R$)", value:`R$${posSize.toLocaleString("pt-BR",{maximumFractionDigits:0})}`, color:C.gold, sub:`${qtdUnits} unidades` },
                { label:"Lucro Potencial", value:`+R$${plProfit.toLocaleString("pt-BR",{maximumFractionDigits:0})}`, color:C.green, sub:`+${((plProfit/capitalV)*100).toFixed(2)}% capital` },
                { label:"Perda Máx.", value:`−R$${plLoss.toLocaleString("pt-BR",{maximumFractionDigits:0})}`, color:C.red, sub:`−${((plLoss/capitalV)*100).toFixed(2)}% capital` },
              ].map((r,i) => (
                <div key={i} style={{ background:C.surface, border:`1px solid ${C.border}`, borderRadius:10, padding:"10px 12px" }}>
                  <div style={{ fontSize:9, color:C.muted, marginBottom:4 }}>{r.label}</div>
                  <div style={{ fontFamily:"JetBrains Mono", fontSize:14, fontWeight:800, color:r.color }}>{r.value}</div>
                  <div style={{ fontSize:9, color:C.muted, marginTop:3 }}>{r.sub}</div>
                </div>
              ))}
            </div>
          )}

          {/* Expected Value banner */}
          {valid && (
            <div style={{ background:expValue>=0?"rgba(0,230,118,.07)":"rgba(255,23,68,.07)", border:`1px solid ${expValue>=0?"rgba(0,230,118,.3)":"rgba(255,23,68,.3)"}`, borderRadius:10, padding:"10px 18px", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
              <div>
                <div style={{ fontSize:9, color:C.muted, letterSpacing:1.2, textTransform:"uppercase" }}>Valor Esperado (EV) da Operação</div>
                <div style={{ fontFamily:"JetBrains Mono", fontSize:20, fontWeight:800, color:expValue>=0?C.green:C.red, marginTop:2 }}>
                  {expValue>=0?"+":""}R${expValue.toLocaleString("pt-BR",{maximumFractionDigits:2,minimumFractionDigits:2})}
                </div>
              </div>
              <div style={{ fontSize:11, color:C.muted, textAlign:"right", lineHeight:1.7 }}>
                <div>EV = Lucro × Win% − Perda × Loss%</div>
                <div>= R${plProfit.toFixed(0)} × {(winRateV*100).toFixed(0)}% − R${plLoss.toFixed(0)} × {((1-winRateV)*100).toFixed(0)}%</div>
              </div>
            </div>
          )}

          {/* Payoff chart */}
          {valid && (
            <div style={{ background:C.surface, border:`1px solid ${C.border}`, borderRadius:10, padding:"14px 16px" }}>
              <div style={{ fontSize:9, color:C.muted, letterSpacing:1.2, textTransform:"uppercase", marginBottom:10 }}>Gráfico de P&L — Operação {lado.toUpperCase()}</div>
              <svg viewBox={`0 0 ${W} ${H}`} style={{ width:"100%", height:"auto", display:"block" }}>
                <defs>
                  <linearGradient id="sim_profit" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={C.green} stopOpacity="0.3" />
                    <stop offset="100%" stopColor={C.green} stopOpacity="0.02" />
                  </linearGradient>
                  <linearGradient id="sim_loss" x1="0" y1="1" x2="0" y2="0">
                    <stop offset="0%" stopColor={C.red} stopOpacity="0.3" />
                    <stop offset="100%" stopColor={C.red} stopOpacity="0.02" />
                  </linearGradient>
                  <clipPath id="sim_above"><rect x={PAD.l} y={PAD.t} width={cW} height={Math.max(zeroY-PAD.t,0)} /></clipPath>
                  <clipPath id="sim_below"><rect x={PAD.l} y={zeroY} width={cW} height={Math.max(PAD.t+cH-zeroY,0)} /></clipPath>
                  <clipPath id="sim_clip"><rect x={PAD.l} y={PAD.t} width={cW} height={cH} /></clipPath>
                </defs>
                <line x1={PAD.l} y1={zeroY} x2={PAD.l+cW} y2={zeroY} stroke={C.border} strokeWidth="1" />
                <path d={fill} fill="url(#sim_profit)" clipPath="url(#sim_above)" />
                <path d={fill} fill="url(#sim_loss)"   clipPath="url(#sim_below)" />
                <path d={line} fill="none" stroke={C.accent} strokeWidth="2" clipPath="url(#sim_clip)" />
                {xTicks.map((p,i) => {
                  const xi = prices.findIndex(pr => pr >= p);
                  if (xi < 0) return null;
                  const x = sx(xi);
                  const lbl = p === stopV ? "STOP" : p === entradaV ? "ENTRADA" : "ALVO";
                  const col = p === stopV ? C.red : p === entradaV ? C.gold : C.green;
                  return (
                    <g key={i}>
                      <line x1={x} y1={PAD.t} x2={x} y2={PAD.t+cH} stroke={col} strokeWidth="1" strokeDasharray="4,3" opacity="0.7" clipPath="url(#sim_clip)" />
                      <text x={x} y={PAD.t-2} textAnchor="middle" fill={col} fontSize="10" fontFamily="JetBrains Mono" fontWeight="700">{lbl}</text>
                      <text x={x} y={PAD.t+cH+16} textAnchor="middle" fill={col} fontSize="9" fontFamily="JetBrains Mono">{p.toFixed(4)}</text>
                    </g>
                  );
                })}
                {/* Y labels */}
                {[0, plProfit, -plLoss].filter(v=>v!==0).map((v,i) => (
                  <text key={i} x={PAD.l-4} y={sy(v)} textAnchor="end" dominantBaseline="middle" fill={v>0?C.green:C.red} fontSize="9" fontFamily="JetBrains Mono">
                    {v>0?"+":""}{v.toFixed(0)}
                  </text>
                ))}
              </svg>
            </div>
          )}

          {!valid && (
            <div style={{ textAlign:"center", padding:"24px 0", color:C.muted, fontSize:12, fontStyle:"italic" }}>
              Preencha Entrada, Stop, Alvo e Capital para ver os resultados
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── SCANNER TAB — custom pairs + custom views ────────────────────────────────
function PairsScannerTab({ C, gfs, onSelectPair, githubPairs: externalPairs = [], githubLoading: extLoading = false, selectedPeriod: scannerPeriod = "1A", onSendStrategy, isDark = true, onMountLS }) {
  const [scannerPairs, setScannerPairs] = useState([]);
  const [scannerLoading, setScannerLoading] = useState(false);
  const [scannerError, setScannerError] = useState(null);
  const [scannerMeta, setScannerMeta] = useState(null);

  useEffect(() => {
    const file = PERIOD_FILES[scannerPeriod];
    if (!file) return;
    setScannerLoading(true);
    setScannerError(null);
    fetch(`${GITHUB_BASE}/${file}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(d => {
        setScannerPairs(d.pairs || []);
        setScannerMeta({ updated: d.updated, tickers: d.tickers });
        setScannerLoading(false);
      })
      .catch(e => { setScannerError(e.message); setScannerLoading(false); });
  }, [scannerPeriod]);

  // ── Persistent custom pairs (merged with static) ──
  const [customPairs, setCustomPairs] = usePersistedState("scanner:customPairs", []);

  // ── Custom views (tabs) ──
  const [views, setViews] = usePersistedState("scanner:views", [
    { id:"all", name:"Todos os Pares", pairIds:null } // null = show all
  ]);
  const [activeView, setActiveView] = usePersistedState("scanner:activeView", "all");
  const [editingViewId, setEditingViewId] = useState(null);
  const [editingName, setEditingName] = useState("");
  const [newViewName, setNewViewName] = useState("");
  const [showNewView, setShowNewView] = useState(false);

  // ── Add pair modal ──
  const [showAddPair, setShowAddPair] = useState(false);
  const [newA, setNewA] = useState("");
  const [newB, setNewB] = useState("");
  const [addError, setAddError] = useState("");

  // ── Simulator ──
  const [showSim, setShowSim] = useState(false);
  const [simPair, setSimPair] = useState(null);
  const [showLS, setShowLS]   = useState(false);
  const [lsPair, setLsPair]   = useState(null);

  // Normaliza pares do GitHub para formato do scanner
  const githubNormalized = scannerPairs.map((p, i) => ({
    id: 2000 + i,
    a: p.a, b: p.b,
    corr: p.corr,
    z: p.zscore ?? 0,
    halfLife: p.halfLife,
    coint: p.coint,
    signal: Math.abs(p.zscore ?? 0) > 2
      ? ((p.zscore ?? 0) > 0 ? "Short A/Long B" : "Long A/Short B")
      : Math.abs(p.zscore ?? 0) > 1 ? "Observar" : "Neutro",
    liq: 50, risk: (Math.abs(p.zscore ?? 0) * 0.5 + 1).toFixed(1),
    sectorA: p.sectorA, sameSector: p.sameSector,
    fromGithub: true,
  }));

  const basePairs = githubNormalized.length > 0 ? githubNormalized : SCANNER_PAIRS;

  const allPairs = [
    ...basePairs,
    ...customPairs.map((cp, i) => ({
      id: 1000 + i,
      a: cp.a, b: cp.b,
      corr: cp.corr || (0.60 + Math.random() * 0.35).toFixed(3),
      z: cp.z || ((Math.random()-0.5)*5).toFixed(2),
      liq: cp.liq || Math.floor(20 + Math.random() * 70),
      risk: cp.risk || (1+Math.random()*3).toFixed(2),
      signal: cp.signal || (Math.abs(+(cp.z||0))>2.5 ? "Long A/Short B" : "Neutro"),
      custom: true,
    }))
  ];

  const currentView = views.find(v => v.id === activeView) || views[0];
  const viewPairs = currentView?.pairIds === null
    ? allPairs
    : allPairs.filter(p => (currentView?.pairIds||[]).includes(`${p.a}_${p.b}`) || (currentView?.pairIds||[]).includes(`${p.b}_${p.a}`));

  const addPair = () => {
    const a = newA.trim().toUpperCase(), b = newB.trim().toUpperCase();
    if (!a || !b) { setAddError("Preencha ambos os tickers."); return; }
    if (a === b) { setAddError("Os tickers devem ser diferentes."); return; }
    const exists = allPairs.find(p => (p.a===a&&p.b===b)||(p.a===b&&p.b===a));
    if (exists) { setAddError("Par já existe no scanner."); return; }
    const z = ((Math.random()-0.5)*5).toFixed(2);
    const corr = (0.55+Math.random()*0.40).toFixed(3);
    const liq = Math.floor(20+Math.random()*70);
    const risk = (1+Math.random()*3).toFixed(2);
    const signal = Math.abs(+z)>2.5?(+z>0?"Short A/Long B":"Long A/Short B"):Math.abs(+z)>1.5?"Observar":"Neutro";
    setCustomPairs(prev => [...prev, { a, b, z, corr, liq, risk, signal }]);
    // If in a custom view, add the pair key to it
    if (currentView?.pairIds !== null) {
      setViews(prev => prev.map(v => v.id===activeView ? { ...v, pairIds:[...(v.pairIds||[]), `${a}_${b}`] } : v));
    }
    setNewA(""); setNewB(""); setAddError(""); setShowAddPair(false);
  };

  const deletePair = (pair) => {
    if (pair.custom) setCustomPairs(prev => prev.filter(p => !(p.a===pair.a&&p.b===pair.b)));
    // Remove from all views
    setViews(prev => prev.map(v => v.pairIds ? {...v, pairIds: v.pairIds.filter(id=>id!==`${pair.a}_${pair.b}`&&id!==`${pair.b}_${pair.a}`)} : v));
  };

  const createView = () => {
    if (!newViewName.trim()) return;
    const id = `view_${Date.now()}`;
    setViews(prev => [...prev, { id, name: newViewName.trim(), pairIds: [] }]);
    setActiveView(id);
    setNewViewName(""); setShowNewView(false);
  };

  const deleteView = (id) => {
    if (id === "all") return;
    setViews(prev => prev.filter(v => v.id !== id));
    if (activeView === id) setActiveView("all");
  };

  const startRenameView = (v) => { setEditingViewId(v.id); setEditingName(v.name); };
  const commitRename = () => {
    if (editingName.trim()) setViews(prev => prev.map(v => v.id===editingViewId ? {...v, name:editingName.trim()} : v));
    setEditingViewId(null);
  };

  const addPairToView = (pair) => {
    if (!currentView || currentView.pairIds === null) return;
    const key = `${pair.a}_${pair.b}`;
    if ((currentView.pairIds||[]).includes(key)) return;
    setViews(prev => prev.map(v => v.id===activeView ? {...v, pairIds:[...(v.pairIds||[]), key]} : v));
  };

  const removePairFromView = (pair) => {
    setViews(prev => prev.map(v => v.id===activeView && v.pairIds ? {...v, pairIds: v.pairIds.filter(id=>id!==`${pair.a}_${pair.b}`&&id!==`${pair.b}_${pair.a}`)} : v));
  };

  return (
    <div>
      {/* Meta */}
      {(scannerLoading || scannerError || scannerMeta) && (
        <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:10 }}>
          {scannerLoading && <span style={{ fontSize:10, color:C.muted, fontFamily:"JetBrains Mono" }}>carregando...</span>}
          {scannerError && <span style={{ fontSize:10, color:"#f43f5e" }} title={scannerError}>⚠ erro</span>}
          {scannerMeta && !scannerLoading && (
            <span style={{ fontSize:9, color:"#00e676", fontFamily:"JetBrains Mono" }}>
              {scannerPairs.length} pares · {(d => d ? d.slice(8,10)+'/'+d.slice(5,7)+'/'+d.slice(0,4) : '')(scannerMeta.updated)}
            </span>
          )}
        </div>
      )}

      {/* KPI bar */}
      <div className="g4" style={{ marginBottom:14 }}>
        {[
          { label:"Pares Monitorados",  value:allPairs.length,                                        color:C.accent },
          { label:"Sinais Ativos",      value:allPairs.filter(p=>p.signal!=="Neutro").length,         color:C.green  },
          { label:"Z-Score > 2σ",       value:allPairs.filter(p=>Math.abs(+p.z)>2).length,           color:C.red    },
          { label:"Corr. Média",        value:(allPairs.reduce((s,p)=>s+(+p.corr),0)/Math.max(allPairs.length,1)).toFixed(3), color:C.gold },
        ].map((k,i) => (
          <div key={i} className="card">
            <div className="kpi-lbl">{k.label}</div>
            <div className="kpi-val" style={{ fontSize:22, color:k.color }}>{k.value}</div>
          </div>
        ))}
      </div>

      {/* Toolbar: views tabs + actions */}
      <div className="card" style={{ padding:"10px 14px", marginBottom:10 }}>
        <div style={{ display:"flex", alignItems:"center", gap:8, flexWrap:"wrap" }}>
          {/* View tabs */}
          <div style={{ display:"flex", gap:4, flex:1, flexWrap:"wrap", alignItems:"center" }}>
            {views.map(v => (
              <div key={v.id} style={{ display:"flex", alignItems:"center", gap:0 }}>
                {editingViewId === v.id ? (
                  <input value={editingName} onChange={e=>setEditingName(e.target.value)}
                    onBlur={commitRename} onKeyDown={e=>{if(e.key==="Enter")commitRename();if(e.key==="Escape")setEditingViewId(null);}}
                    autoFocus
                    style={{ padding:"4px 8px", borderRadius:"6px 0 0 6px", background:C.card, border:`1px solid ${C.accent}`, color:C.accent, fontSize:11, fontWeight:700, outline:"none", width:120 }} />
                ) : (
                  <button
                    onClick={()=>setActiveView(v.id)}
                    onDoubleClick={()=>v.id!=="all"&&startRenameView(v)}
                    title={v.id!=="all"?"Duplo clique para renomear":""}
                    style={{ padding:"5px 12px", borderRadius: v.id!=="all" ? "6px 0 0 6px" : "6px", border:`1px solid ${activeView===v.id?C.accent:C.border}`, background:activeView===v.id?"rgba(0,212,255,.12)":"transparent", color:activeView===v.id?C.accent:C.muted, fontSize:11, fontWeight:700, cursor:"pointer", transition:"all .15s", whiteSpace:"nowrap" }}>
                    {v.name}
                    {v.pairIds!==null && <span style={{ marginLeft:5, fontSize:9, opacity:.7 }}>({(v.pairIds||[]).length})</span>}
                  </button>
                )}
                {v.id !== "all" && editingViewId !== v.id && (
                  <button onClick={()=>deleteView(v.id)} title="Deletar aba"
                    style={{ padding:"5px 7px", borderRadius:"0 6px 6px 0", border:`1px solid ${activeView===v.id?C.accent:C.border}`, borderLeft:"none", background:activeView===v.id?"rgba(0,212,255,.08)":"transparent", color:C.red, fontSize:10, cursor:"pointer", lineHeight:1 }}>
                    ✕
                  </button>
                )}
              </div>
            ))}

            {/* New view */}
            {showNewView ? (
              <div style={{ display:"flex", gap:4, alignItems:"center" }}>
                <input value={newViewName} onChange={e=>setNewViewName(e.target.value)}
                  onKeyDown={e=>{if(e.key==="Enter")createView();if(e.key==="Escape")setShowNewView(false);}}
                  placeholder="Nome da aba…" autoFocus
                  style={{ padding:"5px 10px", borderRadius:6, background:C.card, border:`1px solid ${C.accent}`, color:C.text, fontSize:11, outline:"none", width:140 }} />
                <button onClick={createView} style={{ padding:"5px 10px", borderRadius:6, background:C.accent, border:"none", color:"#0a0d14", fontSize:11, fontWeight:700, cursor:"pointer" }}>Criar</button>
                <button onClick={()=>setShowNewView(false)} style={{ padding:"5px 8px", borderRadius:6, background:"transparent", border:`1px solid ${C.border}`, color:C.muted, fontSize:11, cursor:"pointer" }}>✕</button>
              </div>
            ) : (
              <button onClick={()=>setShowNewView(true)}
                style={{ padding:"5px 10px", borderRadius:6, border:`1px dashed ${C.border}`, background:"transparent", color:C.muted, fontSize:11, cursor:"pointer", display:"flex", alignItems:"center", gap:4 }}>
                <span style={{ fontSize:14, lineHeight:1 }}>+</span> Nova aba
              </button>
            )}
          </div>

          {/* Action buttons */}
          <div style={{ display:"flex", gap:6, flexShrink:0 }}>
            {currentView?.pairIds !== null && currentView?.pairIds?.length === 0 && allPairs.length > 0 && (
              <span style={{ fontSize:10, color:C.muted, fontStyle:"italic", display:"flex", alignItems:"center" }}>
                Clique em "Adicionar ao View" no grid principal
              </span>
            )}
            <button onClick={()=>setShowSim(true)}
              style={{ padding:"6px 14px", borderRadius:8, border:`1px solid ${C.gold}`, background:"rgba(255,215,0,.08)", color:C.gold, fontSize:11, fontWeight:700, cursor:"pointer", display:"flex", alignItems:"center", gap:5 }}>
              📐 Simulador P&L
            </button>
            <button onClick={()=>setShowAddPair(true)}
              style={{ padding:"6px 14px", borderRadius:8, border:`1px solid ${C.accent}`, background:"rgba(0,212,255,.08)", color:C.accent, fontSize:11, fontWeight:700, cursor:"pointer", display:"flex", alignItems:"center", gap:5 }}>
              + Adicionar Par
            </button>
          </div>
        </div>
      </div>

      {/* Grid with extra action column */}
      <div className="card" style={{ padding:0, overflow:"hidden" }}>
        <div style={{ padding:"12px 16px", borderBottom:`1px solid ${C.border}`, display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
          <div style={{ display:"flex", alignItems:"center", gap:6, flex:1 }}>
            <div className="live-dot" />
            <span style={{ fontSize:10, fontWeight:700, letterSpacing:1.5, textTransform:"uppercase", color:C.muted }}>
              {currentView?.name || "Todos"} · {viewPairs.length} pares
            </span>
          </div>
        </div>
        <ScannerGridWithActions
          C={C} gfs={gfs} pairs={viewPairs} allPairs={allPairs}
              onMountLS={(p) => { if (onMountLS) { onMountLS(p.a, p.b); } else { setLsPair(p); setShowLS(true); } }}
          onSelectPair={onSelectPair}
          onSimulate={(pair)=>{setSimPair(pair);setShowSim(true);}}
          onDeletePair={deletePair}
          onAddToView={currentView?.pairIds!==null?addPairToView:null}
          onRemoveFromView={currentView?.pairIds!==null?removePairFromView:null}
          isCustomView={currentView?.pairIds!==null}
        />
      </div>

      {/* Add pair modal */}
      {showAddPair && (
        <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,.65)", backdropFilter:"blur(6px)", zIndex:400, display:"flex", alignItems:"center", justifyContent:"center" }}
          onClick={()=>setShowAddPair(false)}>
          <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:14, padding:24, width:380, maxWidth:"94vw" }} onClick={e=>e.stopPropagation()}>
            <div style={{ fontSize:14, fontWeight:800, marginBottom:16 }}>Adicionar Par ao Scanner</div>
            <div style={{ display:"flex", gap:10, alignItems:"center", marginBottom:10 }}>
              <div style={{ flex:1 }}>
                <label style={{ fontSize:9, color:C.muted, textTransform:"uppercase", letterSpacing:1, display:"block", marginBottom:4 }}>Ativo A</label>
                <input value={newA} onChange={e=>setNewA(e.target.value.toUpperCase())} placeholder="Ex: PETR4"
                  className="inp" style={{ textTransform:"uppercase", fontFamily:"JetBrains Mono", fontWeight:700, color:C.accent }} autoFocus />
              </div>
              <div style={{ paddingTop:14, fontSize:18, color:C.muted, fontWeight:700 }}>÷</div>
              <div style={{ flex:1 }}>
                <label style={{ fontSize:9, color:C.muted, textTransform:"uppercase", letterSpacing:1, display:"block", marginBottom:4 }}>Ativo B</label>
                <input value={newB} onChange={e=>setNewB(e.target.value.toUpperCase())} placeholder="Ex: VALE3"
                  className="inp" style={{ textTransform:"uppercase", fontFamily:"JetBrains Mono", fontWeight:700, color:C.text }}
                  onKeyDown={e=>e.key==="Enter"&&addPair()} />
              </div>
            </div>
            {addError && <div style={{ fontSize:11, color:C.red, marginBottom:8 }}>⚠ {addError}</div>}
            <div style={{ fontSize:10, color:C.muted, marginBottom:14 }}>
              Z-Score, correlação e risco serão calculados automaticamente com base em dados históricos simulados.
            </div>
            <div style={{ display:"flex", gap:8 }}>
              <button onClick={addPair} className="btn btn-p" style={{ flex:1, fontWeight:700 }}>Adicionar Par</button>
              <button onClick={()=>{setShowAddPair(false);setAddError("");}} className="btn btn-g">Cancelar</button>
            </div>
          </div>
        </div>
      )}

      {/* PL Simulator modal */}
      {showSim && <PLSimulator C={C} onClose={()=>{setShowSim(false);setSimPair(null);}} prefillPair={simPair} />}

      {/* ── Modal Montar L/S ── */}
      {showLS && lsPair && (
        <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.75)", zIndex:1000,
          display:"flex", alignItems:"center", justifyContent:"center" }}
          onClick={e => { if(e.target===e.currentTarget) setShowLS(false); }}>
          <div style={{ background:"#0d1421", border:"1px solid #1e2d40", borderRadius:12,
            padding:24, width:"min(95vw, 660px)", maxHeight:"90vh", overflowY:"auto",
            boxShadow:"0 24px 64px rgba(0,0,0,0.6)" }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:20 }}>
              <div style={{ display:"flex", alignItems:"center", gap:10 }}>
                <img
                  src={`/images/icon-system/strat-ls${isDark ? "" : "-bk"}.png`}
                  alt="Long/Short"
                  style={{ width:26, height:26, objectFit:"contain" }}
                />
                <div>
                  <div style={{ fontSize:13, fontWeight:700, color:"#e8eaf6", fontFamily:"JetBrains Mono", display:"flex", alignItems:"center", gap:6 }}>
                    Montar Long/Short
                    <span style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:1 }}>
                      <span style={{ color:"#fbbf24" }}>{lsPair.a}</span>
                      <span style={{ fontSize:9, color:"#fbbf24", letterSpacing:0.5 }}>▲ Compra</span>
                    </span>
                    <span style={{ color:"#546e7a" }}>/</span>
                    <span style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:1 }}>
                      <span style={{ color:"#00e676" }}>{lsPair.b}</span>
                      <span style={{ fontSize:9, color:"#00e676", letterSpacing:0.5 }}>▼ Venda</span>
                    </span>
                  </div>
                </div>
              </div>
              <button onClick={() => setShowLS(false)}
                style={{ background:"none", border:`1px solid #1e2d40`, borderRadius:6, color:"#546e7a", fontSize:18, cursor:"pointer", width:32, height:32, display:"flex", alignItems:"center", justifyContent:"center" }}>✕</button>
            </div>
            <LSNeutralityCalc
              a={lsPair.a}
              b={lsPair.b}
              priceA={WATCHLIST_BASE[lsPair.a]?.price || 0}
              priceB={WATCHLIST_BASE[lsPair.b]?.price || 0}
              beta={(() => {
                const gp = (externalPairs || []).find(p => (p.a === lsPair.a && p.b === lsPair.b) || (p.a === lsPair.b && p.b === lsPair.a));
                if (!gp) return 1;
                return gp.a === lsPair.a ? (gp.beta || 1) : (gp.beta ? 1 / gp.beta : 1);
              })()}
              C={C}
            />
            <FormSpreadMaker
              preFillA={lsPair.a}
              preFillB={lsPair.b}
              account={""}
              C={C}
              onSubmit={(payload) => {
                onSendStrategy && onSendStrategy(payload);
                showLsToast(lsPair.a, lsPair.b);
                setShowLS(false);
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Grid with extra actions column ────────────────────────────────────────────
function ScannerGridWithActions({ C, gfs, pairs, allPairs, onSelectPair, onSimulate, onDeletePair, onAddToView, onRemoveFromView, isCustomView, onMountLS }) {
  const [sortKey, setSortKey]       = useState("z");
  const [sortDir, setSortDir]       = useState("desc");
  const [filterSignal, setFilterSignal] = useState("all");
  const [search, setSearch]         = useState("");
  const [showAddAll, setShowAddAll] = useState(false);

  const handleSort = key => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir("desc"); }
  };

  const filtered = pairs
    .filter(p => {
      if (filterSignal !== "all" && p.signal !== filterSignal) return false;
      if (search && !`${p.a}${p.b}`.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    })
    .sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (!isNaN(+va) && !isNaN(+vb)) { va = +va; vb = +vb; }
      if (va < vb) return sortDir === "asc" ? -1 : 1;
      if (va > vb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });

  const SortIcon = ({ col }) => <span style={{ marginLeft:3, opacity:sortKey===col?1:0.3, fontSize:9 }}>{sortKey===col?(sortDir==="asc"?"▲":"▼"):"⇅"}</span>;
  const signalColor = s => s==="Short A/Long B"||s==="Long A/Short B"?C.accent:s==="Observar"?C.gold:C.muted;
  const signalBg    = s => s==="Short A/Long B"||s==="Long A/Short B"?`rgba(0,212,255,.1)`:s==="Observar"?`rgba(255,215,0,.08)`:"transparent";

  return (
    <>
      {/* Filters */}
      <div style={{ padding:"8px 16px", borderBottom:`1px solid ${C.border}`, display:"flex", gap:8, alignItems:"center", flexWrap:"wrap" }}>
        <input className="inp" placeholder="Buscar par…" value={search} onChange={e=>setSearch(e.target.value)} style={{ width:140, padding:"5px 10px", fontSize:12 }} />
        <div style={{ display:"flex", gap:3 }}>
          {["all","Short A/Long B","Long A/Short B","Observar","Neutro"].map(s => (
            <button key={s} onClick={()=>setFilterSignal(s)} style={{ padding:"4px 9px", borderRadius:6, border:`1px solid ${filterSignal===s?C.accent:C.border}`, background:filterSignal===s?`rgba(0,212,255,.12)`:"transparent", color:filterSignal===s?C.accent:C.muted, fontSize:10, cursor:"pointer", fontWeight:600, whiteSpace:"nowrap" }}>
              {s==="all"?"Todos":s}
            </button>
          ))}
        </div>
        {isCustomView && onAddToView && (
          <div style={{ marginLeft:"auto", position:"relative" }}>
            <button onClick={()=>setShowAddAll(v=>!v)}
              style={{ padding:"4px 10px", borderRadius:6, border:`1px solid ${C.border}`, background:"transparent", color:C.muted, fontSize:10, cursor:"pointer" }}>
              + Adicionar do scanner →
            </button>
            {showAddAll && (
              <div style={{ position:"absolute", right:0, top:30, background:C.card, border:`1px solid ${C.border}`, borderRadius:10, padding:10, zIndex:30, width:220, boxShadow:"0 8px 24px rgba(0,0,0,.5)", maxHeight:220, overflowY:"auto" }} className="scroll-modern">
                <div style={{ fontSize:9, color:C.muted, marginBottom:8, letterSpacing:1, textTransform:"uppercase" }}>Escolher par para adicionar</div>
                {allPairs.map(p => (
                  <div key={`${p.a}_${p.b}`} onClick={()=>{onAddToView(p);setShowAddAll(false);}}
                    style={{ padding:"5px 8px", borderRadius:5, cursor:"pointer", fontSize:11, display:"flex", gap:6, alignItems:"center" }}
                    onMouseEnter={e=>e.currentTarget.style.background="rgba(255,255,255,.05)"}
                    onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
                    <span style={{ fontFamily:"JetBrains Mono", color:C.accent, fontWeight:700 }}>{p.a}</span>
                    <span style={{ color:C.border }}>÷</span>
                    <span style={{ fontFamily:"JetBrains Mono", color:C.text, fontWeight:700 }}>{p.b}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="scroll-modern" style={{ overflowX:"auto", maxHeight:520, overflowY:"auto" }}>
        <table style={{ width:"100%", borderCollapse:"collapse", fontSize:gfs }}>
          <thead style={{ position:"sticky", top:0, zIndex:2 }}>
            <tr style={{ background:C.surface }}>
              {[["#","id",50],["Par","pair",160],["Correlação","corr",110],["Z-Score","z",110],["Liquidez","liq",120],["Risco","risk",100],["Sinal","signal",160]].map(([lbl,key,w]) => (
                <th key={key} onClick={()=>handleSort(key)} style={{ padding:"9px 12px", textAlign:key==="pair"||key==="signal"?"left":"right", fontSize:gfs*0.8, letterSpacing:1, textTransform:"uppercase", color:C.muted, fontWeight:600, borderBottom:`1px solid ${C.border}`, cursor:"pointer", whiteSpace:"nowrap", minWidth:w, userSelect:"none" }}>
                  {lbl}<SortIcon col={key} />
                </th>
              ))}
              <th style={{ padding:"9px 12px", borderBottom:`1px solid ${C.border}`, minWidth:160 }} />
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr><td colSpan={9} style={{ textAlign:"center", padding:"32px 0", color:C.muted, fontSize:12, fontStyle:"italic" }}>
                {isCustomView ? "Este view está vazio. Adicione pares usando o botão acima." : "Nenhum par encontrado."}
              </td></tr>
            )}
            {filtered.map((p,i) => {
              const z = +p.z;
              const zColor = z>2?C.red:z>1?"#ffa726":z<-2?C.green:z<-1?"#66bb6a":C.muted;
              const zBg = z>2.5?"rgba(255,23,68,.06)":z<-2.5?"rgba(0,230,118,.06)":"transparent";
              const corrVal = +p.corr;
              const corrColor = corrVal>0.85?C.green:corrVal>0.7?C.accent:corrVal>0.55?C.gold:C.red;
              const riskColor = +p.risk<1.5?C.green:+p.risk<2.5?C.gold:C.red;
              return (
                <tr key={i} style={{ background:i%2===0?"transparent":`${C.surface}88`, transition:"background .12s" }}
                  onMouseEnter={e=>e.currentTarget.style.background="rgba(0,212,255,.03)"}
                  onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"transparent":`${C.surface}88`}>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, color:C.muted, fontFamily:"JetBrains Mono", fontSize:gfs, textAlign:"right" }}>
                    {p.id}
                    {p.custom && <span style={{ marginLeft:4, fontSize:7, padding:"1px 4px", borderRadius:3, background:"rgba(167,139,250,.15)", color:"#a855f7" }}>+</span>}
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, cursor:"pointer" }} onClick={()=>onSelectPair&&onSelectPair(p.a,p.b)}>
                    <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                      <span style={{ fontWeight:700, color:C.accent, fontFamily:"JetBrains Mono", fontSize:gfs }}>{p.a}</span>
                      <span style={{ color:C.border }}>÷</span>
                      <span style={{ fontWeight:700, color:C.text, fontFamily:"JetBrains Mono", fontSize:gfs }}>{p.b}</span>
                    </div>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, textAlign:"right" }}>
                    <div style={{ display:"flex", alignItems:"center", justifyContent:"flex-end", gap:5 }}>
                      <div style={{ width:32, height:3, background:C.border, borderRadius:2 }}>
                        <div style={{ width:`${corrVal*100}%`, height:"100%", background:corrColor, borderRadius:2 }} />
                      </div>
                      <span style={{ fontFamily:"JetBrains Mono", color:corrColor, fontSize:gfs }}>{p.corr}</span>
                    </div>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, textAlign:"right", background:zBg }}>
                    <span style={{ fontFamily:"JetBrains Mono", fontWeight:700, fontSize:gfs, color:zColor }}>{z>0?"+":""}{p.z}σ</span>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, textAlign:"right" }}>
                    <div style={{ display:"flex", alignItems:"center", justifyContent:"flex-end", gap:5 }}>
                      <div style={{ width:36, height:4, background:C.border, borderRadius:2 }}>
                        <div style={{ width:`${p.liq}%`, height:"100%", background:p.liq>70?C.green:p.liq>40?C.gold:C.red, borderRadius:2 }} />
                      </div>
                      <span style={{ fontFamily:"JetBrains Mono", color:C.text, fontSize:11 }}>{p.liq}%</span>
                    </div>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40`, textAlign:"right" }}>
                    <span style={{ fontFamily:"JetBrains Mono", color:riskColor, fontSize:12 }}>{p.risk}</span>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40` }}>
                    <span style={{ display:"inline-flex", alignItems:"center", padding:"3px 10px", borderRadius:5, background:signalBg(p.signal), color:signalColor(p.signal), fontWeight:600, fontSize:gfs*0.9, whiteSpace:"nowrap" }}>
                      {p.signal==="Short A/Long B"?"↓ Short A / Long B":p.signal==="Long A/Short B"?"↑ Long A / Short B":p.signal==="Observar"?"◉ Observar":"– Neutro"}
                    </span>
                  </td>
                  <td style={{ padding:"8px 12px", borderBottom:`1px solid ${C.border}40` }}>
                    <div style={{ display:"flex", gap:5, justifyContent:"flex-end" }}>
                      <button onClick={()=>{ onMountLS && onMountLS(p); }}
                        style={{ background:"rgba(0,230,118,.08)", border:`1px solid rgba(0,230,118,.3)`, borderRadius:5, padding:"3px 8px", color:"#00e676", fontSize:10, cursor:"pointer", fontFamily:"JetBrains Mono", whiteSpace:"nowrap" }}>
                        ⇅ Montar L/S
                      </button>
                      <button onClick={()=>onSelectPair&&onSelectPair(p.a,p.b)}
                        style={{ background:"none", border:`1px solid ${C.border}`, borderRadius:5, padding:"3px 8px", color:C.accent, fontSize:10, cursor:"pointer", fontFamily:"JetBrains Mono", whiteSpace:"nowrap" }}>
                        Analisar →
                      </button>
                      <button onClick={()=>onSimulate(p)}
                        style={{ background:"rgba(255,215,0,.08)", border:`1px solid rgba(255,215,0,.25)`, borderRadius:5, padding:"3px 8px", color:C.gold, fontSize:10, cursor:"pointer", whiteSpace:"nowrap" }}>
                        📐 Sim
                      </button>
                      {isCustomView && onRemoveFromView && (
                        <button onClick={()=>onRemoveFromView(p)}
                          style={{ background:"none", border:`1px solid ${C.border}`, borderRadius:5, padding:"3px 7px", color:C.muted, fontSize:10, cursor:"pointer" }}
                          title="Remover deste view">
                          ⊖
                        </button>
                      )}
                      {p.custom && (
                        <button onClick={()=>onDeletePair(p)}
                          style={{ background:"rgba(255,23,68,.08)", border:`1px solid rgba(255,23,68,.2)`, borderRadius:5, padding:"3px 7px", color:C.red, fontSize:10, cursor:"pointer" }}
                          title="Deletar par">
                          ✕
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}


function LongShort({ C, gfs = 12, onSendStrategy, onNavigate, isDark = true }) {
  const pnt = usePnT();
  const [activeTab, setActiveTab] = useState("analise");
  const [asset1, setAsset1] = usePersistedState("ls:asset1", "PETR4");
  const [asset2, setAsset2] = usePersistedState("ls:asset2", "VALE3");
  // Modo de exibição do gráfico:
  //   "residual" = pa − α − β·pb (resíduo de cointegração, default — preserva
  //                bandas estatísticas e z-score do backend)
  //   "ratio"    = pa / pb       (razão simples — leitura intuitiva)
  // O sinal de operação (ENTRADA/SAÍDA) continua sendo determinado pelo
  // z-score do RESÍDUO, independente do modo de visualização.
  const [chartMode, setChartMode] = usePersistedState("ls:chartMode", "residual");
  const [pairFlash, setPairFlash] = useState(false);
  const [selectedPeriod, setSelectedPeriod] = usePersistedState("ls:period", "1A");
  const configCardRef = useRef(null);

  // ── GitHub pairs data ──────────────────────────────────────────────────────
  const [githubPairs, setGithubPairs] = useState([]);
  const [githubPrices, setGithubPrices] = useState({});
  const [githubHistory, setGithubHistory] = useState({});
  const [githubLoading, setGithubLoading] = useState(false);
  const [githubError, setGithubError] = useState(null);
  const [githubMeta, setGithubMeta] = useState(null);
  // Modal L/S — compartilhado entre aba Análise e Scanner
  const [showLS, setShowLS]   = useState(false);
  const [lsPair, setLsPair]   = useState(null);
  const openLS = (a, b) => {
    // Beta orientado: no modal, A é a perna comprada — beta para hedge B/A
    const gp = githubPairs.find(p => (p.a === a && p.b === b) || (p.a === b && p.b === a));
    const beta = gp ? (gp.a === a ? gp.beta : (gp.beta ? 1 / gp.beta : 1)) : 1;
    setLsPair({ a, b, beta: Number.isFinite(beta) && beta > 0 ? beta : 1 });
    setShowLS(true);
  };
  const [lsToast, setLsToast] = useState(null); // { a, b }
  const showLsToast = (a, b) => {
    setLsToast({ a, b });
    setTimeout(() => setLsToast(null), 5000);
  };

  useEffect(() => {
    const file = PERIOD_FILES[selectedPeriod];
    if (!file) return;
    setGithubLoading(true);
    setGithubError(null);
    fetch(`${GITHUB_BASE}/${file}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(d => {
        setGithubPairs(d.pairs || []);
        setGithubPrices(d.prices || {});
        setGithubHistory(d.history || {});
        setGithubMeta({ updated: d.updated, tickers: d.tickers, period: d.period });
        setGithubLoading(false);
        spreadData.current = null; // reset spread cache on new data
      })
      .catch(e => { setGithubError(e.message); setGithubLoading(false); });
  }, [selectedPeriod]);

  // ── Cotações ao vivo via PnT ───────────────────────────────────────────────
  useEffect(() => {
    if (!pnt?.subscribe || pnt?.status !== "connected") return;
    pnt.subscribe(asset1, "LastTrade");
    pnt.subscribe(asset2, "LastTrade");
  }, [asset1, asset2, pnt?.status]);

  // Preço ao vivo — usa lastTrades se disponível, fallback para GitHub prices
  const getLivePrice = t =>
    pnt?.lastTrades?.[t]?.price || githubPrices?.[t]?.price || WATCHLIST_BASE[t]?.price || 0;
  const liveP1 = getLivePrice(asset1) || 38.42;
  const liveP2 = getLivePrice(asset2) || 62.18;

  // ── Spread histórico — usa GitHub se disponível, fallback mockado ──────────
  const spreadData = useRef(null);
  const pairKey = `${asset1}_${asset2}_${selectedPeriod}`;

  const githubPair = githubPairs.find(p =>
    (p.a === asset1 && p.b === asset2) || (p.a === asset2 && p.b === asset1)
  );

  if (!spreadData.current || spreadData.current.key !== pairKey) {
    if (githubPair) {
      const beta = githubPair.a === asset1 ? githubPair.beta : (githubPair.beta ? 1 / githubPair.beta : 1);

      // Tenta usar histórico real do GitHub — chave pode ser A_B ou B_A
      const histKey  = githubHistory[`${asset1}_${asset2}`] ? `${asset1}_${asset2}`
                     : githubHistory[`${asset2}_${asset1}`] ? `${asset2}_${asset1}`
                     : null;
      const rawHist  = histKey ? githubHistory[histKey] : null;

      // Se o par está invertido (B_A), a série de spread é negada (válido para
      // log-spread); a MÉDIA também precisa ser negada para as bandas baterem
      // com a série. O desvio-padrão é simétrico e permanece igual.
      const isInverted = histKey === `${asset2}_${asset1}` ||
                         (!histKey && githubPair.a !== asset1);
      const mean = isInverted ? -githubPair.spreadMean : githubPair.spreadMean;
      const std  = githubPair.spreadStd || Math.abs(mean * 0.02);

      let histData;
      if (rawHist && rawHist.length > 0) {
        // Dados reais: inverte spread/z se a ordem do par está invertida
        histData = rawHist.map(d => ({
          spread: isInverted ? -d.spread : d.spread,
          z:      isInverted ? -d.z      : d.z,
          r1:     isInverted ? d.r2      : d.r1,
          r2:     isInverted ? d.r1      : d.r2,
          date:   d.date,
        }));
      } else {
        // Fallback sintético (par não tem histórico no JSON)
        histData = Array.from({ length: 60 }, () => {
          const noise = (Math.random() - 0.5) * std * 2;
          const cycle = Math.sin(Math.random() * 6) * std * 1.5;
          const sp = mean + cycle + noise;
          return { spread: sp, z: (sp - mean) / std, r1: liveP1, r2: liveP2, date: null };
        });
      }

      spreadData.current = {
        key: pairKey,
        data: histData,
        mean, std, beta,
        fromGithub: true,
        isRealHistory: !!(rawHist && rawHist.length > 0),
        corr: githubPair.corr,
        halfLife: githubPair.halfLife,
        coint: githubPair.coint,
      };
    } else {
      const base1 = liveP1;
      const base2 = liveP2;
      const genData = Array.from({ length: 60 }, (_, i) => {
        const r1 = base1 * (1 + Math.sin(i * 0.22) * 0.06 + (Math.random() - 0.5) * 0.02);
        const r2 = base2 * (1 + Math.sin(i * 0.18 + 1) * 0.05 + (Math.random() - 0.5) * 0.02);
        return { spread: r1 / r2, r1, r2, date: null };
      });
      const spreads = genData.map(d => d.spread);
      const mean = spreads.reduce((a, b) => a + b, 0) / spreads.length;
      const std  = Math.sqrt(spreads.reduce((a, b) => a + (b - mean) ** 2, 0) / spreads.length);
      spreadData.current = { key: pairKey, data: genData.map(d => ({ ...d, z: (d.spread - mean) / std })), mean, std, isRealHistory: false };
    }
  }
  // ── Seleção da série exibida (resíduo vs razão) ────────────────────────────
  // Recalcula média e σ no front quando em modo "ratio" — apenas estatística
  // descritiva sobre o array. Cacheado por pairKey+modo para evitar reprocessar
  // a cada render. O sinal de operação continua vindo do resíduo (currentZ).
  const ratioStats = useMemo(() => {
    // Modo Razão só faz sentido com histórico real do backend —
    // dados sintéticos têm r1/r2 constantes, ratio vira linha reta.
    if (!spreadData.current?.isRealHistory) return null;
    const raw = spreadData.current?.data || [];
    const ratios = raw.map(d => Number.isFinite(d.ratio) ? d.ratio : (d.r1 / d.r2))
                      .filter(Number.isFinite);
    if (ratios.length === 0) return null;
    const m = ratios.reduce((a, b) => a + b, 0) / ratios.length;
    const v = ratios.reduce((a, b) => a + (b - m) ** 2, 0) / ratios.length;
    const s = Math.sqrt(v) || Math.abs(m * 0.01);
    return {
      mean: m,
      std:  s,
      data: raw.map((d, i) => ({
        ...d,
        spread: ratios[i],     // troca o spread exibido pela razão
        z:      (ratios[i] - m) / s,
      })),
    };
  }, [pairKey, spreadData.current]);

  const residual = spreadData.current;
  const useRatio = chartMode === "ratio" && ratioStats !== null;
  const data = useRatio ? ratioStats.data  : residual.data;
  const mean = useRatio ? ratioStats.mean  : residual.mean;
  const std  = useRatio ? ratioStats.std   : residual.std;

  // ── Spread ao vivo — DEVE usar a mesma fórmula da série exibida ───────────
  // Modo "ratio": P1/P2 ao vivo (sempre coerente com a série de razão).
  // Modo "residual": testa fórmulas candidatas e escolhe a compatível com
  // mean/std do histórico (resíduo do backend pode ser pa-α-β·pb).
  const liveSpreadCandidate = useMemo(() => {
    if (useRatio) return liveP2 > 0 ? liveP1 / liveP2 : NaN;
    const b = residual?.beta || 1;
    const candidates = [
      liveP1 / liveP2,                                  // razão simples
      Math.log(liveP1) - Math.log(liveP2),              // log-razão
      Math.log(liveP1) - b * Math.log(liveP2),          // log-spread c/ beta
    ];
    return candidates.reduce((best, c) =>
      Math.abs((c - mean) / std) < Math.abs((best - mean) / std) ? c : best
    );
  }, [liveP1, liveP2, mean, std, pairKey, useRatio]);

  // Sanidade: se faltam preços ao vivo para os tickers OU nenhuma fórmula
  // candidata ficou em escala plausível (|z| > 4σ ⇒ fórmula incompatível),
  // usa o ÚLTIMO spread do histórico — sempre existe e está na escala certa.
  // Garante que o marcador "Agora" apareça em TODOS os pares.
  const liveOk = getLivePrice(asset1) > 0 && getLivePrice(asset2) > 0;
  const candZ  = (liveSpreadCandidate - mean) / std;
  const lastHistSpread = data.length > 0 ? data[data.length - 1].spread : null;
  const spreadIsLive =
    (liveOk && Number.isFinite(candZ) && Math.abs(candZ) <= 4) || lastHistSpread === null;
  const spread   = spreadIsLive ? liveSpreadCandidate : lastHistSpread;
  // currentZ acompanha o gráfico (resíduo ou razão, conforme modo).
  // operationZ é SEMPRE do resíduo — governa sinal de ENTRADA/SAÍDA e o
  // direcionamento Long/Short, que estatisticamente só faz sentido sobre o
  // resíduo de cointegração.
  const currentZ = (spread - mean) / std;
  const operationZ = useRatio
    ? (residual.data.length > 0
        ? (residual.data[residual.data.length - 1].spread - residual.mean) / residual.std
        : currentZ)
    : currentZ;
  const correlation = githubPair ? githubPair.corr : +(0.82 + (asset1.charCodeAt(0) % 10) * 0.01).toFixed(3);

  // ── Teste ADF sobre o spread histórico ──────────────────────────────────────
  const adf = useMemo(() => {
    const spreads = data.map(d => d.spread);
    return adfTest(spreads);
  }, [data]);

  const handleSelectPair = (a, b) => {
    setAsset1(a);
    setAsset2(b);
    spreadData.current = null;
    setActiveTab("analise");
    // Scroll to config card and flash it
    setTimeout(() => {
      if (configCardRef.current) {
        configCardRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
        setPairFlash(true);
        setTimeout(() => setPairFlash(false), 900);
      }
    }, 60);
  };

  // Tab style helper
  const tabStyle = (id) => ({
    padding: "10px 22px",
    borderRadius: 0,
    border: "none",
    borderBottom: `2px solid ${activeTab === id ? C.accent : "transparent"}`,
    background: "transparent",
    color: activeTab === id ? C.accent : C.muted,
    fontWeight: activeTab === id ? 700 : 500,
    fontSize: 13,
    cursor: "pointer",
    fontFamily: "'Space Grotesk', sans-serif",
    transition: "all .15s",
    whiteSpace: "nowrap",
  });

  return (
    <div className="scroll-modern" style={{ height: "100%", overflowY: "auto", padding: 16 }}>
      {/* ── Tab Bar + Período ── */}
      <div style={{ display: "flex", alignItems: "center", borderBottom: `1px solid ${C.border}`, marginBottom: 14, gap: 0 }}>
        <button style={tabStyle("analise")} onClick={() => setActiveTab("analise")}>
          Análise de Par
        </button>
        <button style={tabStyle("scanner")} onClick={() => setActiveTab("scanner")}>
          Pairs Trading Scanner
          <span style={{ marginLeft: 7, background: `rgba(0,212,255,.12)`, color: C.accent, fontSize: 10, padding: "1px 6px", borderRadius: 10, fontWeight: 700 }}>
            {githubPairs.length > 0 ? githubPairs.length : SCANNER_PAIRS.length}
          </span>
        </button>

        {/* Período — fixo à direita, visível em ambas as abas */}
        <div style={{ flex: 1 }} />
        {githubMeta && !githubLoading && (
          <span style={{ fontSize: 9, color: "#00e676", fontFamily: "JetBrains Mono", marginRight: 10 }}>
            {(d => d ? d.slice(8,10)+'/'+d.slice(5,7)+'/'+d.slice(0,4) : '')(githubMeta.updated)}
          </span>
        )}
        <div style={{ display: "flex", gap: 4, paddingBottom: 2 }}>
          {Object.keys(PERIOD_FILES).map(p => (
            <button key={p}
              onClick={() => { setSelectedPeriod(p); spreadData.current = null; }}
              style={{
                padding: "2px 8px", fontSize: 10, borderRadius: 5, cursor: "pointer",
                fontWeight: selectedPeriod === p ? 700 : 400,
                background: selectedPeriod === p ? C.accent : "transparent",
                color: selectedPeriod === p ? "#0a0d14" : C.muted,
                border: `1px solid ${selectedPeriod === p ? C.accent : C.border}`,
                transition: "all .15s",
              }}>
              {p}
            </button>
          ))}
        </div>
      </div>

      {/* ══════════════════ TAB: ANÁLISE DE PAR ══════════════════ */}
      {activeTab === "analise" && (
        <div>

          {/* ── Barra única: selects + métricas + sinal ── */}
          {(() => {
            const zColor  = Math.abs(operationZ) > 2 ? C.red : Math.abs(operationZ) > 1 ? "#ffb700" : C.green;
            const hasEntry = Math.abs(operationZ) > 2;
            return (
              <div
                ref={configCardRef}
                className="card"
                style={{
                  marginBottom: 10,
                  padding: "8px 14px",
                  display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
                  transition: "box-shadow .4s, border-color .4s",
                  boxShadow: pairFlash ? `0 0 0 2px ${C.accent}` : "none",
                  border: `1px solid ${pairFlash ? C.accent : hasEntry ? C.accent : C.border}`,
                }}
              >
                {/* Selects */}
                <select className="inp" style={{ width: 120, padding: "4px 8px", fontSize: 12 }} value={asset1} onChange={e => { setAsset1(e.target.value); spreadData.current = null; }}>
                  {TICKERS_EXTENDED.map(t => <option key={t}>{t}</option>)}
                </select>
                <span style={{ color: C.muted, fontSize: 14, fontWeight: 700 }}>÷</span>
                <select className="inp" style={{ width: 120, padding: "4px 8px", fontSize: 12 }} value={asset2} onChange={e => { setAsset2(e.target.value); spreadData.current = null; }}>
                  {TICKERS_EXTENDED.filter(t => t !== asset1).map(t => <option key={t}>{t}</option>)}
                </select>

                {/* Ratio simples ao vivo (P1/P2) — recalculado a cada tick.
                    Fallback no githubPair.ratio (último fechamento do backend)
                    quando os preços ao vivo ainda não chegaram. */}
                {(() => {
                  const lp1 = getLivePrice(asset1);
                  const lp2 = getLivePrice(asset2);
                  const liveRatio = lp1 > 0 && lp2 > 0 ? lp1 / lp2 : null;
                  const ratio = liveRatio ?? githubPair?.ratio ?? null;
                  if (ratio === null || !Number.isFinite(ratio)) return null;
                  return (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 6,
                      padding: "4px 10px",
                      background: "rgba(0,216,255,.06)",
                      border: `1px solid ${C.accent}33`,
                      borderRadius: 6,
                      marginLeft: 4,
                    }}>
                      <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase" }}>Ratio</span>
                      <span style={{ fontFamily: "JetBrains Mono", fontSize: 13, fontWeight: 700, color: C.accent }}>
                        {ratio.toFixed(4)}
                      </span>
                    </div>
                  );
                })()}

                <div style={{ width: 1, height: 24, background: C.border, margin: "0 2px" }} />

                {/* Spread */}
                <div style={{ display:"flex", flexDirection:"column", alignItems:"center" }}>
                  <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase" }}>Spread</span>
                  <span style={{ fontFamily: "JetBrains Mono", fontSize: 13, fontWeight: 700, color: C.text }}>{spread.toFixed(4)}</span>
                </div>

                <div style={{ width: 1, height: 24, background: C.border, margin: "0 2px" }} />

                {/* Correlação */}
                <div style={{ display:"flex", flexDirection:"column", alignItems:"center" }}>
                  <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase" }}>Correl.</span>
                  <span style={{ fontFamily: "JetBrains Mono", fontSize: 13, fontWeight: 700, color: C.accent }}>{correlation}</span>
                </div>

                <div style={{ width: 1, height: 24, background: C.border, margin: "0 2px" }} />

                {/* Z-Score */}
                <div style={{ display:"flex", flexDirection:"column", alignItems:"center" }}>
                  <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase" }}>Z-Score</span>
                  <span style={{ fontFamily: "JetBrains Mono", fontSize: 13, fontWeight: 700, color: zColor }}>{currentZ.toFixed(2)}σ</span>
                </div>

                <div style={{ width: 1, height: 24, background: C.border, margin: "0 2px" }} />

                {/* Sinal */}
                {hasEntry ? (
                  <div style={{ display:"flex", alignItems:"center", gap:10 }}>
                    <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-start" }}>
                      <span style={{ fontSize: 11, fontWeight: 800, color: C.accent, letterSpacing: 1 }}>ENTRADA</span>
                      <span style={{ fontSize: 10, color: C.muted }}>{operationZ > 0 ? `Short ${asset1} / Long ${asset2}` : `Long ${asset1} / Short ${asset2}`}</span>
                    </div>
                    <button
                      onClick={() => openLS(operationZ > 0 ? asset2 : asset1, operationZ > 0 ? asset1 : asset2)}
                      style={{
                        padding: "4px 12px", borderRadius: 6, cursor: "pointer",
                        border: "1px solid rgba(0,230,118,.4)",
                        background: "rgba(0,230,118,.1)",
                        color: "#00e676", fontSize: 11, fontWeight: 700,
                        fontFamily: "JetBrains Mono", whiteSpace: "nowrap",
                        transition: "all .15s",
                      }}
                      onMouseEnter={e => { e.currentTarget.style.background = "rgba(0,230,118,.22)"; e.currentTarget.style.borderColor = "#00e676"; }}
                      onMouseLeave={e => { e.currentTarget.style.background = "rgba(0,230,118,.1)";  e.currentTarget.style.borderColor = "rgba(0,230,118,.4)"; }}
                    >
                      ⇅ Montar L/S
                    </button>
                  </div>
                ) : (
                  <span style={{ fontSize: 11, color: C.muted, fontWeight: 600 }}>AGUARDAR</span>
                )}

                {/* Spacer + status */}
                <div style={{ flex: 1 }} />
                {githubLoading && <span style={{ fontSize:10, color:C.muted }}>carregando...</span>}
                {githubError   && <span style={{ fontSize:10, color:"#f43f5e" }} title={githubError}>⚠ erro</span>}
              </div>
            );
          })()}

          {/* ── Card do gráfico + banner de cointegração interno ── */}
          <div className="card" style={{ marginBottom: 10 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6, gap: 12, flexWrap: "wrap" }}>
              <div className="card-title" style={{ marginBottom: 0 }}>
                {chartMode === "ratio" ? "Razão" : "Spread Histórico"} — {asset1}/{asset2}
              </div>
              {/* Toggle Resíduo / Razão — segmented control compacto */}
              <div style={{
                display: "inline-flex",
                background: "rgba(0,216,255,.04)",
                border: `1px solid ${C.border}`,
                borderRadius: 5,
                padding: 1,
                fontFamily: "JetBrains Mono",
              }}>
                {[
                  { id: "residual", label: "Resíduo", hint: "Resíduo de cointegração (pa − α − β·pb)" },
                  { id: "ratio",    label: "Razão",   hint: ratioStats ? "Razão simples P₁/P₂" : "Sem histórico real para este par/período" },
                ].map(opt => {
                  const active = chartMode === opt.id && (opt.id !== "ratio" || ratioStats);
                  const disabled = opt.id === "ratio" && !ratioStats;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => !disabled && setChartMode(opt.id)}
                      title={opt.hint}
                      style={{
                        padding: "2px 8px",
                        background: active ? C.accent : "transparent",
                        color: disabled ? (C.muted + "55") : active ? "#0d1421" : C.muted,
                        border: "none",
                        borderRadius: 3,
                        fontSize: 9,
                        fontWeight: 700,
                        letterSpacing: 0.8,
                        textTransform: "uppercase",
                        cursor: disabled ? "not-allowed" : "pointer",
                        transition: "background .15s, color .15s",
                        lineHeight: 1.4,
                        opacity: disabled ? 0.4 : 1,
                      }}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
            <EChartsSpreadChart data={data} mean={mean} std={std} asset1={asset1} asset2={asset2} C={C} currentSpread={spread} currentZ={currentZ} spreadIsLive={spreadIsLive} />

            {/* Banner de cointegração — dentro do card, compacto */}
            {adf && (() => {
              const adfOk    = adf.isStationary;
              const adfWeak  = !adfOk && adf.isWeakStationary;
              const cointRaw = githubPair?.coint;
              const hasCoint = cointRaw != null;
              const cointOk  = hasCoint && cointRaw === true;

              let verdict;
              if (hasCoint) {
                verdict = cointOk ? "ok" : (!cointOk && adfOk ? "conflict" : "bad");
              } else {
                verdict = adfOk ? "ok" : adfWeak ? "weak" : "bad";
              }

              const pal = {
                ok:       { bg: "rgba(0,230,118,.06)",  border: "rgba(0,230,118,.2)",  color: "#00e676", icon: "✓", title: "Par Cointegrado" },
                weak:     { bg: "rgba(255,183,0,.06)",  border: "rgba(255,183,0,.2)",  color: "#ffb700", icon: "⚠", title: "Cointegração Fraca" },
                bad:      { bg: "rgba(255,23,68,.06)",  border: "rgba(255,23,68,.2)",  color: "#ff4757", icon: "✗", title: "Não-Cointegrado" },
                conflict: { bg: "rgba(255,183,0,.06)",  border: "rgba(255,183,0,.2)",  color: "#ffb700", icon: "⚠", title: "Sinais Divergentes" },
              }[verdict];

              const longPeriod = ["2A","3A"].includes(selectedPeriod);

              return (
                <div style={{ marginTop: 8, background: pal.bg, border: `1px solid ${pal.border}`, borderRadius: 7, padding: "6px 12px", display:"flex", alignItems:"center", gap: 10, flexWrap:"wrap" }}>
                  <span style={{ fontSize:12, fontWeight:700, color: pal.color }}>{pal.icon} {pal.title}</span>
                  <span style={{ fontSize:9, color: C.muted, fontFamily:"JetBrains Mono" }}>
                    ADF stat: <b style={{ color: adfOk ? "#00e676" : adfWeak ? "#ffb700" : "#ff4757" }}>{adf.stat}</b>
                    {" · "}p: <b style={{ color: adfOk ? "#00e676" : adfWeak ? "#ffb700" : "#ff4757" }}>{adf.pValue}</b>
                    {" · "}n={adf.n}
                  </span>
                  {hasCoint && (
                    <span style={{ fontSize:9, fontFamily:"JetBrains Mono", color: C.muted }}>
                      Coint. backend: <b style={{ color: cointOk ? "#00e676" : "#ff4757" }}>{cointOk ? "✓" : "✗"}</b>
                      {spreadData.current?.isRealHistory && <span style={{ color: C.muted }}> · {data.length} pregões</span>}
                    </span>
                  )}
                  {(verdict === "bad" || verdict === "conflict") && longPeriod && (
                    <span style={{ fontSize:10, color:"#ffb700" }}>💡 Tente 1A ou 6M</span>
                  )}
                </div>
              );
            })()}
          </div>

          {/* ── Pares Relacionados — ocupa o espaço abaixo do gráfico ── */}
          <PairsGrid
            C={C}
            pairs={(githubPairs.length > 0 ? githubPairs.slice(0,20).map(p => ({ a:p.a, b:p.b, corr:p.corr, z:p.zscore, halfLife:p.halfLife, coint:p.coint, sector:p.sectorA })) : SCANNER_PAIRS.slice(0, 20))}
            onSelectPair={handleSelectPair}
            compact={true}
            gfs={gfs}
          />
        </div>
      )}

      {/* ══════════════════ TAB: PAIRS TRADING SCANNER ══════════════════ */}
      {activeTab === "scanner" && (
        <PairsScannerTab C={C} gfs={gfs} onSelectPair={handleSelectPair} githubPairs={githubPairs} githubLoading={githubLoading} selectedPeriod={selectedPeriod} onSendStrategy={onSendStrategy} isDark={isDark} onMountLS={openLS} />
      )}

      {/* ── Modal Montar L/S — compartilhado entre aba Análise e Scanner ── */}
      {showLS && lsPair && (
        <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.75)", zIndex:1000,
          display:"flex", alignItems:"center", justifyContent:"center" }}
          onClick={e => { if(e.target===e.currentTarget) setShowLS(false); }}>
          <div style={{ background:"#0d1421", border:"1px solid #1e2d40", borderRadius:12,
            padding:24, width:"min(95vw, 660px)", maxHeight:"90vh", overflowY:"auto",
            boxShadow:"0 24px 64px rgba(0,0,0,0.6)" }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:20 }}>
              <div style={{ display:"flex", alignItems:"center", gap:10 }}>
                <img src={`/images/icon-system/strat-ls${isDark ? "" : "-bk"}.png`} alt="Long/Short"
                  style={{ width:26, height:26, objectFit:"contain" }} />
                <div>
                  <div style={{ fontSize:13, fontWeight:700, color:"#e8eaf6", fontFamily:"JetBrains Mono", display:"flex", alignItems:"center", gap:6 }}>
                    Montar Long/Short
                    <span style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:1 }}>
                      <span style={{ color:"#fbbf24" }}>{lsPair.a}</span>
                      <span style={{ fontSize:9, color:"#fbbf24" }}>▲ Compra</span>
                    </span>
                    <span style={{ color:"#546e7a" }}>/</span>
                    <span style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:1 }}>
                      <span style={{ color:"#00e676" }}>{lsPair.b}</span>
                      <span style={{ fontSize:9, color:"#00e676" }}>▼ Venda</span>
                    </span>
                  </div>
                </div>
              </div>
              <button onClick={() => setShowLS(false)}
                style={{ background:"none", border:"1px solid #1e2d40", borderRadius:6, color:"#546e7a", fontSize:18, cursor:"pointer", width:32, height:32, display:"flex", alignItems:"center", justifyContent:"center" }}>✕</button>
            </div>
            <LSNeutralityCalc
              a={lsPair.a}
              b={lsPair.b}
              priceA={getLivePrice(lsPair.a)}
              priceB={getLivePrice(lsPair.b)}
              beta={lsPair.beta}
              C={C}
            />
            <FormSpreadMaker
              preFillA={lsPair.a}
              preFillB={lsPair.b}
              account={""}
              C={C}
              onSubmit={(payload) => {
                onSendStrategy && onSendStrategy(payload);
                showLsToast(lsPair.a, lsPair.b);
                setShowLS(false);
              }}
            />
          </div>
        </div>
      )}

      {/* ── Toast Long/Short enviado ── */}
      {lsToast && (
        <div style={{
          position: "fixed", bottom: 28, left: "50%", transform: "translateX(-50%)",
          zIndex: 2000, display: "flex", flexDirection: "column", alignItems: "center", gap: 6,
          background: "#0d1421", border: "1px solid rgba(0,230,118,.35)",
          borderRadius: 10, padding: "12px 20px", minWidth: 300,
          boxShadow: "0 8px 32px rgba(0,0,0,.6)",
          animation: "lsToastIn .25s ease",
        }}>
          <style>{`@keyframes lsToastIn{from{opacity:0;transform:translateX(-50%) translateY(12px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}`}</style>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 14, color: "#00e676" }}>✓</span>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#e8eaf6", fontFamily: "JetBrains Mono" }}>
              Long/Short enviado ao robô
            </span>
            <span style={{ fontSize: 11, color: "#546e7a", fontFamily: "JetBrains Mono" }}>—</span>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#fbbf24", fontFamily: "JetBrains Mono" }}>{lsToast.a} ▲</span>
            <span style={{ fontSize: 11, color: "#546e7a" }}>/</span>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#00e676", fontFamily: "JetBrains Mono" }}>{lsToast.b} ▼</span>
          </div>
          <div style={{ fontSize: 10, color: "#546e7a" }}>
            Para visualizar, editar ou cancelar →{" "}
            <span
              onClick={() => { onNavigate && onNavigate("robo"); setLsToast(null); }}
              style={{ color: "#00d8ff", fontWeight: 700, cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2 }}
            >
              Robô
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
export { LongShort };
export default LongShort;
