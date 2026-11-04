import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Download, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { backups as backupsApi } from "@/api/endpoints";
import { Button } from "@/components/ui/Button";
import { Card, EmptyState, Spinner } from "@/components/ui/Primitives";
import { useConfirm } from "@/hooks/useConfirm";
import { formatBytes, timeAgo } from "@/lib/format";

export function BackupsPage() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const queryClient = useQueryClient();

  const query = useQuery({ queryKey: ["backups"], queryFn: backupsApi.list });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["backups"] });

  const createMutation = useMutation({ mutationFn: backupsApi.create, onSuccess: invalidate });
  const verifyMutation = useMutation({ mutationFn: backupsApi.verify, onSuccess: invalidate });
  const removeMutation = useMutation({ mutationFn: backupsApi.remove, onSuccess: invalidate });
  const restoreMutation = useMutation({ mutationFn: backupsApi.restore, onSuccess: invalidate });

  const onCreate = () => createMutation.mutate();

  const onVerify = async (id: string) => {
    const result = await verifyMutation.mutateAsync(id);
    const ok = await confirm({
      title: result.ok ? t("backups.verifyOk") : t("backups.verifyFailed"),
      body: result.ok ? id : result.issues.join("\n"),
      confirmLabel: t("common.ok"),
    });
    void ok;
  };

  const onRestore = async (id: string) => {
    const ok = await confirm({
      title: t("backups.restoreTitle"),
      body: t("backups.restoreBody", { word: t("backups.restoreConfirmWord"), safety: "—" }),
      typedWord: t("backups.restoreConfirmWord"),
      danger: true,
      confirmLabel: t("backups.restore"),
    });
    if (ok) restoreMutation.mutate(id);
  };

  const onDelete = async (id: string) => {
    const ok = await confirm({
      title: t("backups.deleteConfirmTitle"),
      body: t("backups.deleteConfirmBody", { id }),
      typedWord: id,
      danger: true,
      confirmLabel: t("common.delete"),
    });
    if (ok) removeMutation.mutate(id);
  };

  const list = query.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-center justify-between gap-2">
        <h1 className="text-lg font-semibold tracking-tight">{t("backups.title")}</h1>
        <Button size="sm" loading={createMutation.isPending} onClick={onCreate}>
          <Plus className="h-4 w-4" aria-hidden />
          {t("backups.create")}
        </Button>
      </header>

      {restoreMutation.isSuccess && restoreMutation.data && (
        <p className="rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-600 dark:text-emerald-400">
          {t("backups.restoreDone", { safety: restoreMutation.data.safety_backup })}
        </p>
      )}
      {restoreMutation.isError && (
        <p className="text-xs text-danger">{String(restoreMutation.error)}</p>
      )}

      <Card>
        {query.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : list.length === 0 ? (
          <EmptyState title={t("states.emptyTitle")} hint={t("states.emptyBackups")} />
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th className="table-th">{t("backups.id")}</th>
                  <th className="table-th">{t("backups.created")}</th>
                  <th className="table-th">{t("backups.size")}</th>
                  <th className="table-th">{t("common.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((backup) => (
                  <tr key={backup.backup_id}>
                    <td className="table-td font-mono text-xs" dir="ltr">{backup.backup_id}</td>
                    <td className="table-td text-xs text-slate-400">
                      {backup.created_at ? timeAgo(Date.parse(backup.created_at) / 1000) : "—"}
                    </td>
                    <td className="table-td text-xs">{formatBytes(backup.size)}</td>
                    <td className="table-td">
                      <div className="flex flex-wrap gap-1">
                        <Button size="sm" variant="secondary" loading={verifyMutation.isPending}
                                onClick={() => void onVerify(backup.backup_id)}>
                          <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
                          {t("backups.verify")}
                        </Button>
                        <a
                          className="inline-flex items-center gap-1 rounded-lg border border-slate-300
                            px-2.5 py-1.5 text-xs font-medium dark:border-ink-600"
                          href={`/api/v1/backups/${encodeURIComponent(backup.backup_id)}/download`}
                          download
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden />
                          {t("backups.downloadArchive")}
                        </a>
                        <Button size="sm" variant="secondary" loading={restoreMutation.isPending}
                                onClick={() => void onRestore(backup.backup_id)}>
                          {t("backups.restore")}
                        </Button>
                        <Button size="sm" variant="ghost" aria-label={t("common.delete")}
                                onClick={() => void onDelete(backup.backup_id)}>
                          <Trash2 className="h-3.5 w-3.5 text-danger" aria-hidden />
                        </Button>
                      </div>
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
