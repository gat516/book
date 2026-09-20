import { publicHref } from "../publicNavigation";
import type { ReactNode } from "react";
import { ThemePicker } from "./ThemePicker";
import { BookOpen } from "lucide-react";

export function SiteHeader({ children }: { children?: ReactNode }) {
  return <header className="site-header">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <a className="wordmark" href={publicHref("/")} aria-label="QiReadr home"><span className="brand-mark" aria-hidden="true"><BookOpen size={21} strokeWidth={1.6} /></span>qireadr<span className="brand-beta">PRIVATE BETA</span></a>
    <nav aria-label="Main navigation"><a className="site-demo-link" href={publicHref("/demo")}>Explore demo</a><ThemePicker />{children}</nav>
  </header>;
}

export function SiteFooter() {
  return <footer className="site-footer"><span><BookOpen size={15} aria-hidden="true" /> One chapter at a time.</span><a href={publicHref("/privacy")}>Privacy &amp; your data</a></footer>;
}
