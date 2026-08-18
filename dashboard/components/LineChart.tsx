import type { ReactNode } from "react";

export interface ChartPoint {
  at: string;
  value: number | null | undefined;
}

interface Series {
  label: string;
  color: string;
  points: ChartPoint[];
}

export function LineChart({ series, empty }: { series: Series[]; empty: ReactNode }) {
  const values = series.flatMap((item) =>
    item.points.flatMap((point) => (typeof point.value === "number" ? [point.value] : [])),
  );
  if (values.length < 2) {
    return <div className="chart-empty">{empty}</div>;
  }
  const maximum = Math.max(...values, 1);
  const minimum = Math.min(...values, 0);
  const range = Math.max(maximum - minimum, 1);
  return (
    <div className="chart-wrap">
      <svg viewBox="0 0 720 220" role="img" aria-label="Time series chart">
        <defs>
          <linearGradient id="grid-fade" x1="0" x2="1">
            <stop offset="0" stopColor="#26364a" stopOpacity="0.9" />
            <stop offset="1" stopColor="#26364a" stopOpacity="0.18" />
          </linearGradient>
        </defs>
        {[35, 85, 135, 185].map((y) => (
          <line key={y} x1="18" x2="704" y1={y} y2={y} stroke="url(#grid-fade)" />
        ))}
        {series.map((item) => {
          const points = item.points
            .map((point, index) => {
              if (typeof point.value !== "number") return null;
              const x = 18 + (index / Math.max(item.points.length - 1, 1)) * 686;
              const y = 195 - ((point.value - minimum) / range) * 170;
              return `${x.toFixed(1)},${y.toFixed(1)}`;
            })
            .filter(Boolean)
            .join(" ");
          return (
            <polyline
              key={item.label}
              points={points}
              fill="none"
              stroke={item.color}
              strokeWidth="3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          );
        })}
      </svg>
      <div className="legend">
        {series.map((item) => (
          <span key={item.label}>
            <i style={{ background: item.color }} /> {item.label}
          </span>
        ))}
      </div>
    </div>
  );
}
