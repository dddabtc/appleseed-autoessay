import { useState } from "react";
import { Link, Route, Routes } from "react-router";

import { AuthGate, AuthProvider } from "./auth/AuthContext";
import { useAuth } from "./auth/AuthState";
import LoginPage from "./auth/LoginPage";
import {
  UI_LANGUAGE_LABELS,
  UILanguage,
  useT,
  useUILanguage,
} from "./lib/i18n";
import CorpusPage from "./pages/CorpusPage";
import HelpPage from "./pages/HelpPage";
import NewRunPage from "./pages/NewRunPage";
import RunsPage from "./pages/RunsPage";
import SettingsPage from "./pages/SettingsPage";
import WorkspacePage from "./pages/WorkspacePage";

export default function App() {
  // PR redesign-login: split /login out of AppShell so the landing
  // page can render full-bleed (its own header, full-width hero,
  // own background) instead of being framed by the in-app shell's
  // header + main padding wrapper.
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/*" element={<AppShell />} />
      </Routes>
    </AuthProvider>
  );
}

function AppShell() {
  const { user, logout } = useAuth();
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const userLabel = user?.display_name ?? user?.email ?? user?.id ?? "";
  const t = useT();

  return (
    <div className="min-h-screen bg-slate-50 font-sans text-slate-900">
      <header className="relative z-20 border-b border-slate-200 bg-white px-4 sm:px-6 lg:px-8">
        <div className="mx-auto grid min-h-16 max-w-7xl grid-cols-[44px_minmax(0,1fr)_44px] items-center gap-3 lg:flex lg:justify-between lg:gap-6">
          <button
            type="button"
            data-testid="nav-menu-toggle"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md border border-slate-200 bg-white text-xl font-bold text-[#114b5f] transition hover:bg-slate-50 lg:hidden"
            aria-label={t("nav.menu_open")}
            aria-expanded={isMenuOpen}
            onClick={() => setIsMenuOpen((current) => !current)}
          >
            {isMenuOpen ? "×" : "☰"}
          </button>
          <Link
            to="/"
            data-testid="nav-logo-link"
            className="min-w-0 truncate text-center text-base font-bold text-slate-950 no-underline lg:text-left"
            onClick={() => setIsMenuOpen(false)}
          >
            appleseed-autoessay
          </Link>
          <nav
            className="hidden items-center gap-2 lg:flex"
            aria-label="Primary navigation"
          >
            <Link data-testid="nav-runs-link" className={navLinkClasses} to="/">
              {t("nav.runs")}
            </Link>
            <Link
              data-testid="nav-new-run-link"
              className={navLinkClasses}
              to="/runs/new"
            >
              {t("nav.new_run")}
            </Link>
            <Link
              data-testid="nav-corpus-link"
              className={navLinkClasses}
              to="/corpus"
            >
              {t("nav.corpus")}
            </Link>
            <Link
              data-testid="nav-settings-link"
              className={navLinkClasses}
              to="/settings"
            >
              {t("nav.settings")}
            </Link>
            <Link
              data-testid="nav-help-link"
              className={navLinkClasses}
              to="/help"
            >
              {t("nav.help")}
            </Link>
          </nav>
          {user ? (
            <div className="hidden items-center justify-end gap-3 lg:flex">
              <LanguageSwitcher />
              <UserAvatar pictureUrl={user.picture_url} label={userLabel} />
              <span className="max-w-56 truncate text-sm font-medium text-slate-700">
                {userLabel}
              </span>
              <button
                type="button"
                data-testid="nav-logout-button"
                className={secondaryButtonClasses}
                onClick={() => void logout()}
              >
                {t("nav.logout")}
              </button>
            </div>
          ) : null}
          {/* Mobile header drops the user avatar per user request —
              redundant decoration on a small viewport when the
              hamburger drawer already exposes Logout/account access. */}
        </div>
        <div
          className={`absolute left-0 right-0 top-full border-b border-slate-200 bg-white p-4 shadow-lg transition duration-200 lg:hidden ${
            isMenuOpen
              ? "translate-y-0 opacity-100"
              : "pointer-events-none -translate-y-[120%] opacity-0"
          }`}
        >
          <nav className="grid gap-2" aria-label="Mobile navigation">
            <Link
              data-testid="nav-mobile-runs-link"
              className={mobileNavLinkClasses}
              to="/"
              onClick={() => setIsMenuOpen(false)}
            >
              {t("nav.runs")}
            </Link>
            <Link
              data-testid="nav-mobile-new-run-link"
              className={mobileNavLinkClasses}
              to="/runs/new"
              onClick={() => setIsMenuOpen(false)}
            >
              {t("nav.new_run")}
            </Link>
            <Link
              data-testid="nav-mobile-corpus-link"
              className={mobileNavLinkClasses}
              to="/corpus"
              onClick={() => setIsMenuOpen(false)}
            >
              {t("nav.corpus")}
            </Link>
            <Link
              data-testid="nav-mobile-settings-link"
              className={mobileNavLinkClasses}
              to="/settings"
              onClick={() => setIsMenuOpen(false)}
            >
              {t("nav.settings")}
            </Link>
            <Link
              data-testid="nav-mobile-help-link"
              className={mobileNavLinkClasses}
              to="/help"
              onClick={() => setIsMenuOpen(false)}
            >
              {t("nav.help")}
            </Link>
          </nav>
          <div className="mt-4 grid gap-2 border-t border-slate-200 pt-4">
            <span className="text-xs font-bold uppercase text-slate-500">
              {t("header.ui_language")}
            </span>
            <LanguageSwitcher />
          </div>
          {user ? (
            <div className="mt-4 grid gap-3 border-t border-slate-200 pt-4">
              <div className="flex min-w-0 items-center gap-3">
                <UserAvatar pictureUrl={user.picture_url} label={userLabel} />
                <span className="min-w-0 truncate text-sm font-medium text-slate-700">
                  {userLabel}
                </span>
              </div>
              <button
                type="button"
                data-testid="nav-mobile-logout-button"
                className={secondaryButtonClasses}
                onClick={() => {
                  setIsMenuOpen(false);
                  void logout();
                }}
              >
                {t("nav.logout")}
              </button>
            </div>
          ) : null}
        </div>
      </header>
      {/* PR-383: revert PR-380's ``overflow-x-hidden`` — it made the
          mobile layout WORSE (right-edge still cut off + pinch-zoom
          stopped working on iOS Safari). The real fix needs to find
          the wide child that's overflowing the viewport in the
          first place, not blanket-hide it. Tracking via codex
          consensus. */}
      <main className="mx-auto max-w-screen-2xl px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        <Routes>
          {/* PR redesign-login: /login moved to App-level Routes
              outside AppShell. This duplicate route was kept here so
              direct navigation inside the shell still works for tests
              that mount AppShell standalone — but production goes
              through the App-level route, which renders LoginPage
              full-bleed. */}
          <Route
            path="/"
            element={
              <AuthGate>
                <RunsPage />
              </AuthGate>
            }
          />
          <Route
            path="/runs/new"
            element={
              <AuthGate>
                <NewRunPage />
              </AuthGate>
            }
          />
          <Route
            path="/corpus"
            element={
              <AuthGate>
                <CorpusPage />
              </AuthGate>
            }
          />
          <Route
            path="/help"
            element={
              <AuthGate>
                <HelpPage />
              </AuthGate>
            }
          />
          <Route
            path="/runs/:id"
            element={
              <AuthGate>
                <WorkspacePage />
              </AuthGate>
            }
          />
          <Route
            path="/settings"
            element={
              <AuthGate>
                <SettingsPage />
              </AuthGate>
            }
          />
        </Routes>
      </main>
    </div>
  );
}

