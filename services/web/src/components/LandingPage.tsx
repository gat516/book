import { publicHref } from "../publicNavigation";
import { ArrowDown, ArrowRight, BookOpen, Languages, MessageCircle, ShieldCheck, Sparkles } from "lucide-react";
import { Fade } from "./animate-ui/motion";
import { SiteFooter, SiteHeader } from "./SiteHeader";

export function LandingPage({ error }: { error?: string | null }) {
  const invite = new URLSearchParams(window.location.search).get("invite");
  const login = `/api/auth/login${invite ? `?invite=${encodeURIComponent(invite)}` : ""}`;
  return <><SiteHeader><a className="site-signin" href={login}>Sign in <ArrowRight size={14} /></a></SiteHeader>
    <main id="main-content" className="landing-page">
      {error && <p className="page-notice" role="alert">{error} <button onClick={() => window.location.reload()}>Retry</button></p>}
      {invite && <p className="invitation-notice"><ShieldCheck size={18} /> Your invitation is ready. Continue with the Google account it was sent to. <a href={login}>Accept invitation →</a></p>}
      <section className="landing-hero">
        <Fade className="landing-copy">
          <p className="eyebrow"><span className="eyebrow-line" />FOR THE WORLDS YOU GET LOST IN</p>
          <h1>Stay in the story.<br /><em>We’ll keep track<br className="hero-break" /> of the world.</em></h1>
          <p className="landing-description">A thousand chapters. A hundred names. One reading companion that remembers just as much as you do.</p>
          <div className="hero-actions"><a className="action-link btn-primary" href={publicHref("/demo")}>Step into a story <ArrowRight size={17} /></a><a className="hero-secondary" href={login}>Open your library <ArrowRight size={16} /></a></div>
          <p className="landing-note"><BookOpen size={14} /> Try a short story. No account or API key needed.</p>
        </Fade>
        <Fade delay={120} className="hero-scene">
          <div className="scene-caption"><span>ON THE READING DESK</span><span>VOL. 001</span></div>
          <a className="sample-cover" href={publicHref("/demo")} aria-label="Read The Lantern Keeper sample">
            <span className="cover-kicker">A QIREADR ORIGINAL</span>
            <span className="cover-orbit" aria-hidden="true"><span>灯</span></span>
            <span className="cover-title">The Lantern<br /><em>Keeper</em></span>
            <span className="cover-subtitle">Some lights remember.</span>
            <span className="cover-bottom">THREE CHAPTERS <ArrowRight size={17} /></span>
          </a>
          <div className="scene-character"><div className="scene-card-top"><span className="character-dot">梅</span><div><strong>Mei</strong><span>Character discovered</span></div><Sparkles size={16} /></div><p>Mends ropes at the ferry landing.<br />Carries a small copper key.</p><div className="scene-gate"><ShieldCheck size={13} /> Known through chapter 1</div></div>
          <span className="scene-note">A world that unfolds with you.</span>
        </Fade>
      </section>
      <div className="landing-divider"><span>A little less remembering. A little more reading.</span><ArrowDown size={16} /></div>
      <section className="feature-strip" aria-label="Made for the way you read">
        <article><div className="feature-top"><Languages size={22} strokeWidth={1.5} /><span>01 / READ</span></div><h2>Cross the language barrier.</h2><p>Bring a web novel, choose your AI provider, and translate with names that stay consistent.</p></article>
        <article><div className="feature-top"><BookOpen size={22} strokeWidth={1.5} /><span>02 / REMEMBER</span></div><h2>Meet your living story wiki.</h2><p>People, places, and everything in between. A world guide that grows one chapter at a time.</p></article>
        <article><div className="feature-top"><MessageCircle size={22} strokeWidth={1.5} /><span>03 / ASK</span></div><h2>Curiosity, without spoilers.</h2><p>Forgot how two characters met? Ask your book. Answers stay within the chapters you’ve reached.</p></article>
      </section>
      <section className="landing-own-library"><div><p className="eyebrow">YOUR BOOKS. YOUR LITTLE CORNER.</p><h2>Make room for your next obsession.</h2><p>A private library, your own provider keys, and a companion for the long read.<br />Private beta · New accounts need an invitation · AI usage is billed by your provider.</p></div><a className="action-link" href={login}>Continue with Google <ArrowRight size={17} /></a></section>
    </main><SiteFooter /></>;
}
