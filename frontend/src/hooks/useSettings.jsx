import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  applySettings,
  DEFAULT_SETTINGS,
  loadSettings,
  normaliseSettings,
  PALETTES,
  riskColors,
  saveSettings,
  systemDefaults,
} from "../lib/accessibility.js";

const SettingsContext = createContext(null);

export function SettingsProvider({ children }) {
  const [settings, setSettings] = useState(loadSettings);

  useEffect(() => {
    applySettings(settings);
    saveSettings(settings);
  }, [settings]);

  const update = useCallback((patch) => {
    setSettings((previous) => normaliseSettings({ ...previous, ...patch }, previous));
  }, []);

  const reset = useCallback(() => setSettings(systemDefaults()), []);

  const value = useMemo(() => {
    const palette = PALETTES[settings.cvd] || PALETTES.default;
    return {
      settings,
      update,
      reset,
      palette,
      risk: riskColors(palette),
      // Derived flags, so components never re-implement the same comparison.
      reducedMotion: settings.motion === "reduced",
      shapes: settings.shapes,
      isDefault: JSON.stringify(settings) === JSON.stringify(DEFAULT_SETTINGS),
    };
  }, [settings, update, reset]);

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export default function useSettings() {
  const context = useContext(SettingsContext);
  if (!context) throw new Error("useSettings must be used inside <SettingsProvider>");
  return context;
}
