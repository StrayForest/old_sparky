"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState
} from "react";
import { registerPlatformUnauthorizedHandler } from "@/lib/auth-session-signal";
import { PlatformApiError, platformApiRequest } from "@/lib/platform-api";
import type { PlatformUser } from "@/lib/platform-types";

export type AuthStatus = "authenticated" | "anonymous" | "unavailable";

type AuthUserPatch = Partial<PlatformUser> | ((user: PlatformUser) => PlatformUser);

type AuthContextValue = {
  status: AuthStatus;
  user: PlatformUser | null;
  clearUser: () => void;
  refreshUser: () => Promise<void>;
  setUser: (user: PlatformUser | null) => void;
  updateUser: (userId: string, patch: AuthUserPatch) => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({
  children,
  initialStatus,
  initialUser
}: {
  children: ReactNode;
  initialStatus: AuthStatus;
  initialUser: PlatformUser | null;
}) {
  // Root layouts persist across client navigation. Intentionally use the
  // server value only as the initializer so a route transition cannot reset a
  // newer login/logout/profile mutation to a loading or anonymous state.
  const [user, setUser] = useState<PlatformUser | null>(initialUser);
  const [status, setStatus] = useState<AuthStatus>(initialStatus);
  const replaceUser = useCallback((nextUser: PlatformUser | null) => {
    setUser(nextUser);
    setStatus(nextUser ? "authenticated" : "anonymous");
  }, []);
  const clearUser = useCallback(() => replaceUser(null), [replaceUser]);
  const refreshUser = useCallback(async () => {
    try {
      replaceUser(await platformApiRequest<PlatformUser>("/users/me"));
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 401) {
        replaceUser(null);
        return;
      }
      setStatus("unavailable");
      throw error;
    }
  }, [replaceUser]);
  const updateUser = useCallback((userId: string, patch: AuthUserPatch) => {
    setUser((current) => {
      if (!current || current.id !== userId) {
        return current;
      }
      return typeof patch === "function" ? patch(current) : { ...current, ...patch };
    });
  }, []);
  useEffect(
    () => registerPlatformUnauthorizedHandler(clearUser),
    [clearUser]
  );
  const value = useMemo(
    () => ({ status, user, clearUser, refreshUser, setUser: replaceUser, updateUser }),
    [clearUser, refreshUser, replaceUser, status, updateUser, user]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used inside AuthProvider.");
  }
  return value;
}
