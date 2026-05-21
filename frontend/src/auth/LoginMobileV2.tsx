import type { FormEvent } from "react";
import { useState } from "react";

import { type UILanguage, useT, useUILanguage } from "../lib/i18n";

import "./LoginMobileV2.css";

// 1:1 React port of frontend/_login_design_ref/v2-zh-slice (the static
// design slice the user shipped). Absolute-positioned layout with
// pixel-perfect coords matches the design mockup exactly. zh keeps
// the Laozi quote per user direction; en/ja translate the Confucian
// pair "study without weariness / refine day by day" to match the
// per-language calligraphy already shipped in i18n.

const LANG_LABEL: Record<UILanguage, string> = {
  en: "EN",
  zh: "中文",
  ja: "日本語",
};

interface LoginMobileV2Props {
  username: string;
  password: string;
  submitting: boolean;
  errorMessage: string | null;
  onUsernameChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onComingSoon: () => void;
  onMenuToggle: () => void;
  isMenuOpen: boolean;
}

export default function LoginMobileV2(props: LoginMobileV2Props) {
  const t = useT();
  const [lang, setLang] = useUILanguage();
  const [showPassword, setShowPassword] = useState(false);
  const [langOpen, setLangOpen] = useState(false);

  return (
    <div className="login-mobile-v2-root" data-testid="login-mobile-v2-root">
      <main
        className="mobile-page"
        data-lang={lang}
        aria-label={t("login.brand.name")}
      >
        <header className="site-header">
          <a className="brand" href="#" aria-label={t("login.aria.home")}>
            <img
              src="/login-bg/logo.png"
              alt=""
              className="brand-logo"
              data-testid="login-brand-logo"
            />
            <span className="brand-name">{t("login.brand.name")}</span>
          </a>

          <nav
            className="header-actions"
            aria-label={t("login.aria.primary_navigation")}
          >
            <div style={{ position: "relative" }}>
              <button
                className="language-button"
                type="button"
                aria-label={t("login.aria.language_switcher")}
                aria-haspopup="listbox"
                aria-expanded={langOpen}
                data-testid="login-lang-pill-trigger"
                onClick={() => setLangOpen((v) => !v)}
              >
                <span>{LANG_LABEL[lang]}</span>
                <span className="chevron" aria-hidden="true" />
              </button>
              {langOpen ? (
                <>
                  <button
                    type="button"
                    aria-label={t("login.aria.close_menu")}
                    style={{
                      position: "fixed",
                      inset: 0,
                      background: "transparent",
                      border: 0,
                      padding: 0,
                      zIndex: 30,
                    }}
                    onClick={() => setLangOpen(false)}
                  />
                  <ul
                    role="listbox"
                    data-testid="login-lang-pill-menu"
                    style={{
                      position: "absolute",
                      top: "calc(100% + 6px)",
                      right: 0,
                      minWidth: 96,
                      background: "#fff",
                      border: "1px solid #dededa",
                      borderRadius: 12,
                      boxShadow: "0 6px 18px rgba(20,30,25,0.12)",
                      padding: 4,
                      margin: 0,
                      listStyle: "none",
                      zIndex: 40,
                    }}
                  >
                    {(Object.keys(LANG_LABEL) as UILanguage[]).map((code) => (
                      <li key={code}>
                        <button
                          type="button"
                          role="option"
                          aria-selected={lang === code}
                          data-testid={`login-lang-pill-${code}`}
                          onClick={() => {
                            setLang(code);
                            setLangOpen(false);
                          }}
                          style={{
                            display: "block",
                            width: "100%",
                            padding: "8px 12px",
                            border: 0,
                            background:
                              lang === code ? "#e6efe9" : "transparent",
                            color: lang === code ? "#1c4e3c" : "#222",
                            fontSize: 14,
                            textAlign: "left",
                            fontWeight: lang === code ? 700 : 400,
                            borderRadius: 8,
                          }}
                        >
                          {LANG_LABEL[code]}
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>
            <button
              className="menu-button"
              type="button"
              aria-label={t(
                props.isMenuOpen
                  ? "login.aria.close_menu"
                  : "login.aria.mobile_menu",
              )}
              onClick={props.onMenuToggle}
            >
              <span aria-hidden="true" />
              <span aria-hidden="true" />
              <span aria-hidden="true" />
            </button>
          </nav>
        </header>

        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-mark" aria-hidden="true">
            <img src="/login-bg/svg/leaf.svg" alt="" />
            <i />
          </div>

          <h1 id="hero-title" data-testid="login-hero-headline">
            <span>{t("login.hero.title_pre")}</span>
            <span>
              {t("login.hero.title_main")}
              {lang === "en" ? " " : null}
              <strong>{t("login.hero.title_accent")}</strong>
            </span>
          </h1>

          <p>{t("login.hero.tagline")}</p>
        </section>

        {/* Wisdom calligraphy card removed on mobile per user
            feedback ("手机版把这个 banner 去掉") — the bg PNG
            already carries the cultural decoration so the floating
            card felt redundant. Desktop still renders the
            calligraphy via DecorativeCalligraphy in LoginPage. */}

        <section className="login-panel" aria-labelledby="login-title">
          <div className="seed-badge" aria-hidden="true">
            <img src="/login-bg/svg/sprout.svg" alt="" />
          </div>

          <h2 id="login-title">{t("login.card.welcome_back")}</h2>
          <p className="login-subtitle">{t("login.card.subtitle")}</p>

          <form
            className="login-form"
            onSubmit={props.onSubmit}
            data-testid="login-form"
            noValidate
          >
            <label className="field">
              <span className="field-icon user-icon" aria-hidden="true" />
              <input
                type="text"
                name="username"
                placeholder={t("login.input.username_placeholder")}
                autoComplete="username"
                value={props.username}
                onChange={(e) => props.onUsernameChange(e.target.value)}
                disabled={props.submitting}
                data-testid="login-username-input"
              />
            </label>

            <label className="field">
              <span className="field-icon lock-icon" aria-hidden="true" />
              <input
                type={showPassword ? "text" : "password"}
                name="password"
                placeholder={t("login.input.password_placeholder")}
                autoComplete="current-password"
                value={props.password}
                onChange={(e) => props.onPasswordChange(e.target.value)}
                disabled={props.submitting}
                data-testid="login-password-input"
              />
              <button
                className="password-toggle"
                type="button"
                aria-label={t(
                  showPassword
                    ? "login.input.hide_password"
                    : "login.input.show_password",
                )}
                onClick={() => setShowPassword((v) => !v)}
              >
                <span aria-hidden="true" />
              </button>
            </label>

            <div className="form-options">
              <label className="remember">
                <input
                  type="checkbox"
                  name="remember"
                  data-testid="login-remember-checkbox"
                />
                <span>{t("login.input.remember_me")}</span>
              </label>
              <button
                type="button"
                onClick={props.onComingSoon}
                data-testid="login-forgot-button"
              >
                {t("login.input.forgot_password")}
              </button>
            </div>

            {props.errorMessage ? (
              <div
                role="alert"
                data-testid="login-error-message"
                className="login-error"
              >
                {props.errorMessage}
              </div>
            ) : null}

            <button
              className="submit-button"
              type="submit"
              disabled={
                props.submitting ||
                props.username.length === 0 ||
                props.password.length === 0
              }
              data-testid="login-primary-submit-button"
            >
              {props.submitting
                ? t("login.cta.signing_in")
                : t("login.cta.sign_in")}
            </button>
          </form>

          <div className="divider" aria-hidden="true">
            <span>{t("login.divider.or_continue")}</span>
          </div>

          <div
            className="social-login"
            aria-label={t("login.aria.features")}
          >
            <button
              className="social-button"
              type="button"
              aria-label={t("login.oauth.google")}
              data-testid="login-oauth-google-button"
              onClick={props.onComingSoon}
            >
              <img src="/login-bg/svg/oauth-google.svg" alt="" />
            </button>
            <button
              className="social-button microsoft"
              type="button"
              aria-label={t("login.oauth.microsoft")}
              data-testid="login-oauth-microsoft-button"
              onClick={props.onComingSoon}
            >
              <img src="/login-bg/svg/oauth-microsoft.svg" alt="" />
            </button>
            <button
              className="social-button school"
              type="button"
              aria-label={t("login.oauth.sso")}
              data-testid="login-oauth-sso-button"
              onClick={props.onComingSoon}
            >
              <img src="/login-bg/svg/oauth-sso.svg" alt="" />
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}

