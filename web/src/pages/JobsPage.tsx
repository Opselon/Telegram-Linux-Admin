import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { jobs as jobsApi } from "@/api/endpoints";
import { Button } from "@/components/ui/Button";
import { Badge, Card, EmptyState, Spinner } from "@/components/ui/Primitives";
import { useConfirm } from "@/hooks/useConfirm";
import { usePageVisible } from "@/hooks/usePageVisible";
import { timeAgo } from "@/lib/format";
import type { JobOut } from "@/api/types";

export function JobsPage() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const queryClient = useQueryClient();
  const visible = usePageVisible();

  const query = useQuery({
    queryKey: ["jobs"],
    queryFn: jobsApi.list,
    refetchInterval: visible ? 5_000 : false,
  });

  const cancelMutation = useMutation({
    mutationFn: jobsApi.cancel,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const onCancel = async (job: JobOut) => {
    const ok = await confirm({
      title: t("jobs.cancelConfirmTitle"),
      body: t("jobs.cancelConfirmBody", { id: job.id }),
      danger: true,
      confirmLabel: t("jobs.cancel"),
    });
    if (ok) cancelMutation.mutate(job.id);
  };

  const list = query.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">{t("jobs.title")}</h1>
      </header>

      <Card>
        {query.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : list.length === 0 ? (
          <EmptyState title={t("states.emptyJobs")} />
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th className="table-th">{t("jobs.id")}</th>
                  <th className="table-th">{t("servers.alias")}</th>
                  <th className="table-th">{t("jobs.command")}</th>
                  <th className="table-th">{t("jobs.state")}</th>
                  <th className="table-th">{t("jobs.created")}</th>
                  <th className="table-th">{t("common.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((job) => (
                  <tr key={job.id}>
                    <td className="table-td font-mono text-xs" dir="ltr">{job.id}</td>
                    <td className="table-td">{job.alias}</td>
                    <td className="table-td max-w-xs truncate font-mono text-xs" dir="ltr"
                        title={job.command}>
                      {job.command}
                    </td>
                    <td className="table-td">
                      <Badge tone={stateTone(job.state)}>
                        {t(`jobs.states.${job.state}`, { defaultValue: job.state })}
                      </Badge>
                    </td>
                    <td className="table-td text-xs text-slate-400">{timeAgo(job.created_at)}</td>
                    <td className="table-td">
                      {(job.state === "queued" || job.state === "running") && (
                        <Button size="sm" variant="ghost" onClick={() => void onCancel(job)}>
                          {t("jobs.cancel")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function stateTone(state: string): "ok" | "warn" | "danger" | "neutral" {
  if (state === "done") return "ok";
  if (state === "running" || state === "queued") return "warn";
  if (state === "failed" || state === "cancelled") return "danger";
  return "neutral";
}
