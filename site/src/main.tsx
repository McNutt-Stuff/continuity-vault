import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { applyInlineCms } from "./cms";
import "./theme.css";

// Apply content the node inlined into index.html before the first render, so
// the page paints real CMS content with no runtime query.
applyInlineCms();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
