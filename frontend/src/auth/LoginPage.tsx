import type { CSSProperties, FormEvent, ReactNode } from "react";
import { useCallback, useState } from "react";
import { Navigate, useLocation } from "react-router";

import {
  UI_LANGUAGE_LABELS,
  type UILanguage,
  useT,
  useUILanguage,
} from "../lib/i18n";
import { useAuth } from "./AuthState";
import LoginMobileV2 from "./LoginMobileV2";

interface LoginLocationState {
  next?: string;
}

type LoginTheme = "blue" | "green";

const SUPPORTED_LANGUAGES: readonly UILanguage[] = ["en", "zh", "ja"];
const SUPPORTED_THEMES: readonly LoginTheme[] = ["blue", "green"];

interface ThemeTokens {
  primary: string;
  primaryHover: string;
  primarySoft: string;
  primarySurface: string;
  primaryRing: string;
  mountain: string;
  plant: string;
}

const THEME_PALETTE: Record<LoginTheme, ThemeTokens> = {
  blue: {
    primary: "#1d4ed8",
    primaryHover: "#1e40af",
    primarySoft: "#bfdbfe",
    primarySurface: "#eef4ff",
    primaryRing: "rgba(29, 78, 216, 0.14)",
    mountain: "#64748b",
    plant: "#9aa9bd",
  },
  green: {
    primary: "#2f5d3a",
    primaryHover: "#264c30",
    primarySoft: "#c5d2c3",
    primarySurface: "#f0ece1",
    primaryRing: "rgba(47, 93, 58, 0.12)",
    mountain: "#5d6358",
    plant: "#a8b09d",
  },
};

function readEnvDefaultTheme(): LoginTheme {
  const env = (import.meta as unknown as { env?: Record<string, unknown> })
    .env;
  const raw = String(env?.VITE_LOGIN_THEME ?? "").toLowerCase();
  return SUPPORTED_THEMES.includes(raw as LoginTheme)
    ? (raw as LoginTheme)
    : "green";
}

