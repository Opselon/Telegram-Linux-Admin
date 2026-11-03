import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { auth } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Primitives";
import { useAuth } from "@/hooks/useAuth";

export function SetupPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");
  const [tokenRequired, setTokenRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    auth.setupStatus()
      .then((status) => {
        setTokenRequired(status.token_required);
        if (!status.required) navigate("/login", { replace: true });
      })
      .catch(() => setTokenRequired(false))
      .finally(() => setChecking(false));
  }, [navigate]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.setup({
        username,
        password,
        ...(setupToken ? { setup_token: setupToken } : {}),
      });
      refresh();
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errors.generic"));
    } finally {
      setBusy(false);
    }
  };

  if (checking) return <Spinner label={t("common.loading")} />;

  return (
    <div className="flex min-h-dvh items-center justify-center bg-slate-100 px-4 dark:bg-ink-950">
      <form onSubmit={submit} className="panel w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.setupTitle")}</h1>
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{t("auth.setupDesc")}</p>
        <div className="mt-5 flex flex-col gap-3">
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("auth.username")}
            <input
              className="input mt-1"
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
              required
              minLength={10}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <span className="mt-1 block font-normal">{t("auth.passwordHint")}</span>
          </label>
          {tokenRequired && (
            <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
              {t("auth.setupToken")}
              <input
                className="input mt-1"
                required
                value={setupToken}
                onChange={(event) => setSetupToken(event.target.value)}
              />
              <span className="mt-1 block font-normal">{t("auth.setupTokenOptional")}</span>
            </label>
          )}
          {error && <p className="text-xs text-danger">{error}</p>}
          <Button type="submit" loading={busy} className="mt-1 w-full">
            {t("auth.createOwner")}
          </Button>
        </div>
      </form>
    </div>
  );
}