const navLinkClasses =
  "inline-flex min-h-11 items-center rounded-md px-3 py-2 text-sm font-semibold text-[#114b5f] no-underline transition hover:bg-slate-50";

const mobileNavLinkClasses =
  "inline-flex min-h-11 items-center rounded-md px-3 py-2 text-base font-semibold text-[#114b5f] no-underline transition hover:bg-slate-50";

const secondaryButtonClasses =
  "inline-flex min-h-11 items-center justify-center rounded-md bg-slate-100 px-4 py-2 text-sm font-bold text-[#114b5f] transition hover:bg-slate-200 disabled:cursor-default disabled:opacity-65";

function LanguageSwitcher() {
  const [lang, setLang] = useUILanguage();
  const t = useT();
  const codes: UILanguage[] = ["en", "zh", "ja"];
  return (
    <div
      className="inline-flex overflow-hidden rounded-md border border-slate-200 bg-white"
      role="group"
      aria-label={t("header.ui_language")}
    >
      {codes.map((code) => (
        <button
          key={code}
          type="button"
          data-testid={`lang-switcher-${code}`}
          onClick={() => setLang(code)}
          aria-pressed={lang === code}
          className={`min-h-9 px-2 text-xs font-bold transition ${
            lang === code
              ? "bg-[#114b5f] text-white"
              : "bg-white text-[#114b5f] hover:bg-slate-50"
          }`}
        >
          {UI_LANGUAGE_LABELS[code]}
        </button>
      ))}
    </div>
  );
}

function UserAvatar({
  pictureUrl,
  label,
}: {
  pictureUrl?: string | null;
  label: string;
}) {
  if (pictureUrl) {
    return (
      <img
        className="h-9 w-9 rounded-full object-cover"
        src={pictureUrl}
        alt=""
      />
    );
  }

  return (
    <span
      className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-[#236b45] text-sm font-bold text-white"
      aria-hidden="true"
    >
      {label.slice(0, 1).toUpperCase()}
    </span>
  );
}