export default function LoginPage() {
  const { user, isLoading, login } = useAuth();
  const location = useLocation();
  const state = location.state as LoginLocationState | null;
  const nextPath = state?.next ?? "/";
  const t = useT();
  const [lang, setLang] = useUILanguage();

  // Theme is the env default (green) — no URL or localStorage
  // override. Earlier builds persisted the chosen theme into both
  // ``?theme=`` URL and ``localStorage.v2``; PR-221 dropped the
  // URL side, but returning users with ``localStorage.v2 = "blue"``
  // (written before the switcher was removed) would still get
  // pinned to blue. Drop the localStorage read too so the default
  // wins for everyone, no matter what state their browser is in.
  const [theme] = useState<LoginTheme>(readEnvDefaultTheme());
  const [isMobileNavOpen, setIsMobileNavOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // ``errorKey`` is an i18n key under ``login.error.*`` so the message
  // stays in sync with the language switcher (codex Q5/Q8 amendment).
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [retryAfterSeconds, setRetryAfterSeconds] = useState<number | null>(
    null,
  );
  const [comingSoonOpen, setComingSoonOpen] = useState(false);

  const openComingSoon = useCallback(() => {
    setComingSoonOpen(true);
  }, []);

  if (!isLoading && user) {
    return <Navigate to={nextPath} replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting || username.length === 0 || password.length === 0) return;
    setSubmitting(true);
    setErrorKey(null);
    setRetryAfterSeconds(null);
    const result = await login(username, password);
    if (result.ok) {
      // ``user`` becomes truthy on the next render → the early
      // ``Navigate`` guard above handles the redirect; no manual
      // navigation here.
      return;
    }
    setSubmitting(false);
    if (result.code === "rate_limited") {
      setErrorKey("login.error.rate_limited");
      setRetryAfterSeconds(result.retryAfterSeconds ?? null);
    } else if (result.code === "invalid_credentials") {
      setErrorKey("login.error.invalid_credentials");
    } else {
      setErrorKey("login.error.network");
    }
  }

  const tokens = THEME_PALETTE[theme];
  const cssVars: CSSProperties = {
    ["--brand" as string]: tokens.primary,
    ["--brand-hover" as string]: tokens.primaryHover,
    ["--brand-ring" as string]: tokens.primaryRing,
    ["--color-bg" as string]: "#f6f3eb",
    ["--color-bg-elevated" as string]: "#ffffff",
    ["--color-surface-2" as string]: tokens.primarySurface,
    ["--color-primary" as string]: tokens.primary,
    ["--color-primary-hover" as string]: tokens.primaryHover,
    ["--color-primary-soft" as string]: tokens.primarySoft,
    ["--color-primary-ring" as string]: tokens.primaryRing,
    ["--color-accent-gold" as string]: "#b59b66",
    ["--color-seal-red" as string]: "#b3322d",
    ["--color-text" as string]: "#1f2a24",
    ["--color-text-muted" as string]: "#6b7268",
    ["--color-text-subtle" as string]: "#97998f",
    ["--color-border" as string]: "#e5e1d3",
    ["--color-border-strong" as string]: "#d6d2c2",
    ["--color-mountain" as string]: tokens.mountain,
    ["--color-plant" as string]: tokens.plant,
    ["--shadow-card" as string]: "0 6px 24px rgba(46, 64, 48, 0.08)",
    ["--shadow-soft" as string]: "0 2px 8px rgba(46, 64, 48, 0.05)",
  };

  // Translate errorKey + retryAfterSeconds → single string for the
  // mobile port (LoginMobileV2 takes a plain string; desktop still
  // formats inline below).
  const errorMessage = errorKey
    ? errorKey === "login.error.rate_limited" &&
      retryAfterSeconds &&
      retryAfterSeconds > 0
      ? t(errorKey).replace(
          "{minutes}",
          String(Math.max(1, Math.ceil(retryAfterSeconds / 60))),
        )
      : t(errorKey)
    : null;

  return (
    <>
      {/* xs / sm / md: 1:1 port of the user's design slice
          (`frontend/_login_design_ref/v2-zh-slice`). Absolute layout
          + fixed px values + isolated CSS module — see
          ``LoginMobileV2.tsx``. */}
      <div className="lg:hidden">
        <LoginMobileV2
          username={username}
          password={password}
          submitting={submitting}
          errorMessage={errorMessage}
          onUsernameChange={setUsername}
          onPasswordChange={setPassword}
          onSubmit={(e) => void handleSubmit(e)}
          onComingSoon={openComingSoon}
          onMenuToggle={() => setIsMobileNavOpen((v) => !v)}
          isMenuOpen={isMobileNavOpen}
        />
        {comingSoonOpen ? (
          <ComingSoonDialog
            title={t("login.coming_soon.title")}
            body={t("login.coming_soon.body")}
            dismissLabel={t("login.coming_soon.dismiss")}
            onDismiss={() => setComingSoonOpen(false)}
          />
        ) : null}
      </div>

      {/* lg+: original desktop layout (user said "PC OK, just swap
          bg by language"). Drop the Chinese-only DecorativePlant +
          DecorativeMountain SVG — the per-language watercolor PNG
          carries the same role and changes with `lang`. Keep
          DecorativeCalligraphy (text element, drives via i18n). */}
      <div
        className="hidden lg:block"
      >
        <div
          style={cssVars}
          data-theme={theme}
          data-testid="login-page-root"
          className="relative min-h-screen overflow-x-hidden bg-[var(--color-bg)] font-sans text-[var(--color-text)]"
        >
      <img
        src={`/login-bg/bg-${lang}.png`}
        alt=""
        aria-hidden
        data-testid={`login-bg-${lang}`}
        className="pointer-events-none fixed inset-0 z-0 h-screen w-screen object-cover object-center opacity-90"
      />
      <DecorativeCalligraphy
        lineOne={t("login.decor.calligraphy_line_1")}
        lineTwo={t("login.decor.calligraphy_line_2")}
        sealTop={t("login.decor.seal_top")}
        sealBottom={t("login.decor.seal_bottom")}
      />

      <header className="relative z-10 mx-auto flex max-w-[1280px] items-center gap-4 px-5 py-5 md:px-8 lg:px-12">
        <a
          href="#top"
          className="flex min-w-0 flex-1 items-center gap-3 no-underline lg:flex-none"
          aria-label={t("login.aria.home")}
          data-testid="login-brand-link"
        >
          <LogoMark />
          <span className="flex min-w-0 flex-col leading-none">
            <span className="truncate font-serif text-[1.35rem] font-bold text-[var(--color-text)]">
              {t("login.brand.name")}
            </span>
            <span className="mt-1 hidden text-[0.6rem] font-semibold uppercase tracking-[0.18em] text-[var(--color-text-subtle)] sm:block">
              {t("login.brand.tagline")}
            </span>
          </span>
        </a>

        <nav
          className="hidden flex-1 justify-center lg:flex"
          aria-label={t("login.aria.primary_navigation")}
        >
          <ul className="flex items-center gap-9">
            <li>
              <a
                className="relative py-1 text-[0.95rem] font-medium text-[var(--color-primary)] after:absolute after:bottom-[-4px] after:left-0 after:right-0 after:h-0.5 after:bg-[var(--color-primary)]"
                href="#top"
              >
                {t("login.nav.home")}
              </a>
            </li>
            <li>
              <HeaderNavLink href="#features" testid="login-nav-features">
                {t("login.nav.features")}
              </HeaderNavLink>
            </li>
            <li>
              <HeaderNavLink onClick={openComingSoon} testid="login-nav-pricing">
                {t("login.nav.pricing")}
              </HeaderNavLink>
            </li>
            <li>
              <HeaderNavLink
                onClick={openComingSoon}
                withCaret
                testid="login-nav-solutions"
              >
                {t("login.nav.solutions")}
              </HeaderNavLink>
            </li>
            <li>
              <HeaderNavLink
                onClick={openComingSoon}
                withCaret
                testid="login-nav-resources"
              >
                {t("login.nav.resources")}
              </HeaderNavLink>
            </li>
            <li>
              <HeaderNavLink href="#about" testid="login-nav-about">
                {t("login.nav.about")}
              </HeaderNavLink>
            </li>
          </ul>
        </nav>

        <div className="flex items-center gap-2 sm:gap-3">
          <LanguageSwitcher current={lang} onChange={setLang} />
          {/* On xs the mobile drawer carries its own large Sign Up
              button, so hiding the header copy avoids the duplicate
              the user reported. Visible from sm+ where the drawer
              is hidden. */}
          <button
            type="button"
            data-testid="login-header-signup-button"
            onClick={openComingSoon}
            className="hidden min-h-9 items-center justify-center whitespace-nowrap rounded-md bg-[var(--color-primary)] px-3 py-2 text-sm font-medium leading-none text-white transition hover:bg-[var(--color-primary-hover)] sm:inline-flex sm:px-[18px]"
          >
            {t("login.cta.signup")}
          </button>
        </div>

        <button
          type="button"
          className="ml-auto inline-flex h-9 w-9 flex-col items-center justify-center gap-[5px] sm:hidden"
          aria-controls="login-mobile-nav"
          aria-expanded={isMobileNavOpen}
          aria-label={t(
            isMobileNavOpen ? "login.aria.close_menu" : "login.aria.mobile_menu",
          )}
          onClick={() => setIsMobileNavOpen((current) => !current)}
        >
          <span
            className={`block h-0.5 w-[22px] bg-[var(--color-primary)] transition ${
              isMobileNavOpen ? "translate-y-[7px] rotate-45" : ""
            }`}
          />
          <span
            className={`block h-0.5 w-[22px] bg-[var(--color-primary)] transition ${
              isMobileNavOpen ? "opacity-0" : ""
            }`}
          />
          <span
            className={`block h-0.5 w-[22px] bg-[var(--color-primary)] transition ${
              isMobileNavOpen ? "-translate-y-[7px] -rotate-45" : ""
            }`}
          />
        </button>
      </header>

      <div
        id="login-mobile-nav"
        hidden={!isMobileNavOpen}
        className="fixed inset-x-0 top-20 z-20 bg-[var(--color-bg-elevated)] px-5 py-5 [box-shadow:var(--shadow-card)] sm:hidden"
      >
        <nav
          className="grid gap-1"
          aria-label={t("login.aria.mobile_navigation")}
        >
          {(
            [
              { kind: "anchor", href: "#top", label: t("login.nav.home") },
              {
                kind: "anchor",
                href: "#features",
                label: t("login.nav.features"),
              },
              {
                kind: "coming_soon",
                key: "pricing",
                label: t("login.nav.pricing"),
              },
              {
                kind: "coming_soon",
                key: "solutions",
                label: t("login.nav.solutions"),
              },
              {
                kind: "coming_soon",
                key: "resources",
                label: t("login.nav.resources"),
              },
              { kind: "anchor", href: "#about", label: t("login.nav.about") },
            ] as const
          ).map((entry) =>
            entry.kind === "anchor" ? (
              <a
                key={entry.href}
                href={entry.href}
                className="border-b border-[var(--color-border)] px-1 py-2.5 text-base text-[var(--color-text)] no-underline"
                onClick={() => setIsMobileNavOpen(false)}
              >
                {entry.label}
              </a>
            ) : (
              <button
                key={entry.key}
                type="button"
                className="border-b border-[var(--color-border)] bg-transparent px-1 py-2.5 text-left text-base text-[var(--color-text)]"
                onClick={() => {
                  setIsMobileNavOpen(false);
                  openComingSoon();
                }}
              >
                {entry.label}
              </button>
            ),
          )}
        </nav>
        <div className="mt-4 grid gap-3 border-t border-[var(--color-border)] pt-4">
          <div className="flex flex-wrap gap-2">
            {SUPPORTED_LANGUAGES.map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => setLang(code)}
                aria-pressed={lang === code}
                className={`min-h-9 rounded-md px-3 py-1 text-sm font-medium ${
                  lang === code
                    ? "bg-[var(--color-primary)] text-white"
                    : "bg-[var(--color-surface-2)] text-[var(--color-text)]"
                }`}
              >
                {UI_LANGUAGE_LABELS[code]}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => {
              setIsMobileNavOpen(false);
              openComingSoon();
            }}
            className="inline-flex min-h-11 items-center justify-center rounded-xl bg-[var(--color-primary)] px-5 py-3 text-base font-medium text-white"
          >
            {t("login.cta.signup")}
          </button>
        </div>
      </div>

      <main
        id="top"
        className="relative z-10 mx-auto max-w-[1280px] px-5 pb-16 pt-0 md:max-w-[720px] md:px-8 md:pt-6 lg:max-w-[1280px] lg:px-12 lg:pb-20 lg:pt-12"
      >
        <section
          className="grid grid-cols-1 items-start gap-12 pb-6 pt-0 md:gap-16 md:py-6 lg:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)] lg:items-center lg:gap-16 lg:py-12"
          aria-labelledby="login-hero-headline"
        >
          {/* PR-A (mobile aesthetic, codex round-1 AGREE-w-amend):
              hero is the secondary element on `/login` — the user
              came to log in, not to read the headline. mobile-only
              changes:
                - left-aligned (no more text-center / mx-auto /
                  justify-center; the previous 4-round center↔left
                  flip-flop proved center has no anchor here)
                - title cap 2rem (32px) so the login card on the
                  right of `lg+` and below on xs/sm wins as the
                  visual focal point
                - decoration line w-12 to track the smaller title
              lg+ keeps the original 60px serif clamp + 16px gold
              line because it's a 2-column layout where the hero
              earns its weight balancing the login card. */}
          <div className="relative">
            <div className="mb-4 flex items-center gap-2.5 text-[var(--color-accent-gold)]">
              <LeafSprig />
              <span
                className="h-px w-12 bg-[var(--color-accent-gold)] lg:w-16"
                aria-hidden
              />
            </div>
            <h1
              id="login-hero-headline"
              className="font-serif text-[clamp(1.5rem,5vw,2rem)] font-bold leading-[1.25] tracking-normal text-[var(--color-text)] lg:text-[clamp(2rem,4.4vw,3.75rem)] lg:leading-[1.15]"
              data-testid="login-hero-headline"
            >
              {/* Force the design's two-line layout: line 1 =
                  "Ancient Wisdom,", line 2 = "Modern Writing.".
                  Each line wrapped in `whitespace-nowrap` so the
                  hero column can never break a line in the middle
                  of a phrase even at narrow lg breakpoints (which
                  caused the "Ancient / Wisdom, / Modern / Writing."
                  4-line wrap user reported). */}
              <span className="block whitespace-nowrap">
                {t("login.hero.title_pre")}
              </span>
              <span className="block whitespace-nowrap">
                {t("login.hero.title_main")}
                {/* CJK scripts don't separate words with spaces, so
                    only emit the join-space for languages that do.
                    Without this, zh/ja render "现代 写作" / "現代の 執筆"
                    with a visible gap inside the headline. */}
                {lang === "en" ? " " : null}
                <span className="text-[var(--color-primary)]">
                  {t("login.hero.title_accent")}
                </span>
              </span>
            </h1>
            {/* 32ch was sized for English; the Chinese tagline (38
                chars) wrapped to 3 lines and orphaned `多` on the
                last line. Bumped to 42ch — wide enough for CJK on
                2 lines, English stays at the same wrap shape. */}
            <p className="mt-4 max-w-[42ch] text-[0.95rem] leading-[1.55] text-[var(--color-text)] lg:mt-6 lg:text-[1.05rem] lg:leading-[1.6]">
              {t("login.hero.tagline")}
            </p>
          </div>

          <section
            className="relative rounded-[18px] border border-[var(--color-border)] bg-white/85 px-6 pb-6 pt-8 backdrop-blur-[6px] [box-shadow:var(--shadow-card)] sm:px-8"
            aria-labelledby="login-card-title"
            data-testid="login-card"
          >
            <div className="absolute left-1/2 top-[-22px] grid h-11 w-11 -translate-x-1/2 place-items-center rounded-full bg-[var(--color-bg)]">
              <SproutIcon />
            </div>
            <h2
              id="login-card-title"
              className="text-center font-serif text-[1.6rem] font-bold leading-tight text-[var(--color-primary)]"
            >
              {t("login.card.welcome_back")}
            </h2>
            <p className="mb-6 mt-1 text-center text-sm text-[var(--color-text-muted)]">
              {t("login.card.subtitle")}
            </p>

            <form
              className="flex flex-col gap-3"
              onSubmit={handleSubmit}
              data-testid="login-form"
              noValidate
            >
              <FormField
                icon={<UserGlyph />}
                type="text"
                name="username"
                autoComplete="username"
                placeholder={t("login.input.username_placeholder")}
                testid="login-username-input"
                value={username}
                onChange={setUsername}
                disabled={submitting}
              />
              <FormField
                icon={<LockGlyph />}
                type="password"
                name="password"
                autoComplete="current-password"
                placeholder={t("login.input.password_placeholder")}
                testid="login-password-input"
                value={password}
                onChange={setPassword}
                disabled={submitting}
                showPasswordLabel={t("login.input.show_password")}
                hidePasswordLabel={t("login.input.hide_password")}
              />

              <div className="mb-2 mt-1 flex items-center justify-between gap-3 text-sm">
                <label className="inline-flex min-w-0 cursor-pointer items-center gap-2 text-[0.85rem] text-[var(--color-text-muted)]">
                  <input
                    type="checkbox"
                    name="remember"
                    className="h-4 w-4 rounded border-[var(--color-border-strong)] bg-white"
                    style={{ accentColor: "var(--color-primary)" }}
                    data-testid="login-remember-checkbox"
                  />
                  <span>{t("login.input.remember_me")}</span>
                </label>
                <button
                  type="button"
                  onClick={openComingSoon}
                  className="shrink-0 text-[0.85rem] font-medium text-[var(--color-primary)] transition hover:text-[var(--color-primary-hover)]"
                  data-testid="login-forgot-button"
                >
                  {t("login.input.forgot_password")}
                </button>
              </div>

              {errorKey ? (
                <div
                  role="alert"
                  data-testid="login-error-message"
                  className="rounded-xl border border-[#f0c0c0] bg-[#fdecec] px-3 py-2 text-sm text-[#922323]"
                >
                  {errorKey === "login.error.rate_limited" &&
                  retryAfterSeconds && retryAfterSeconds > 0
                    ? t(errorKey).replace(
                        "{minutes}",
                        String(Math.max(1, Math.ceil(retryAfterSeconds / 60))),
                      )
                    : t(errorKey)}
                </div>
              ) : null}

              <button
                type="submit"
                data-testid="login-primary-submit-button"
                disabled={
                  submitting ||
                  username.length === 0 ||
                  password.length === 0
                }
                className="inline-flex min-h-12 w-full items-center justify-center rounded-xl bg-[var(--color-primary)] px-5 py-3 text-base font-medium leading-none text-white transition hover:bg-[var(--color-primary-hover)] active:translate-y-px disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {submitting ? t("login.cta.signing_in") : t("login.cta.sign_in")}
              </button>

              <div className="my-3 flex items-center gap-3 text-[0.85rem] text-[var(--color-text-subtle)]">
                <span className="h-px flex-1 bg-[var(--color-border)]" />
                <span>{t("login.divider.or_continue")}</span>
                <span className="h-px flex-1 bg-[var(--color-border)]" />
              </div>

              <div className="grid grid-cols-3 gap-3">
                <OAuthButton
                  onClick={openComingSoon}
                  ariaLabel={t("login.oauth.google")}
                  testid="login-oauth-google-button"
                >
                  <GoogleIcon />
                </OAuthButton>
                <OAuthButton
                  onClick={openComingSoon}
                  ariaLabel={t("login.oauth.microsoft")}
                  testid="login-oauth-microsoft-button"
                >
                  <MicrosoftIcon />
                </OAuthButton>
                <OAuthButton
                  onClick={openComingSoon}
                  ariaLabel={t("login.oauth.sso")}
                  testid="login-oauth-sso-button"
                >
                  <InstitutionIcon />
                </OAuthButton>
              </div>
            </form>
          </section>
        </section>

        <section
          id="features"
          className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-5 lg:mt-12 lg:grid-cols-4"
          aria-label={t("login.aria.features")}
        >
          <FeatureCard
            icon={<FeatureFeather />}
            title={t("login.feature.ai_writing.title")}
            blurb={t("login.feature.ai_writing.blurb")}
            testid="login-feature-ai-writing"
          />
          <FeatureCard
            icon={<FeatureResearch />}
            title={t("login.feature.research.title")}
            blurb={t("login.feature.research.blurb")}
            testid="login-feature-research"
          />
          <FeatureCard
            icon={<FeatureMultilingual />}
            title={t("login.feature.multilingual.title")}
            blurb={t("login.feature.multilingual.blurb")}
            testid="login-feature-multilingual"
          />
          <FeatureCard
            icon={<FeatureBooks />}
            title={t("login.feature.knowledge.title")}
            blurb={t("login.feature.knowledge.blurb")}
            testid="login-feature-knowledge"
          />
        </section>

        <section
          id="about"
          className="mt-12 grid gap-6 rounded-2xl border border-[var(--color-border)] bg-white/70 px-6 py-8 [box-shadow:var(--shadow-soft)] md:px-10 md:py-10 lg:mt-16"
          aria-labelledby="login-about-heading"
          data-testid="login-about-section"
        >
          <h2
            id="login-about-heading"
            className="font-serif text-2xl font-bold text-[var(--color-primary)] md:text-3xl"
          >
            {t("login.about.heading")}
          </h2>
          <p className="max-w-[68ch] text-[1.02rem] leading-[1.7] text-[var(--color-text)]">
            {t("login.about.body")}
          </p>
        </section>
      </main>

      {comingSoonOpen ? (
        <ComingSoonDialog
          title={t("login.coming_soon.title")}
          body={t("login.coming_soon.body")}
          dismissLabel={t("login.coming_soon.dismiss")}
          onDismiss={() => setComingSoonOpen(false)}
        />
      ) : null}
        </div>
      </div>
    </>
  );
}

