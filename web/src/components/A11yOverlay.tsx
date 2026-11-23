import { useState } from "react";
import type { A11yAuditResult, A11yFinding } from "../types";

interface A11yOverlayProps {
  a11y: A11yAuditResult | null | undefined;
  widthPoints: number;
  heightPoints: number;
  highlightedKey: string | null;
  onHighlightChange?: (key: string | null) => void;
}

export interface TaggedA11yBox {
  key: string;
  bucket: "unnamed" | "undersized";
  finding: A11yFinding;
}

export function flattenA11yBoxes(a11y: A11yAuditResult | null | undefined): TaggedA11yBox[] {
  if (!a11y) return [];
  const boxes: TaggedA11yBox[] = [];
  (a11y.unnamed ?? []).forEach((finding, idx) => {
    boxes.push({ key: `unnamed-${idx}`, bucket: "unnamed", finding });
  });
  (a11y.undersized ?? []).forEach((finding, idx) => {
    boxes.push({ key: `undersized-${idx}`, bucket: "undersized", finding });
  });
  return boxes;
}

export function A11yOverlay({
  a11y,
  widthPoints,
  heightPoints,
  highlightedKey,
  onHighlightChange,
}: A11yOverlayProps) {
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const boxes = flattenA11yBoxes(a11y);

  if (boxes.length === 0 || widthPoints <= 0 || heightPoints <= 0) {
    return null;
  }

  const activeKey = hoveredKey ?? highlightedKey;
  const activeBox = activeKey ? boxes.find((b) => b.key === activeKey) ?? null : null;

  const tooltipLeftPct = activeBox
    ? Math.min(82, Math.max(18, ((activeBox.finding.x + activeBox.finding.width / 2) / widthPoints) * 100))
    : 50;
  const tooltipTopPct = activeBox
    ? activeBox.finding.y > heightPoints * 0.22
      ? (activeBox.finding.y / heightPoints) * 100
      : ((activeBox.finding.y + activeBox.finding.height) / heightPoints) * 100
    : 12;
  const placeBelow = activeBox ? activeBox.finding.y <= heightPoints * 0.22 : false;

  return (
    <div className="a11y-overlay-layer" aria-label="Accessibility audit overlay">
      <svg
        className="a11y-overlay-svg"
        viewBox={`0 0 ${widthPoints} ${heightPoints}`}
        preserveAspectRatio="none"
      >
        {boxes.map(({ key, bucket, finding }) => {
          const isSelected = key === activeKey;
          const w = Math.max(4, finding.width);
          const h = Math.max(4, finding.height);
          const x = Math.max(0, Math.min(widthPoints - 2, finding.x));
          const y = Math.max(0, Math.min(heightPoints - 2, finding.y));
          return (
            <g
              key={key}
              className={`a11y-box a11y-box-${bucket} ${isSelected ? "is-selected" : ""}`}
              tabIndex={0}
              role="button"
              aria-label={`${bucket === "unnamed" ? "Unnamed active control" : "Touch target under 44pt"}: ${finding.kind}${finding.name ? ` ${finding.name}` : ""} (${Math.round(finding.width)}x${Math.round(finding.height)}pt)`}
              onPointerEnter={() => {
                setHoveredKey(key);
                onHighlightChange?.(key);
              }}
              onPointerLeave={() => {
                setHoveredKey(null);
                onHighlightChange?.(null);
              }}
              onFocus={() => {
                setHoveredKey(key);
                onHighlightChange?.(key);
              }}
              onBlur={() => {
                setHoveredKey(null);
                onHighlightChange?.(null);
              }}
              onClick={(e) => {
                e.stopPropagation();
                const next = highlightedKey === key ? null : key;
                onHighlightChange?.(next);
              }}
            >
              <rect
                x={x}
                y={y}
                width={w}
                height={h}
                rx={3}
                strokeWidth={isSelected ? 2.6 : 1.7}
                strokeDasharray={isSelected ? "none" : "5 3"}
              />
              <circle cx={finding.cx} cy={finding.cy} r={isSelected ? 3.5 : 2.2} />
            </g>
          );
        })}
      </svg>

      {activeBox && (
        <div
          className={`a11y-tooltip a11y-tooltip-${activeBox.bucket} ${placeBelow ? "is-below" : "is-above"}`}
          style={{
            left: `${tooltipLeftPct}%`,
            top: `${tooltipTopPct}%`,
          }}
          role="tooltip"
        >
          <div className="a11y-tooltip-head">
            <span className="a11y-tooltip-badge">
              {activeBox.bucket === "unnamed" ? "Unnamed Control" : "Target < 44pt"}
            </span>
            <span className="a11y-tooltip-dims tabular-nums">
              {Math.round(activeBox.finding.width)}×{Math.round(activeBox.finding.height)}pt
            </span>
          </div>
          <div className="a11y-tooltip-body">
            <strong>{activeBox.finding.kind || "Control"}</strong>
            {activeBox.finding.name ? ` · "${activeBox.finding.name}"` : " · (no label)"}
          </div>
          <div className="a11y-tooltip-coords tabular-nums">
            ({Math.round(activeBox.finding.x)}, {Math.round(activeBox.finding.y)}) · center (
            {activeBox.finding.cx}, {activeBox.finding.cy})
          </div>
        </div>
      )}
    </div>
  );
}
