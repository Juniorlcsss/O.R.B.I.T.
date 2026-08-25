import useSettings from "../hooks/useSettings.jsx";
import { TONE } from "../lib/format.js";

/*
 * Redundant, non-colour encoding of status (WCAG 2.2 SC 1.4.1).
 *
 * With shape coding off this is the plain coloured dot the console has always
 * shown. With it on, each tone gets a silhouette that stays distinguishable at
 * 10px in greyscale — circle, triangle, diamond, bar — so an operator who
 * cannot separate the hues can still read the feed at a glance.
 */

const SHAPES = {
  nominal: { path: "M5 1.4A3.6 3.6 0 1 1 5 8.6 3.6 3.6 0 0 1 5 1.4Z", label: "nominal" },
  caution: { path: "M5 1 9.3 8.7H0.7L5 1Z", label: "caution" },
  alert: { path: "M5 0.6 9.4 5 5 9.4 0.6 5 5 0.6Z", label: "alert" },
  info: { path: "M0.8 3.9h8.4v2.2H0.8V3.9Z", label: "information" },
};

const DOT = "M5 1.4A3.6 3.6 0 1 1 5 8.6 3.6 3.6 0 0 1 5 1.4Z";

export default function StatusMark({ tone = "info", size = 10, className = "" }) {
  const { shapes } = useSettings();
  const shape = SHAPES[tone] || SHAPES.info;
  const palette = TONE[tone] || TONE.info;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 10 10"
      className={`${palette.text} shrink-0 ${className}`}
      role="img"
      aria-label={shape.label}
    >
      <path d={shapes ? shape.path : DOT} fill="currentColor" />
    </svg>
  );
}
