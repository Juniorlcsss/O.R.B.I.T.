import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import { SettingsProvider } from "./hooks/useSettings.jsx";
import "./styles/index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <SettingsProvider>
      <App />
    </SettingsProvider>
  </React.StrictMode>
);
