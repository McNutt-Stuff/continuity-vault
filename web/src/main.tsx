import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./auth";
import App from "./App";
import { initTheme } from "./theme";
import { injectAnalytics } from "./analytics";
import "./theme.css";

initTheme();

// Load Google Analytics if the node configured a measurement ID (public /api/site).
fetch("/api/site", { credentials: "omit", cache: "no-cache" })
  .then((r) => (r.ok ? r.json() : null))
  .then((d) => injectAnalytics(d?.analytics_id))
  .catch(() => {});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
