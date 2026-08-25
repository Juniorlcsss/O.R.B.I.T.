/** @type {import('tailwindcss').Config} */

/*
 * O.R.B.I.T. console design tokens.
 *
 * The palette is deliberately narrow: five neutral surfaces, three text
 * weights, one interactive accent and three status hues. Colour carries
 * meaning here — anything tinted is telling the operator something. Chrome
 * stays grey so the globe and the status rail are the only things that glow.
 *
 * Every semantic colour resolves through a CSS custom property so the
 * accessibility layer can swap the whole palette (colour-vision presets, high
 * contrast) at runtime without a rebuild. The `rgb(var(--x) / <alpha-value>)`
 * form keeps Tailwind's opacity modifiers working, e.g. `border-alert/40`.
 */
const themed = (variable) => `rgb(var(${variable}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Surfaces, darkest (page) to lightest (input / hover).
        ink: {
          900: themed("--c-ink-900"),
          800: themed("--c-ink-800"),
          700: themed("--c-ink-700"),
          600: themed("--c-ink-600"),
          500: themed("--c-ink-500"),
        },
        // Hairlines. Seams between surfaces, never a boxed-in "card border".
        hair: "var(--hair)",
        hairlit: "var(--hairlit)",
        // Text ramp: primary reading, secondary detail, tertiary labels.
        fg: {
          DEFAULT: themed("--c-fg"),
          2: themed("--c-fg-2"),
          3: themed("--c-fg-3"),
        },
        // The single interactive accent. Selection and focus only.
        accent: {
          DEFAULT: themed("--c-accent"),
          soft: "rgb(var(--c-accent) / 0.14)",
        },
        // Status semantics. Used for state, never for decoration.
        nominal: themed("--c-nominal"),
        caution: themed("--c-caution"),
        alert: themed("--c-alert"),
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "Consolas", "monospace"],
      },
      fontSize: {
        // Six steps, nothing in between. Arbitrary px values are a smell.
        // Expressed in rem so the interface-scale setting moves the whole UI.
        "2xs": ["0.625rem", { lineHeight: "0.8125rem", letterSpacing: "0.085em" }],
        xs: ["0.6875rem", { lineHeight: "0.9375rem" }],
        sm: ["0.75rem", { lineHeight: "1.0625rem" }],
        base: ["0.8125rem", { lineHeight: "1.1875rem" }],
        md: ["0.9375rem", { lineHeight: "1.3125rem" }],
        lg: ["1.125rem", { lineHeight: "1.5625rem" }],
      },
      borderRadius: {
        DEFAULT: "3px",
        md: "4px",
        lg: "6px",
      },
      transitionTimingFunction: {
        console: "cubic-bezier(0.22, 0.61, 0.36, 1)",
      },
    },
  },
  plugins: [],
};
