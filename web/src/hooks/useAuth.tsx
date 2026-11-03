import {
  createContext, useCallback, useContext, useEffect, useMemo,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createSearchParams, useLocation, useNavigate } from "react-router-dom";
import { auth } from "@/api/endpoints";
import { onUnauthorized } from "@/api/client";
import type { AccountOut } from "@/api/types";

interface AuthState {
  account: AccountOut | null;
  loading: boolean;
  /** True once a successful /auth/me proves we are signed in. */
  authenticated: boolean;
  can: (permission: string) => boolean;
  refresh: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }): ReactNode {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();

  const meQuery = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => auth.me(),
    retry: false,
    staleTime: 60_000,
  });

  const account = meQuery.data ?? null;

  // 401 anywhere (except auth routes) → /login with a return path.
  useEffect(() => {
    return onUnauthorized(() => {
      queryClient.setQueryData(["auth", "me"], null);
      const params = createSearchParams({ next: location.pathname + location.search });
      navigate(`/login?${params.toString()}`, { replace: true });
    });
  }, [navigate, location.pathname, location.search, queryClient]);

  const signOut = useCallback(async () => {
    try {
      await auth.logout();
    } catch {
      // Even if logout fails, drop local state.
    }
    queryClient.setQueryData(["auth", "me"], null);
    queryClient.clear();
    navigate("/login", { replace: true });
  }, [navigate, queryClient]);

  const can = useCallback(
    (permission: string) => account?.permissions.includes(permission) ?? false,
    [account],
  );

  const value = useMemo<AuthState>(
    () => ({
      account,
      loading: meQuery.isLoading,
      authenticated: account !== null,
      can,
      refresh: () => void queryClient.invalidateQueries({ queryKey: ["auth", "me"] }),
      signOut,
    }),
    [account, meQuery.isLoading, can, signOut, queryClient],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === null) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
