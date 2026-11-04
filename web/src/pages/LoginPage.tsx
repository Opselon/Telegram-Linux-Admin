import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { auth } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/hooks/useAuth";

export function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.login({ username, password });
      refresh();
      const next = params.get("next");
      navigate(next && next.startsWith("/") ? next : "/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === "login_locked_out"
            ? t("auth.lockedOut")
            : t("auth.invalidCredentials"),
        );
      } else {
        setError(t("errors.network"));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-slate-100 px-4 dark:bg-ink-950">
      <form onSubmit={submit} className="panel w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.loginTitle")}</h1>
        <div className="mt-5 flex flex-col gap-3">
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("auth.username")}
            <input
              className="input mt-1"
              autoComplete="username"
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("auth.password")}
            <input
              className="input mt-1"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {error && <p className="text-xs text-danger">{error}</p>}
          <Button type="submit" loading={busy} className="mt-1 w-full">
            {busy ? t("auth.signingIn") : t("auth.signIn")}
          </Button>
        </div>
      </form>
    </div>
  );
}
