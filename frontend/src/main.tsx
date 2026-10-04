import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { configureAmplify } from "./auth/amplify";
import App from "./App";
import "./styles/tokens.css";

// Configure Amplify (Cognito Hosted UI) before React renders so the auth
// session is available on first paint.
configureAmplify();

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root not found in index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