function ComingSoonDialog(props: {
  title: string;
  body: string;
  dismissLabel: string;
  onDismiss: () => void;
}) {
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="coming-soon-title"
      aria-describedby="coming-soon-body"
      data-testid="login-coming-soon-dialog"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={(event) => {
        if (event.target === event.currentTarget) props.onDismiss();
      }}
    >
      <div
        className="w-full max-w-sm rounded-2xl bg-white px-6 py-6 [box-shadow:var(--shadow-card)]"
      >
        <h2
          id="coming-soon-title"
          className="font-serif text-xl font-bold text-[var(--color-primary)]"
        >
          {props.title}
        </h2>
        <p
          id="coming-soon-body"
          className="mt-3 text-sm leading-relaxed text-[var(--color-text)]"
        >
          {props.body}
        </p>
        <div className="mt-6 flex justify-end">
          <button
            type="button"
            data-testid="login-coming-soon-dismiss"
            onClick={props.onDismiss}
            autoFocus
            className="inline-flex min-h-10 items-center justify-center rounded-xl bg-[var(--color-primary)] px-5 py-2 text-sm font-medium text-white transition hover:bg-[var(--color-primary-hover)]"
          >
            {props.dismissLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function HeaderNavLink(props: {
  children: ReactNode;
  href?: string;
  onClick?: () => void;
  withCaret?: boolean;
  testid?: string;
}) {
  const className =
    "py-1 text-[0.95rem] font-medium text-[var(--color-text)] transition hover:text-[var(--color-primary)] bg-transparent border-0 cursor-pointer";
  const inner = (
    <>
      {props.children}
      {props.withCaret ? (
        <span className="ml-1 text-[0.7em] text-[var(--color-text-muted)]">
          ▾
        </span>
      ) : null}
    </>
  );
  if (props.onClick) {
    return (
      <button
        type="button"
        onClick={props.onClick}
        className={className}
        data-testid={props.testid}
      >
        {inner}
      </button>
    );
  }
  return (
    <a className={className} href={props.href} data-testid={props.testid}>
      {inner}
    </a>
  );
}

function LanguageSwitcher(props: {
  current: UILanguage;
  onChange: (lang: UILanguage) => void;
}) {
  const t = useT();
  return (
    <div
      className="inline-flex rounded-md border border-transparent bg-white/50 p-0.5"
      aria-label={t("login.aria.language_switcher")}
    >
      {SUPPORTED_LANGUAGES.map((code) => (
        <button
          key={code}
          type="button"
          onClick={() => props.onChange(code)}
          aria-pressed={props.current === code}
          data-testid={`login-lang-${code}`}
          className={`min-h-8 rounded-md px-2.5 py-1 text-xs font-medium transition ${
            props.current === code
              ? "bg-[var(--color-primary)] text-white"
              : "text-[var(--color-text)] hover:bg-[var(--color-surface-2)]"
          }`}
        >
          {UI_LANGUAGE_LABELS[code]}
        </button>
      ))}
    </div>
  );
}

function FormField(props: {
  icon: ReactNode;
  type: "text" | "password";
  name: string;
  autoComplete: string;
  placeholder: string;
  testid: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  showPasswordLabel?: string;
  hidePasswordLabel?: string;
}) {
  const [isPasswordVisible, setIsPasswordVisible] = useState(false);
  const isPassword = props.type === "password";
  const inputType = isPassword && isPasswordVisible ? "text" : props.type;

  return (
    <label className="flex min-h-12 items-center gap-2.5 rounded-xl border border-[var(--color-border)] bg-white px-3.5 py-3 transition focus-within:border-[var(--color-primary-soft)] focus-within:[box-shadow:0_0_0_3px_var(--color-primary-ring)]">
      <span className="text-[var(--color-text-subtle)]" aria-hidden>
        {props.icon}
      </span>
      <input
        type={inputType}
        name={props.name}
        autoComplete={props.autoComplete}
        placeholder={props.placeholder}
        data-testid={props.testid}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        disabled={props.disabled}
        className="min-w-0 flex-1 bg-transparent text-sm text-[var(--color-text)] outline-none placeholder:text-[var(--color-text-subtle)] disabled:opacity-60"
      />
      {isPassword ? (
        <button
          type="button"
          className="text-[var(--color-text-subtle)] transition hover:text-[var(--color-primary)]"
          aria-label={
            isPasswordVisible ? props.hidePasswordLabel : props.showPasswordLabel
          }
          onClick={() => setIsPasswordVisible((current) => !current)}
        >
          <EyeIcon isVisible={isPasswordVisible} />
        </button>
      ) : null}
    </label>
  );
}

function OAuthButton(props: {
  onClick: () => void;
  ariaLabel: string;
  testid: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-label={props.ariaLabel}
      data-testid={props.testid}
      className="inline-flex min-h-12 items-center justify-center rounded-xl border border-[var(--color-border)] bg-white transition hover:border-[var(--color-primary-soft)] active:translate-y-px"
    >
      {props.children}
    </button>
  );
}

function FeatureCard(props: {
  icon: ReactNode;
  title: string;
  blurb: string;
  testid: string;
}) {
  return (
    <article
      data-testid={props.testid}
      className="grid grid-cols-[56px_minmax(0,1fr)_16px] items-center gap-4 rounded-xl border border-[var(--color-border)] bg-white/60 p-4 transition hover:-translate-y-0.5 hover:border-[var(--color-primary-soft)] hover:[box-shadow:var(--shadow-card)] lg:grid-cols-1 lg:items-start lg:p-6"
    >
      <span
        aria-hidden
        className="grid h-14 w-14 place-items-center rounded-full bg-[var(--color-surface-2)]"
      >
        {props.icon}
      </span>
      <span className="flex min-w-0 flex-col gap-1">
        <h3 className="font-serif text-[1.1rem] font-bold leading-tight text-[var(--color-text)]">
          {props.title}
        </h3>
        <p className="text-[0.85rem] leading-[1.45] text-[var(--color-text-muted)]">
          {props.blurb}
        </p>
      </span>
      <span
        className="text-xl text-[var(--color-text-subtle)] lg:hidden"
        aria-hidden
      >
        ›
      </span>
    </article>
  );
}

function DecorativeCalligraphy(props: {
  lineOne: string;
  lineTwo: string;
  sealTop: string;
  sealBottom: string;
}) {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute right-4 top-[140px] z-[2] flex items-start gap-1.5 md:right-8 md:top-[150px] lg:right-16 lg:top-[170px]"
    >
      <span
        className="font-serif text-[0.95rem] text-[var(--color-text)] opacity-70 md:text-[1.05rem] lg:text-[1.15rem]"
        style={{ writingMode: "vertical-rl", letterSpacing: "0.6em" }}
      >
        {props.lineOne}
      </span>
      <span
        className="font-serif text-[0.95rem] text-[var(--color-text)] opacity-70 md:text-[1.05rem] lg:text-[1.15rem]"
        style={{ writingMode: "vertical-rl", letterSpacing: "0.6em" }}
      >
        {props.lineTwo}
      </span>
      <span className="ml-1 mt-1.5 flex h-7 w-7 flex-col items-center justify-center rounded-[2px] bg-[var(--color-seal-red)] text-[0.58rem] font-bold leading-none text-white lg:h-8 lg:w-8">
        <span>{props.sealTop}</span>
        <span>{props.sealBottom}</span>
      </span>
    </div>
  );
}

function LogoMark() {
  return (
    <svg
      aria-hidden
      className="h-11 w-11 shrink-0"
      viewBox="0 0 64 64"
      fill="none"
    >
      <g
        stroke="var(--color-accent-gold)"
        strokeWidth="1.4"
        strokeLinecap="round"
      >
        <path d="M6 50 Q12 38 16 32" />
        <path d="M52 50 Q46 38 42 32" />
        <ellipse
          cx="10"
          cy="44"
          rx="3"
          ry="1.6"
          fill="var(--color-accent-gold)"
          transform="rotate(-30 10 44)"
        />
        <ellipse
          cx="14"
          cy="38"
          rx="3"
          ry="1.6"
          fill="var(--color-accent-gold)"
          transform="rotate(-30 14 38)"
        />
        <ellipse
          cx="48"
          cy="44"
          rx="3"
          ry="1.6"
          fill="var(--color-accent-gold)"
          transform="rotate(30 48 44)"
        />
        <ellipse
          cx="44"
          cy="38"
          rx="3"
          ry="1.6"
          fill="var(--color-accent-gold)"
          transform="rotate(30 44 38)"
        />
      </g>
      <g stroke="var(--color-primary)" strokeWidth="1.6" fill="none">
        <path d="M16 50 H48" />
        <path d="M14 46 H50" />
        <path d="M18 46 V22" />
        <path d="M24 46 V22" />
        <path d="M32 46 V22" />
        <path d="M40 46 V22" />
        <path d="M46 46 V22" />
        <path d="M14 22 H50" />
        <path d="M16 18 L32 10 L48 18 Z" />
      </g>
    </svg>
  );
}

function LeafSprig() {
  return (
    <svg
      aria-hidden
      className="h-4 w-10"
      viewBox="0 0 40 16"
      fill="none"
    >
      <g
        stroke="currentColor"
        strokeWidth="1.2"
        fill="currentColor"
        opacity="0.85"
      >
        <ellipse
          cx="8"
          cy="8"
          rx="6"
          ry="2.5"
          transform="rotate(-15 8 8)"
        />
        <ellipse
          cx="20"
          cy="6"
          rx="6"
          ry="2.2"
          transform="rotate(-5 20 6)"
        />
        <ellipse
          cx="32"
          cy="8"
          rx="6"
          ry="2.5"
          transform="rotate(15 32 8)"
        />
      </g>
    </svg>
  );
}

function SproutIcon() {
  return (
    <svg aria-hidden className="h-7 w-7" viewBox="0 0 32 32" fill="none">
      <path
        d="M16 28 V14"
        stroke="var(--color-primary)"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path
        d="M16 18 C 10 18 6 14 6 8 C 12 8 16 12 16 18 Z"
        fill="var(--color-primary)"
        opacity="0.85"
      />
      <path
        d="M16 14 C 22 14 26 10 26 4 C 20 4 16 8 16 14 Z"
        fill="var(--color-primary)"
        opacity="0.65"
      />
    </svg>
  );
}

function UserGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
    >
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c1.5-4 5-6 8-6s6.5 2 8 6" />
    </svg>
  );
}

function LockGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
    >
      <rect x="4" y="10" width="16" height="11" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

function EyeIcon(props: { isVisible: boolean }) {
  return (
    <svg
      aria-hidden
      className="h-[18px] w-[18px]"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
    >
      <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {props.isVisible ? null : <line x1="4" y1="20" x2="20" y2="4" />}
    </svg>
  );
}

function GoogleIcon() {
  return (
    <svg aria-hidden viewBox="0 0 24 24" className="h-[22px] w-[22px]">
      <path
        fill="#EA4335"
        d="M12 5c1.6 0 3 .6 4.1 1.6l3-3C17.2 1.7 14.8.8 12 .8 7.4.8 3.5 3.4 1.6 7.2l3.5 2.7C6 7.1 8.7 5 12 5z"
      />
      <path
        fill="#34A853"
        d="M12 19c-3.3 0-6-2.1-6.9-4.9l-3.5 2.7C3.5 20.6 7.4 23.2 12 23.2c2.7 0 5.1-.9 7-2.5l-3.4-2.7c-1 .7-2.3 1-3.6 1z"
      />
      <path
        fill="#4A90E2"
        d="M22.6 12.3c0-.8-.1-1.5-.2-2.3H12v4.3h5.9c-.3 1.4-1 2.6-2.2 3.4l3.4 2.7c2-1.9 3.5-4.7 3.5-8.1z"
      />
      <path
        fill="#FBBC05"
        d="M5.1 14.1c-.2-.7-.4-1.4-.4-2.1s.1-1.4.4-2.1L1.6 7.2C.8 8.7.4 10.3.4 12s.4 3.3 1.2 4.8l3.5-2.7z"
      />
    </svg>
  );
}

