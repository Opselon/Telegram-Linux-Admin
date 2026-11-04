import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Activity, Package, Server } from "lucide-react";
import { backups as backupsApi, jobs, notifications, servers as serversApi } from "@/api/endpoints";
import { Badge, Card, EmptyState, Skeleton } from "@/components/ui/Primitives";
import { timeAgo } from "@/lib/format";

export function DashboardPage() {
  const { t } = useTranslation();

  const servers = useQuery({ queryKey: ["servers"], queryFn: serversApi.list });
  const jobsQ = useQuery({ queryKey: ["jobs"], queryFn: jobs.list, refetchInterval: 15_000 });
  const backupsQ = useQuery({ queryKey: ["backups"], queryFn: backupsApi.list });
  const alertsQ = useQuery({
    queryKey: ["alerts", "recent"],
    queryFn: () => notifications.recent(10),
    refetchInterval: 30_000,
  });

  const serverList = servers.data ?? [];
  const activeJobs = (jobsQ.data ?? []).filter(
    (job) => job.state === "queued" || job.state === "running",
  );

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">{t("dashboard.title")}</h1>
        <p className="text-xs text-slate-400">{t("dashboard.subtitle")}</p>
      </header>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label={t("dashboard.totalServers")}
          value={serverList.length}
          icon={<Server className="h-4 w-4" aria-hidden />}
        />
        <Stat
          label={t("dashboard.activeJobs")}
          value={activeJobs.length}
          icon={<Activity className="h-4 w-4" aria-hidden />}
        />
        <Stat
          label={t("dashboard.backups")}
          value={backupsQ.data?.length ?? 0}
          icon={<Package className="h-4 w-4" aria-hidden />}
        />
        <Stat
          label={t("dashboard.alerts24h")}
          value={alertsQ.data?.length ?? 0}
          icon={<Activity className="h-4 w-4" aria-hidden />}
        />
      </div>

      <Card title={t("dashboard.serverList")}>
        {servers.isLoading ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        ) : serverList.length === 0 ? (
          <EmptyState title={t("dashboard.noServers")} />
        ) : (
          <ul className="flex flex-col gap-2">
            {serverList.map((server) => (
              <li key={server.id}>
                <Link
                  to={`/servers/${server.id}`}
                  className="flex items-center justify-between rounded-lg border
                    border-slate-200 px-3 py-2.5 transition-colors hover:border-accent/50
                    dark:border-ink-700"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">{server.alias}</span>
                    <span className="block truncate text-xs text-slate-400" dir="ltr">
                      {server.user}@{server.hostname}:{server.port}
                    </span>
                  </span>
                  <Badge tone="neutral">{t("servers.viewDetails")}</Badge>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title={t("dashboard.recentAlerts")}>
        {(alertsQ.data ?? []).length === 0 ? (
          <EmptyState title={t("states.emptyAlerts")} />
        ) : (
          <ul className="flex flex-col gap-1.5">
            {(alertsQ.data ?? []).map((alert, index) => (
              <li key={index} className="flex items-center justify-between gap-2 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <Badge
                    tone={
                      alert.severity === "critical"
                        ? "danger"
                        : alert.severity === "info"
                          ? "neutral"
                          : "warn"
                    }
                  >
                    {t(`alerts.${alert.kind}`, { defaultValue: alert.kind })}
                  </Badge>
                  <span className="truncate text-xs text-slate-400">{alert.scope}</span>
                </span>
                <span className="shrink-0 text-xs text-slate-400">{timeAgo(alert.at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Stat({ label, value, icon }: { label: string; value: number; icon: React.ReactNode }) {
  return (
    <div className="panel flex items-center gap-3 p-4">
      <div className="rounded-lg bg-accent-soft p-2 text-accent">{icon}</div>
      <div>
        <p className="text-xl font-semibold tabular-nums">{value}</p>
        <p className="text-[11px] text-slate-400">{label}</p>
      </div>
    </div>
  );
}
