import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { commands, servers as serversApi, jobs as jobsApi } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Badge, Card, EmptyState } from "@/components/ui/Primitives";
import { useConfirm } from "@/hooks/useConfirm";
import { usePageVisible } from "@/hooks/usePageVisible";
import type { PlanOut } from "@/api/types";

interface Outcome {
  plan: PlanOut;
  at: number;
}

export function ConsolePage() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const queryClient = useQueryClient();
  const visible = usePageVisible();

  const servers = useQuery({ queryKey: ["servers"], queryFn: serversApi.list });
  const jobsQ = useQuery({
    queryKey: ["jobs"],
    queryFn: jobsApi.list,
    refetchInterval: visible ? 4_000 : false,
  });

  const [serverId, setServerId] = useState<number | null>(null);
  const [command, setCommand] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const list = servers.data ?? [];
  const effectiveServerId = serverId ?? list[0]?.id ?? null;
  const confirmToken = useMutation({
    mutationFn: commands.confirm,
    onSuccess: (plan) => {
      setOutcome({ plan: { ...plan, kind: "executed" }, at: Date.now() });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });

  const run = async (event: FormEvent) => {
    event.preventDefault();
    if (!effectiveServerId || !command.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const plan = await commands.execute(effectiveServerId, command);
      if (plan.kind === "confirm" && plan.pending) {
        const ok = await confirm({
          title: t("console.confirmTitle"),
          body: t("console.confirmBody", {
            risk: plan.risk,
            reason: plan.pending.reason,
          }),
          danger: true,
          confirmLabel: t("common.confirm"),
        });
        if (ok) {
          confirmToken.mutate(plan.pending.token);
        } else {
          setOutcome({ plan, at: Date.now() });
        }
      } else {
        setOutcome({ plan, at: Date.now() });
        void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errors.network"));
    } finally {
      setBusy(false);
    }
  };

  const recent = (jobsQ.data ?? []).slice(0, 8);

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">{t("console.title")}</h1>
      </header>

      <Card>
        <form onSubmit={run} className="flex flex-col gap-3">
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("console.server")}
            <select
              className="input mt-1"
              value={effectiveServerId ?? ""}
              onChange={(event) => setServerId(Number(event.target.value))}
            >
              {list.map((server) => (
                <option key={server.id} value={server.id}>
                  {server.alias} ({server.hostname})
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("jobs.command")}
            <input
              className="input mt-1 font-mono"
              dir="ltr"
              required
              placeholder={t("console.commandPlaceholder")}
              value={command}
              onChange={(event) => setCommand(event.target.value)}
            />
          </label>
          {error && <p className="text-xs text-danger">{error}</p>}
          <div className="flex justify-end">
            <Button type="submit" loading={busy} disabled={!effectiveServerId}>
              {busy ? t("console.running") : t("console.run")}
            </Button>
          </div>
        </form>
      </Card>

      {outcome && <OutcomeCard outcome={outcome} />}

      <Card title={t("console.recentJobs")}>
        {recent.length === 0 ? (
          <EmptyState title={t("states.emptyJobs")} />
        ) : (
          <ul className="flex flex-col gap-1.5">
            {recent.map((job) => (
              <li key={job.id} className="flex items-center justify-between gap-2 text-sm">
                <span className="min-w-0 truncate font-mono text-xs" dir="ltr">
                  {job.command}
                </span>
                <Badge tone={
                  job.state === "done" ? "ok"
                    : job.state === "failed" || job.state === "cancelled" ? "danger"
                      : "warn"
                }>
                  {t(`jobs.states.${job.state}`, { defaultValue: job.state })}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function OutcomeCard({ outcome }: { outcome: Outcome }) {
  const { t } = useTranslation();
  const { plan } = outcome;
  const tone = plan.kind === "executed" ? "ok" : plan.kind === "confirm" ? "warn" : "danger";
  const label =
    plan.kind === "executed" ? t("console.executed")
      : plan.kind === "confirm" ? t("console.confirm")
        : t("console.denied");
  return (
    <Card>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <Badge tone={tone}>{label}</Badge>
          {plan.risk && (
            <Badge tone="neutral">{t("console.risk")}: {plan.risk}</Badge>
          )}
        </div>
        {plan.kind === "executed" && plan.job && (
          <p className="text-sm text-slate-500 dark:text-slate-300">
            {t("console.jobQueued", { id: plan.job.id })}
          </p>
        )}
        {plan.kind === "denied" && (
          <p className="text-sm text-danger">{plan.reason}</p>
        )}
        {plan.kind === "confirm" && (
          <p className="text-sm text-amber-600 dark:text-amber-400">
            {t("console.blockedNotice")}
          </p>
        )}
      </div>
    </Card>
  );
}