function MicrosoftIcon() {
  return (
    <svg aria-hidden viewBox="0 0 24 24" className="h-[22px] w-[22px]">
      <rect x="1" y="1" width="10" height="10" fill="#F35325" />
      <rect x="13" y="1" width="10" height="10" fill="#81BC06" />
      <rect x="1" y="13" width="10" height="10" fill="#05A6F0" />
      <rect x="13" y="13" width="10" height="10" fill="#FFBA08" />
    </svg>
  );
}

function InstitutionIcon() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="h-[22px] w-[22px] text-[var(--color-primary)]"
      fill="none"
    >
      <path
        d="M3 22 H21"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path
        d="M5 22 V12 M9 22 V12 M15 22 V12 M19 22 V12"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path d="M3 12 H21 L12 4 Z" fill="currentColor" />
    </svg>
  );
}

function FeatureFeather() {
  return (
    <svg aria-hidden viewBox="0 0 32 32" className="h-[30px] w-[30px]">
      <rect
        x="6"
        y="22"
        width="14"
        height="6"
        rx="1.5"
        fill="var(--color-primary)"
      />
      <rect x="9" y="20" width="8" height="2" fill="var(--color-primary)" />
      <path
        d="M22 4 C 26 8 26 16 18 22 L 14 22 C 14 18 16 8 22 4 Z"
        fill="var(--color-primary)"
        opacity="0.85"
        stroke="var(--color-primary)"
        strokeWidth="1"
      />
      <path
        d="M22 4 L 16 18"
        stroke="var(--color-bg-elevated)"
        strokeWidth="0.8"
      />
    </svg>
  );
}

