import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Plus, Trash2, Zap } from "lucide-react";
import { servers as serversApi } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import type { ServerOut } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Card, EmptyState, ErrorState, Spinner } from "@/components/ui/Primitives";
import { useAuth } from "@/hooks/useAuth";
import { useConfirm } from "@/hooks/useConfirm";

export function ServersPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const query = useQuery({ queryKey: ["servers"], queryFn: serversApi.list });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["servers"] });

  const testMutation = useMutation({
    mutationFn: serversApi.test,
    onSuccess: () => invalidate(),
    onError: (error) => setActionError(messageOf(error)),
  });
  const removeMutation = useMutation({
    mutationFn: serversApi.remove,
    onSuccess: () => invalidate(),
    onError: (error) => setActionError(messageOf(error)),
  });

  const onDelete = async (server: ServerOut) => {
    const ok = await confirm({
      title: t("servers.deleteConfirmTitle"),
      body: t("servers.deleteConfirmBody", { alias: server.alias }),
      typedWord: server.alias,
      danger: true,
      confirmLabel: t("common.delete"),
    });
    if (ok) removeMutation.mutate(server.id);
  };

  const list = query.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-center justify-between gap-2">
        <h1 className="text-lg font-semibold tracking-tight">{t("servers.title")}</h1>
        {can("servers.create") && (
          <Button size="sm" onClick={() => setAdding(true)}>
            <Plus className="h-4 w-4" aria-hidden />
            {t("servers.addServer")}
          </Button>
        )}
      </header>

      {actionError && <p className="text-xs text-danger">{actionError}</p>}

      <Card>
        {query.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : query.isError ? (
          <ErrorState message={t("errors.network")} onRetry={() => void query.refetch()} />
        ) : list.length === 0 ? (
          <EmptyState title={t("states.emptyTitle")} hint={t("states.emptyServers")} />
        ) : (
          <ul className="flex flex-col gap-2">
            {list.map((server) => (
              <li
                key={server.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg
                  border border-slate-200 px-3 py-2.5 dark:border-ink-700"
              >
                <Link to={`/servers/${server.id}`} className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{server.alias}</span>
                  <span className="block truncate text-xs text-slate-400" dir="ltr">
                    {server.user}@{server.hostname}:{server.port}
                  </span>
                </Link>
                <div className="flex shrink-0 items-center gap-1.5">
                  {can("servers.connect") && (
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={testMutation.isPending && testMutation.variables === server.id}
                      onClick={() => testMutation.mutate(server.id)}
                    >
                      <Zap className="h-3.5 w-3.5" aria-hidden />
                      {t("servers.testConnection")}
                    </Button>
                  )}
                  {can("servers.update") && (
                    <Link
                      to={`/servers/${server.id}`}
                      className="inline-flex items-center justify-center rounded-lg px-2.5
                        py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100
                        dark:text-slate-300 dark:hover:bg-ink-800"
                    >
                      {t("common.details")}
                    </Link>
                  )}
                  {can("servers.delete") && (
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t("common.delete")}
                      onClick={() => void onDelete(server)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-danger" aria-hidden />
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <AddServerModal open={adding} onClose={() => setAdding(false)} onCreated={invalidate} />
    </div>
  );
}

function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function AddServerModal({ open, onClose, onCreated }: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [alias, setAlias] = useState("");
  const [hostname, setHostname] = useState("");
  const [user, setUser] = useState("root");
  const [port, setPort] = useState("22");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await serversApi.create({
        alias,
        hostname,
        user,
        port: Number(port) || 22,
        ...(password ? { password } : {}),
      });
      onCreated();
      setAlias("");
      setHostname("");
      setPassword("");
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errors.generic"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={t("servers.addServer")}>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("servers.alias")}
          <input className="input mt-1" required value={alias}
                 onChange={(event) => setAlias(event.target.value)} />
        </label>
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("servers.hostname")}
          <input className="input mt-1" required dir="ltr" value={hostname}
                 onChange={(event) => setHostname(event.target.value)} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("servers.user")}
            <input className="input mt-1" required dir="ltr" value={user}
                   onChange={(event) => setUser(event.target.value)} />
          </label>
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            {t("servers.port")}
            <input className="input mt-1" type="number" min={1} max={65535} dir="ltr"
                   value={port} onChange={(event) => setPort(event.target.value)} />
          </label>
        </div>
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("servers.password")}
          <input className="input mt-1" type="password" dir="ltr" value={password}
                 onChange={(event) => setPassword(event.target.value)} />
          <span className="mt-1 block font-normal">{t("servers.passwordLeaveBlank")}</span>
        </label>
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" loading={busy}>{t("common.create")}</Button>
        </div>
      </form>
    </Modal>
  );
}
