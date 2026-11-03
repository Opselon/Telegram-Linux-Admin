import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { auth, notifications } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Badge, Card } from "@/components/ui/Primitives";
import { useTheme } from "@/lib/theme";

export function SettingsPage() {
  const { t, i18n } = useTranslation();
  const { dark, set } = useTheme();
  const queryClient = useQueryClient();

  const prefs = useQuery({ queryKey: ["notif", "prefs"], queryFn: notifications.prefs });

  const categories = Object.keys(t("settings.categories", { returnObjects: true }) as Record<string, string>);

  const prefsMutation = useMutation({
    mutationFn: notifications.setPrefs,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["notif"] }),
  });

  const toggleCategory = (category: string) => {
    if (!prefs.data) return;
    const disabled = new Set(prefs.data.disabled_categories);
    if (disabled.has(category)) disabled.delete(category);
    else disabled.add(category);
    prefsMutation.mutate([...disabled]);
  };

  const toggleLanguage = () => {
    const next = i18n.language?.startsWith("fa") ? "en" : "fa";
    void i18n.changeLanguage(next);
    document.documentElement.lang = next;
    document.documentElement.dir = next === "fa" ? "rtl" : "ltr";
    try {
      localStorage.setItem("tla_lang", next);
    } catch {
      // Ignore.
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">{t("settings.title")}</h1>
      </header>

      <PasswordCard />

      <MfaCard />

      <Card title={t("settings.notifications")}>
        <p className="mb-3 text-xs text-slate-400">{t("settings.notificationsHint")}</p>
        <div className="flex flex-wrap gap-2">
          {categories.map((category) => {
            const muted = prefs.data?.disabled_categories.includes(category) ?? false;
            return (
              <button
                key={category}
                type="button"
                onClick={() => toggleCategory(category)}
                className={`rounded-full px-3 py-1 text-xs font-medium transition-colors
                  ${muted
                    ? "bg-slate-500/12 text-slate-400 line-through"
                    : "bg-accent-soft text-accent"}`}
              >
                {t(`settings.categories.${category}`)}
              </button>
            );
          })}
        </div>
      </Card>

      <Card title={t("settings.appearance")}>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant={dark ? "primary" : "secondary"} onClick={() => set("dark")}>
            {t("common.dark")}
          </Button>
          <Button size="sm" variant={!dark ? "primary" : "secondary"} onClick={() => set("light")}>
            {t("common.light")}
          </Button>
          <Button size="sm" variant="secondary" onClick={toggleLanguage}>
            {t("common.language")}: {i18n.language?.startsWith("fa") ? "فارسی" : "English"}
          </Button>
        </div>
      </Card>
    </div>
  );
}

function PasswordCard() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: auth.changePassword,
    onSuccess: () => {
      setMessage(t("settings.passwordChanged"));
      setCurrent("");
      setNext("");
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });

  return (
    <Card title={t("settings.password")}>
      <div className="flex max-w-sm flex-col gap-3">
        <input
          className="input" type="password" dir="ltr" autoComplete="current-password"
          placeholder={t("settings.currentPassword")} value={current}
          onChange={(event) => setCurrent(event.target.value)}
        />
        <input
          className="input" type="password" dir="ltr" autoComplete="new-password"
          placeholder={t("settings.newPassword")} value={next}
          onChange={(event) => setNext(event.target.value)}
        />
        {error && <p className="text-xs text-danger">{error}</p>}
        {message && <p className="text-xs text-ok">{message}</p>}
        <Button
          size="sm"
          className="w-fit"
          loading={mutation.isPending}
          onClick={() => mutation.mutate({ current, new: next })}
        >
          {t("settings.changePassword")}
        </Button>
      </div>
    </Card>
  );
}

function MfaCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [secret, setSecret] = useState<string | null>(null);
  const [otpauth, setOtpauth] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const me = useQuery({ queryKey: ["auth", "me"], queryFn: auth.me });
  const enabled = me.data?.mfa_enabled ?? false;

  const enableMutation = useMutation({
    mutationFn: auth.mfaEnable,
    onSuccess: (result) => {
      setSecret(result.secret);
      setOtpauth(result.otpauth);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });
  const activateMutation = useMutation({
    mutationFn: () => auth.mfaActivate(code),
    onSuccess: () => {
      setMessage(t("settings.mfaActivated"));
      setSecret(null);
      setOtpauth(null);
      setCode("");
      void queryClient.invalidateQueries({ queryKey: ["auth"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });
  const disableMutation = useMutation({
    mutationFn: auth.mfaDisable,
    onSuccess: () => {
      setMessage(t("settings.mfaDisabledMsg"));
      void queryClient.invalidateQueries({ queryKey: ["auth"] });
    },
  });

  return (
    <Card title={t("settings.mfa")}>
      <div className="flex flex-col gap-3 text-sm">
        <Badge tone={enabled ? "ok" : "neutral"}>
          {enabled ? t("settings.mfaEnabled") : t("settings.mfaDisabled")}
        </Badge>
        {!enabled && !secret && (
          <Button size="sm" className="w-fit" loading={enableMutation.isPending}
                  onClick={() => enableMutation.mutate()}>
            {t("settings.enableMfa")}
          </Button>
        )}
        {secret && otpauth && (
          <div className="flex flex-col gap-2">
            <p className="text-xs text-slate-400">{t("settings.mfaOtpauth")}</p>
            <code className="break-all rounded-lg bg-slate-100 p-2 text-xs dark:bg-ink-850" dir="ltr">
              {otpauth}
            </code>
            <code className="text-xs text-slate-400" dir="ltr">{secret}</code>
            <input
              className="input max-w-[12rem]" dir="ltr" inputMode="numeric" maxLength={8}
              placeholder={t("settings.mfaActivate")} value={code}
              onChange={(event) => setCode(event.target.value)}
            />
            <Button size="sm" className="w-fit" loading={activateMutation.isPending}
                    onClick={() => activateMutation.mutate()}>
              {t("auth.verify")}
            </Button>
          </div>
        )}
        {enabled && (
          <Button size="sm" variant="danger" className="w-fit" loading={disableMutation.isPending}
                  onClick={() => disableMutation.mutate()}>
            {t("settings.disableMfa")}
          </Button>
        )}
        {message && <p className="text-xs text-ok">{message}</p>}
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Card>
  );
}
