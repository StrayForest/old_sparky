"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { PlusCircle, Shield, User } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { BrandMark } from "@/components/layout/brand-mark";
import { PreparedMedia } from "@/components/media/prepared-media";
import {
  registerPlatformAuthStateListener,
  requestPlatformAuthStateChange,
  type PlatformAuthState
} from "@/lib/auth-session-signal";
import type { PlatformUser } from "@/lib/platform-types";
import { PlatformApiError, platformApiRequest } from "@/lib/platform-api";
import { navItems } from "@/lib/routes";

const MOBILE_ACCOUNT_BUTTON =
  "max-[820px]:flex-none! max-[820px]:h-[38px]! max-[820px]:w-[38px]! max-[820px]:min-w-[38px]! max-[820px]:rounded-full! max-[820px]:p-0!";
const MOBILE_CREATE_BUTTON =
  "max-[820px]:h-[38px]! max-[820px]:w-[38px]! max-[820px]:min-w-[38px]! max-[820px]:flex-[0_0_38px]! max-[820px]:rounded-full! max-[820px]:p-0!";
const MOBILE_BRAND =
  "max-[820px]:w-auto! max-[820px]:gap-3! max-[820px]:[&_.brand-mark]:h-11! max-[820px]:[&_.brand-mark]:w-11! max-[820px]:[&_.brand-title]:text-[15px]! max-[820px]:[&_.brand-title]:tracking-[.08em]! max-[820px]:[&_.brand-sub]:text-[8px]!";
const MOBILE_NAV =
  "max-[820px]:grid! max-[820px]:h-12! max-[820px]:w-full! max-[820px]:grid-cols-4! max-[820px]:gap-0! max-[820px]:overflow-x-visible!";
const MOBILE_NAV_LINK =
  "max-[820px]:h-12! max-[820px]:w-full! max-[820px]:justify-center! max-[820px]:px-0! max-[820px]:bg-transparent! max-[820px]:after:left-0! max-[820px]:after:right-0!";

export function SiteHeader({
  initialStatus = "anonymous",
  initialUser = null
}: {
  initialStatus?: PlatformAuthState["status"];
  initialUser?: PlatformUser | null;
} = {}) {
  const { t } = useI18n();
  const [authState, setAuthState] = useState<PlatformAuthState>({
    status: initialStatus,
    user: initialUser
  });
  const [isRetryingSession, setIsRetryingSession] = useState(false);
  useEffect(
    () => registerPlatformAuthStateListener((state) => setAuthState(state)),
    []
  );
  const refreshUser = useCallback(async () => {
    try {
      const nextUser = await platformApiRequest<PlatformUser>("/users/me");
      const nextState: PlatformAuthState = { status: "authenticated", user: nextUser };
      setAuthState(nextState);
      requestPlatformAuthStateChange(nextState);
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 401) {
        const nextState: PlatformAuthState = { status: "anonymous", user: null };
        setAuthState(nextState);
        requestPlatformAuthStateChange(nextState);
        return;
      }
      const nextState: PlatformAuthState = { status: "unavailable", user: authState.user };
      setAuthState(nextState);
      requestPlatformAuthStateChange(nextState);
      throw error;
    }
  }, [authState.user]);
  const pathname = usePathname();
  const { status, user } = authState;
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
            const className = `${active ? "nav-link active nav-link-active" : "nav-link"} ${MOBILE_NAV_LINK}`;
            if (item.hardNavigation) {
              return (
                <a key={item.href} className={className} href={item.href}>
                  {item.label}
                </a>
              );
            }
            return (
              <Link key={item.href} className={className} href={item.href}>
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
                <span className="header-profile-avatar" aria-hidden="true">
                  {user.avatar_media || user.avatar_url ? (
                    <PreparedMedia
                      alt=""
                      className="header-profile-avatar-image"
                      descriptor={user.avatar_media}
                      fallbackUrl={user.avatar_url}
                      height={40}
                      sizes="40px"
                      priority
                      width={40}
                    />
                  ) : <User size={18} />}
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
