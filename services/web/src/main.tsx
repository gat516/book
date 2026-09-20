import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AuthGate } from "./components/AuthGate";
import { SampleReader } from "./components/SampleReader";
import { PrivacyPage } from "./components/PrivacyPage";
import { LandingPage } from "./components/LandingPage";
import { MotionConfig } from "motion/react";
import "./index.css";
import "./design.css";
import { applyTheme, storedTheme } from "./theme";

applyTheme(storedTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
    {window.location.pathname.replace(/\/$/, "") === "/demo" ? <SampleReader />
      : window.location.pathname.replace(/\/$/, "") === "/privacy" ? <PrivacyPage />
      : window.location.pathname.replace(/\/$/, "") === "/welcome" ? <LandingPage />
      : <AuthGate><App /></AuthGate>}
    </MotionConfig>
  </StrictMode>,
);
