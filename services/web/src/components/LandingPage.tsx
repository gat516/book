import { publicHref } from "../publicNavigation";
import { ArrowRight, BookOpen, Languages, MessageCircle, ShieldCheck, UserRound } from "lucide-react";
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
        <Fade rise={10} className="landing-copy">
          <p className="eyebrow"><span className="eyebrow-line" />A READER FOR WEB NOVELS</p>
          <h1>A good book.<br /><em>A thousand<br />chapters to go.</em></h1>
          <p className="landing-description">Read, translate, and keep track of the cast—without spoilers.</p>
          <div className="hero-actions"><a className="action-link btn-primary" href={publicHref("/demo")}><BookOpen size={17} />Read a sample <ArrowRight size={17} /></a><a className="hero-secondary" href={login}>Open library <ArrowRight size={16} /></a></div>
          <p className="landing-note">No account needed for the sample.</p>
        </Fade>
        <Fade delay={80} rise={12} className="hero-scene">
          <div className="scene-caption"><span>ON THE READING DESK</span><span>VOL. 001</span></div>
          <a className="sample-cover" href={publicHref("/demo")} aria-label="Read The Lantern Keeper sample">
            <span className="cover-kicker">A QIREADR ORIGINAL</span>
            <span className="cover-orbit" aria-hidden="true"><span>灯</span></span>
            <span className="cover-title">The Lantern<br /><em>Keeper</em></span>
            <span className="cover-subtitle">Some lights remember.</span>
            <span className="cover-bottom">THREE CHAPTERS <ArrowRight size={17} /></span>
          </a>
          <div className="scene-character"><div className="scene-card-top"><span className="character-dot">梅</span><div><strong>Mei</strong><span>Character</span></div><UserRound size={16} /></div><p>Mends ropes at the ferry landing.<br />Carries a small copper key.</p><div className="scene-gate"><ShieldCheck size={13} /> Through chapter 1</div></div>
        </Fade>
      </section>
      <section className="feature-strip" aria-label="Made for the way you read">
        <article><div className="feature-top"><Languages size={24} strokeWidth={1.5} /></div><h2>Translate</h2><p>Your choice of model. Consistent names across chapters.</p></article>
        <article><div className="feature-top"><BookOpen size={24} strokeWidth={1.5} /></div><h2>Story wiki</h2><p>People and places, up to the chapter you’re reading.</p></article>
        <article><div className="feature-top"><MessageCircle size={24} strokeWidth={1.5} /></div><h2>Ask your book</h2><p>A quick reminder when you lose track of a detail.</p></article>
      </section>
      <section className="landing-own-library"><div><h2>Start reading</h2><p>Open to the first 100 users. AI usage is billed by your provider.</p></div><a className="action-link" href={login}>Continue with Google <ArrowRight size={17} /></a></section>
    </main><SiteFooter /></>;
}
