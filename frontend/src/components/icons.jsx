/*
 * A single 16px stroke icon set drawn on one grid with one stroke weight.
 * Everything inherits `currentColor` so tone is decided by the caller, and
 * nothing in the console falls back to a decorative unicode glyph.
 */

function Svg({ size = 14, children, ...rest }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.35"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconActivity = (p) => (
  <Svg {...p}>
    <path d="M1 8h3l2-5 3.5 10L12 8h3" />
  </Svg>
);

export const IconGlobe = (p) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.2" />
    <path d="M1.8 8h12.4M8 1.8c1.7 1.8 2.6 3.9 2.6 6.2S9.7 12.4 8 14.2C6.3 12.4 5.4 10.3 5.4 8S6.3 3.6 8 1.8Z" />
  </Svg>
);

export const IconShield = (p) => (
  <Svg {...p}>
    <path d="M8 1.6 13 3.4v4.2c0 3.2-2 5.6-5 6.8-3-1.2-5-3.6-5-6.8V3.4L8 1.6Z" />
    <path d="M5.9 7.9 7.4 9.4l2.8-3" />
  </Svg>
);

export const IconSatellite = (p) => (
  <Svg {...p}>
    <path d="m5.2 10.8-3 3M4.1 6.6 6.6 4.1l2.6 2.6-2.5 2.5z" />
    <path d="m9.4 1.9 1.6 1.6M12.5 5l1.6 1.6M6.8 9.2l2.4 2.4 2.5-2.5-2.4-2.4" />
  </Svg>
);

export const IconAlert = (p) => (
  <Svg {...p}>
    <path d="M8 2.2 14.6 13.4H1.4L8 2.2Z" />
    <path d="M8 6.4v3.1M8 11.6h.01" />
  </Svg>
);

export const IconChevron = ({ open = false, ...p }) => (
  <Svg {...p} style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform 140ms" }}>
    <path d="m6 3.5 4.5 4.5L6 12.5" />
  </Svg>
);

export const IconClose = (p) => (
  <Svg {...p}>
    <path d="m3.6 3.6 8.8 8.8M12.4 3.6l-8.8 8.8" />
  </Svg>
);

export const IconLock = ({ open = false, ...p }) => (
  <Svg {...p}>
    <rect x="3.2" y="7" width="9.6" height="7" rx="1.4" />
    {open ? <path d="M5.6 7V4.9A2.4 2.4 0 0 1 10 3.7" /> : <path d="M5.6 7V4.9a2.4 2.4 0 0 1 4.8 0V7" />}
  </Svg>
);

export const IconCrosshair = (p) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="5.4" />
    <path d="M8 .9v2.6M8 12.5v2.6M.9 8h2.6M12.5 8h2.6" />
  </Svg>
);

export const IconReset = (p) => (
  <Svg {...p}>
    <path d="M2.4 8a5.6 5.6 0 1 1 1.7 4" />
    <path d="M1.7 14.1v-3.4h3.4" />
  </Svg>
);

export const IconBroadcast = (p) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="1.7" />
    <path d="M4.6 4.6a4.8 4.8 0 0 0 0 6.8M11.4 11.4a4.8 4.8 0 0 0 0-6.8M2.4 2.4a7.9 7.9 0 0 0 0 11.2M13.6 13.6a7.9 7.9 0 0 0 0-11.2" />
  </Svg>
);

export const IconSettings = (p) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="2.2" />
    <path d="M8 1.2v1.9M8 12.9v1.9M1.2 8h1.9M12.9 8h1.9M3.2 3.2l1.35 1.35M11.45 11.45l1.35 1.35M12.8 3.2l-1.35 1.35M4.55 11.45L3.2 12.8" />
  </Svg>
);
