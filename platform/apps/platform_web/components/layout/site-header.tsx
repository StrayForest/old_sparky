"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { PlusCircle, Shield, User } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";
import { BrandMark } from "@/components/layout/brand-mark";
import { PreparedMedia } from "@/components/media/prepared-media";
import { navItems } from "@/lib/routes";

const MOBILE_ACCOUNT_BUTTON =
  "max-[820px]:flex-none! max-[820px]:h-[38px]! max-[820px]:w-[38px]! max-[820px]:min-w-[38px]! max-[820px]:rounded-full! max-[820px]:p-0!";
const MOBILE_CREATE_BUTTON =
  "max-[820px]:h-[38px]! max-[820px]:w-[38px]! max-[820px]:min-w-[38px]! max-[820px]:flex-[0_0_38px]! max-[820px]:rounded-full! max-[820px]:p-0!";
const MOBILE_BRAND =
  "max-[820px]:w-auto! max-[820px]:gap-3! max-[820px]:[&_.brand-mark]:h-11! max-[820px]:[&_.brand-mark]:w-11! max-[820px]:[&_.brand-title]:text-[15px]! max-[820px]:[&_.brand-title]:tracking-[.08em]! max-[820px]:[&_.brand-sub]:text-[8px]!";
const MOBILE_NAV =
  "max-[820px]:grid! max-[820px]:h-12! max-[820px]:w-full! max-[820px]:grid-cols-3! max-[820px]:gap-0! max-[820px]:overflow-x-visible!";
const MOBILE_NAV_LINK =
  "max-[820px]:h-12! max-[820px]:w-full! max-[820px]:justify-center! max-[820px]:px-0! max-[820px]:bg-transparent! max-[820px]:after:left-0! max-[820px]:after:right-0!";

export function SiteHeader() {
  const { refreshUser, status, user } = useAuth();
  const { t } = useI18n();
  const [isRetryingSession, setIsRetryingSession] = useState(false);
  const pathname = usePathname();
  const isCreateTournament = pathname === "/tournaments/new";
  const exactActiveHref = navItems.find((item) => item.href === pathname)?.href;
  const canOpenAdmin = Boolean(user?.roles.includes("admin") || user?.roles.includes("superadmin"));
  const authReturnQuery = pathname.startsWith("/auth/")
    ? ""
    : `?returnTo=${encodeURIComponent(pathname)}`;
  const createTournamentAction = (
    <Link
      aria-label={t("header.createTournament")}
      className={`${isCreateTournament ? "header-create-button active" : "header-create-button"} ${MOBILE_CREATE_BUTTON}`}
      href="/tournaments/new"
    >
      <PlusCircle size={16} aria-hidden="true" />
      <span className="header-create-label">{t("header.createTournament")}</span>
    </Link>
  );
  const unavailableRetryAction = status === "unavailable" ? (
    <button
      aria-label={t("header.retrySessionCheck")}
      className="login-button compact-login-button"
      disabled={isRetryingSession}
      onClick={() => {
        if (isRetryingSession) {
          return;
        }
        setIsRetryingSession(true);
        void refreshUser()
          .catch(() => undefined)
          .finally(() => setIsRetryingSession(false));
      }}
      type="button"
    >
      <User size={18} aria-hidden="true" />
      <span className="header-session-label">
        {isRetryingSession ? t("header.checkingSession") : t("common.retry")}
      </span>
    </button>
  ) : null;

  return (
    <header className="site-header">
      <div className="header-inner">
        <Link className={`brand ${MOBILE_BRAND}`} href="/" aria-label={t("header.homeLabel")}>
          <BrandMark />
          <span className="brand-text">
            <span className="brand-title">OLD SPARKY</span>
            <span className="brand-sub">ARENA</span>
          </span>
        </Link>

        <nav className={`nav ${MOBILE_NAV}`} aria-label={t("header.mainNavigation")}>
          {navItems.map((item) => {
            const active = exactActiveHref
              ? item.href === exactActiveHref
              : Boolean(!isCreateTournament && item.matchPrefix && pathname.startsWith(item.matchPrefix));
            return (
              <Link
                key={item.href}
                className={`${active ? "nav-link active nav-link-active" : "nav-link"} ${MOBILE_NAV_LINK}`}
                href={item.href}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="header-actions">
          {status === "unavailable" ? (
            unavailableRetryAction
          ) : user ? (
            <>
              {canOpenAdmin ? (
                <Link aria-label={t("header.operations")} className="login-button compact-login-button" href="/platform-ops">
                  <Shield size={17} aria-hidden="true" />
                  <span className="header-operations-label">{t("header.operations")}</span>
                </Link>
              ) : null}
              <Link
                aria-label={t("header.profileLabel", { name: user.display_name })}
                className={`login-button ${MOBILE_ACCOUNT_BUTTON}`}
                href="/profile/me"
              >
                <span className="relative inline-grid place-items-center max-[820px]:size-full" aria-hidden="true">
                  <User size={18} />
                  {user.avatar_media || user.avatar_url ? (
                    <PreparedMedia
                      alt=""
                      className="absolute inset-0 hidden h-full w-full rounded-full object-cover max-[820px]:block"
                      descriptor={user.avatar_media}
                      fallbackUrl={user.avatar_url}
                      height={40}
                      sizes="40px"
                      width={40}
                    />
                  ) : null}
                </span>
                <span className="header-profile-label max-[820px]:hidden">
                  {user.display_name}
                </span>
              </Link>
              {createTournamentAction}
            </>
          ) : (
            <>
              <Link
                className="login-button compact-login-button header-register-link"
                href={`/auth/register${authReturnQuery}`}
                prefetch={false}
              >
                {t("auth.createAccount")}
              </Link>
              <Link
                aria-label={t("auth.login")}
                className={`login-button compact-login-button ${MOBILE_ACCOUNT_BUTTON}`}
                href={`/auth/login${authReturnQuery}`}
                prefetch={false}
              >
                <span className="hidden place-items-center max-[820px]:grid" aria-hidden="true">
                  <User size={18} />
                </span>
                <span className="max-[820px]:hidden">{t("auth.login")}</span>
              </Link>
              {createTournamentAction}
            </>
          )}
        </div>
      </div>
    </header>
  );
}
