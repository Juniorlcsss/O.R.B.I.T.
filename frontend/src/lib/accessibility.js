/*
 * Accessibility settings for the command center.
 *
 * This console encodes mission-critical meaning in colour — approved / advisory
 * / rejected, satellite versus debris, and the three conjunction risk bands.
 * Green-amber-red is the single worst trio to hand someone with a red-green
 * colour vision deficiency, which is roughly 1 in 12 men. So the palette is
 * swappable, and every colour-coded state can additionally carry a distinct
 * shape (WCAG 2.2 SC 1.4.1, "Use of Color").
 *
 * One source of truth: the palettes below drive both the CSS custom properties
 * the Tailwind theme reads and the Cesium entity colours on the globe.
 */

const STORAGE_KEY = "orbit.a11y.v1";

/*
 * Palettes are chosen along each deficiency's *safe* axis, then verified by
 * simulating dichromatic vision (Vienot, Brettel & Mollon 1999) and measuring
 * the worst pairwise CIE76 dE between the three status colours. Picking these
 * by eye does not work: an early protanopia palette using violet for "alert"
 * measured dE 19, i.e. *worse* than leaving the default palette alone.
 *
 *   preset          default palette   this palette   min contrast on panel
 *   deuteranopia          20               56               5.12:1
 *   protanopia            25               38               5.76:1
 *   tritanopia             6               54               4.73:1
 *
 *   - deuteranopia / protanopia confuse red-green, so status runs
 *     blue -> yellow -> rose/violet.
 *   - tritanopia confuses blue-yellow but keeps red-green, so status runs
 *     green -> yellow-green -> red. Pink is unusable here: it collapses onto
 *     red for a tritanope (dE 5).
 *   - monochrome drops hue entirely and separates purely by lightness, which
 *     is why it forces shape encoding on.
 */
export const PALETTES = {
  default: {
    accent: "#7aa2f7",
    nominal: "#57c489",
    caution: "#d6a445",
    alert: "#e3675c",
    satellite: "#8fd8ff",
    debris: "#e3675c",
  },
  deuteranopia: {
    accent: "#b3a5f0",
    nominal: "#4fa8de",
    caution: "#e8c64a",
    alert: "#df5378",
    satellite: "#7cc7ee",
    debris: "#ef6f9c",
  },
  protanopia: {
    accent: "#9fb8ef",
    nominal: "#4fa8de",
    caution: "#e8c64a",
    alert: "#cf5ce8",
    satellite: "#7cc7ee",
    debris: "#d68df0",
  },
  tritanopia: {
    accent: "#8fd0c4",
    nominal: "#3fbf88",
    caution: "#c9d94a",
    alert: "#e6423f",
    satellite: "#7fe0b8",
    debris: "#e6423f",
  },
  monochrome: {
    accent: "#c7cfdb",
    nominal: "#8b95a3",
    caution: "#c2cad6",
    alert: "#ffffff",
    satellite: "#f2f5fa",
    debris: "#9aa3b1",
  },
};

export const CVD_MODES = [
  { id: "default", label: "Full colour", note: "Standard mission palette" },
  { id: "deuteranopia", label: "Deuteranopia", note: "Red-green — most common" },
  { id: "protanopia", label: "Protanopia", note: "Red-weak" },
  { id: "tritanopia", label: "Tritanopia", note: "Blue-yellow" },
  { id: "monochrome", label: "Monochrome", note: "Lightness only, shapes forced on" },
];

export const SCALES = [
  { id: 0.9, label: "90%" },
  { id: 1, label: "100%" },
  { id: 1.15, label: "115%" },
  { id: 1.3, label: "130%" },
];

export const TYPEFACES = [
  { id: "plex", label: "IBM Plex", note: "Console default" },
  { id: "hyperlegible", label: "Atkinson Hyperlegible", note: "Braille Institute, low-vision" },
];

export const DEFAULT_SETTINGS = {
  cvd: "default",
  contrast: "standard",
  scale: 1,
  typeface: "plex",
  motion: "full",
  shapes: false, // Redundant shape coding alongside colour.
  audio: true,
  announce: true,
  labels: "auto", // Globe label density: "auto" | "all".
};

/** Risk bands map onto the status ramp, so they can never drift apart. */
export function riskColors(palette) {
  return { LOW: palette.nominal, MEDIUM: palette.caution, HIGH: palette.alert };
}

/** `#8fd8ff` -> `"143 216 255"`, the form `rgb(var(--x) / <alpha>)` needs. */
function triplet(hex) {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.replace(/./g, (c) => c + c) : value;
  const int = parseInt(full, 16);
  return `${(int >> 16) & 255} ${(int >> 8) & 255} ${int & 255}`;
}

/** Seed from the operating system so the first paint already suits the user. */
export function systemDefaults() {
  if (typeof window === "undefined" || !window.matchMedia) return DEFAULT_SETTINGS;
  return {
    ...DEFAULT_SETTINGS,
    motion: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "reduced" : "full",
    contrast: window.matchMedia("(prefers-contrast: more)").matches ? "high" : "standard",
  };
}

export function loadSettings() {
  const base = systemDefaults();
  try {
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null");
    // Merge rather than replace: a setting added in a later build still gets
    // its default instead of coming back undefined.
    return stored && typeof stored === "object" ? { ...base, ...stored } : base;
  } catch {
    return base;
  }
}

export function saveSettings(settings) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* private mode, quota, or no storage: settings simply do not persist */
  }
}

/**
 * Push settings onto the document root. Everything downstream — Tailwind
 * utilities, the animation kill-switch, the type scale — keys off these.
 */
export function applySettings(settings) {
  const root = document.documentElement;
  const palette = PALETTES[settings.cvd] || PALETTES.default;

  root.dataset.cvd = settings.cvd;
  root.dataset.contrast = settings.contrast;
  root.dataset.motion = settings.motion;
  root.dataset.typeface = settings.typeface;
  root.style.setProperty("--ui-scale", String(settings.scale));

  root.style.setProperty("--c-accent", triplet(palette.accent));
  root.style.setProperty("--c-nominal", triplet(palette.nominal));
  root.style.setProperty("--c-caution", triplet(palette.caution));
  root.style.setProperty("--c-alert", triplet(palette.alert));
}

/** Monochrome carries no hue, so shape coding is not optional there. */
export function normaliseSettings(next, previous) {
  const settings = { ...next };
  if (settings.cvd === "monochrome") settings.shapes = true;
  else if (settings.cvd !== "default" && previous?.cvd !== settings.cvd) settings.shapes = true;
  return settings;
}
