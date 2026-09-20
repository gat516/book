import { publicHref } from "../publicNavigation";
import { SiteFooter, SiteHeader } from "./SiteHeader";

export function PrivacyPage() {
  return <><SiteHeader /><main id="main-content" className="privacy-page"><p className="eyebrow">About this beta</p><h1>Your library, your data.</h1><p>QiReadr is an independent reading project. You can explore the sample without an account. Creating your own library requires an invitation and Google sign-in.</p>
    <h2>What is stored</h2><p>We use your verified Google email and account identifier to sign you in. Your account stores the books and chapters you add, translations, reading progress, story knowledge, and settings. Your AI provider keys are encrypted before storage.</p>
    <h2>Where your text goes</h2><p>When you request translation or AI features, relevant chapter text is sent to the provider you choose. Optional semantic search also sends text to your selected embedding provider. Those providers have their own data policies and usage charges. The public sample uses prepared content and makes no AI calls.</p>
    <h2>Cookies and hosting</h2><p>Essential cookies keep you signed in and protect the login flow. Browser storage remembers reading preferences such as your theme. Cloudflare serves the frontend and proxies requests; AWS hosts account data and processing. Operational records may include request metadata and provider usage counts.</p>
    <h2>Control and deletion</h2><p>Your library and saved keys are private to your account. You can remove keys in Account settings and delete individual books from the library. To close your account, open the Account menu and choose Delete account. Access is revoked immediately; stored content is then queued for deletion. Backups may retain copies until their retention period ends.</p>
    <p className="privacy-date">Updated September 20, 2026.</p><a className="action-link" href={publicHref("/")}>Back to QiReadr →</a>
  </main><SiteFooter /></>;
}