function FeatureResearch() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 32 32"
      className="h-[30px] w-[30px]"
      fill="none"
    >
      <rect
        x="5"
        y="3"
        width="18"
        height="22"
        rx="2"
        stroke="var(--color-primary)"
        strokeWidth="1.6"
        fill="var(--color-bg-elevated)"
      />
      <line
        x1="9"
        y1="9"
        x2="19"
        y2="9"
        stroke="var(--color-primary)"
        strokeWidth="1.4"
      />
      <line
        x1="9"
        y1="13"
        x2="19"
        y2="13"
        stroke="var(--color-primary)"
        strokeWidth="1.4"
      />
      <line
        x1="9"
        y1="17"
        x2="15"
        y2="17"
        stroke="var(--color-primary)"
        strokeWidth="1.4"
      />
      <circle
        cx="22"
        cy="22"
        r="5"
        stroke="var(--color-primary)"
        strokeWidth="1.8"
        fill="var(--color-bg-elevated)"
      />
      <line
        x1="26"
        y1="26"
        x2="30"
        y2="30"
        stroke="var(--color-primary)"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function FeatureMultilingual() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 32 32"
      className="h-[30px] w-[30px]"
      fill="none"
    >
      <path
        d="M3 6 H17 a2 2 0 0 1 2 2 V16 a2 2 0 0 1 -2 2 H10 L6 22 V18 H5 a2 2 0 0 1 -2 -2 V8 a2 2 0 0 1 2 -2 z"
        fill="var(--color-primary)"
      />
      <path
        d="M7 11h7M7 14h5"
        stroke="var(--color-bg-elevated)"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <path
        d="M14 12 H27 a2 2 0 0 1 2 2 V22 a2 2 0 0 1 -2 2 H22 L19 28 V24 a2 2 0 0 1 -2 -2 V14 a2 2 0 0 1 2 -2 z"
        fill="var(--color-accent-gold)"
      />
      <path
        d="M19 20l2.5-5 2.5 5M20 18h3"
        stroke="var(--color-bg-elevated)"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FeatureBooks() {
  return (
    <svg aria-hidden viewBox="0 0 32 32" className="h-[30px] w-[30px]">
      <rect
        x="4"
        y="22"
        width="24"
        height="6"
        rx="1"
        fill="var(--color-primary)"
        opacity="0.78"
      />
      <rect
        x="6"
        y="16"
        width="20"
        height="6"
        rx="1"
        fill="var(--color-primary)"
        opacity="0.62"
      />
      <rect
        x="5"
        y="10"
        width="22"
        height="6"
        rx="1"
        fill="var(--color-primary)"
      />
      <line
        x1="9"
        y1="13"
        x2="14"
        y2="13"
        stroke="var(--color-bg-elevated)"
        strokeWidth="0.8"
      />
      <line
        x1="10"
        y1="19"
        x2="15"
        y2="19"
        stroke="var(--color-bg-elevated)"
        strokeWidth="0.8"
      />
      <line
        x1="9"
        y1="25"
        x2="14"
        y2="25"
        stroke="var(--color-bg-elevated)"
        strokeWidth="0.8"
      />
      <rect x="20" y="6" width="3" height="8" fill="var(--color-seal-red)" />
    </svg>
  );
}
