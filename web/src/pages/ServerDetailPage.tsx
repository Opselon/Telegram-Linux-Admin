import { useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  ArrowUp, Cpu, Download, FolderPlus, HardDrive, MemoryStick, Play,
  Plus, RefreshCcw, Square, Trash2, Upload, X,
} from "lucide-react";
import {
  files as filesApi, monitoring, operations, servers as serversApi,
} from "@/api/endpoints";
import { ApiError } from "@/api/client";
import type { FileEntry, FileListing } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Badge, Card, EmptyState, ErrorState, Spinner } from "@/components/ui/Primitives";
import { useAuth } from "@/hooks/useAuth";
import { useConfirm } from "@/hooks/useConfirm";
import { usePageVisible } from "@/hooks/usePageVisible";
import { formatBytes, timeAgo } from "@/lib/format";

const TABS = ["overview", "services", "processes", "docker", "firewall", "files"] as const;
type Tab = (typeof TABS)[number];

export function ServerDetailPage() {
  const { serverId } = useParams();
  const id = Number(serverId);
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("overview");

  const server = useQuery({ queryKey: ["servers"], queryFn: serversApi.list });
  const current = (server.data ?? []).find((s) => s.id === id);

  if (server.isLoading) return <Spinner label={t("common.loading")} />;
  if (!current) {
    return (
      <EmptyState
        title={t("errors.notFound")}
        hint=""
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold tracking-tight">{current.alias}</h1>
          <p className="truncate text-xs text-slate-400" dir="ltr">
            {current.user}@{current.hostname}:{current.port}
          </p>
        </div>
        <Link to="/servers" className="text-xs text-accent hover:underline">
          ← {t("nav.servers")}
        </Link>
      </header>

      <nav className="flex gap-1 overflow-x-auto pb-1" aria-label="tabs">
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setTab(name)}
            className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium
              transition-colors ${tab === name
                ? "bg-accent-soft text-accent"
                : "text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-ink-800"}`}
          >
            {t(`detail.${name}`)}
          </button>
        ))}
      </nav>

      {tab === "overview" && <OverviewTab serverId={id} />}
      {tab === "services" && <ServicesTab serverId={id} />}
      {tab === "processes" && <ProcessesTab serverId={id} />}
      {tab === "docker" && <DockerTab serverId={id} />}
      {tab === "firewall" && <FirewallTab serverId={id} />}
      {tab === "files" && <FilesTab serverId={id} />}
    </div>
  );
}

// --- Overview / monitoring -----------------------------------------------------

function OverviewTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const query = useQuery({
    queryKey: ["monitoring", serverId],
    queryFn: () => monitoring.snapshot(serverId),
    refetchInterval: visible ? 15_000 : false,
    retry: 1,
  });
  const history = useQuery({
    queryKey: ["monitoring", "history", serverId],
    queryFn: () => monitoring.history(serverId),
  });
  const refresh = useMutation({
    mutationFn: () => monitoring.snapshot(serverId, true),
    onSuccess: () => void query.refetch(),
  });

  if (query.isLoading) return <Spinner label={t("common.loading")} />;
  if (query.isError || !query.data) {
    return <ErrorState message={t("detail.noSnapshot")} onRetry={() => void query.refetch()} />;
  }

  const snap = query.data;
  const samples = history.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric icon={<Cpu className="h-4 w-4" aria-hidden />} label={t("detail.cpu")}
                percent={snap.cpu_percent} />
        <Metric icon={<MemoryStick className="h-4 w-4" aria-hidden />} label={t("detail.ram")}
                percent={snap.ram_percent}
                hint={`${formatBytes(snap.ram_used_mb * 1024 * 1024)} / ${formatBytes(snap.ram_total_mb * 1024 * 1024)}`} />
        <Metric icon={<RefreshCcw className="h-4 w-4" aria-hidden />} label={t("detail.swap")}
                percent={snap.swap_percent} />
        <Metric icon={<HardDrive className="h-4 w-4" aria-hidden />}
                label={t("detail.disk")}
                percent={snap.disks[0]?.used_percent ?? 0}
                hint={snap.disks[0]?.mount} />
      </div>

      <Card
        title={t("detail.history")}
        actions={
          <Button size="sm" variant="secondary" loading={refresh.isPending}
                  onClick={() => refresh.mutate()}>
            <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
            {t("common.refresh")}
          </Button>
        }
      >
        <Sparkline samples={samples} />
        <p className="mt-2 text-xs text-slate-400">{t("dashboard.snapshotHint")}</p>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2">
        <Card title={t("detail.load")}>
          <p className="font-mono text-sm" dir="ltr">
            {snap.load["1"]} / {snap.load["5"]} / {snap.load["15"]}
          </p>
          <p className="mt-1 text-xs text-slate-400">{snap.os} · {snap.kernel}</p>
        </Card>
        <Card title={t("detail.interfaces")}>
          <ul className="flex flex-col gap-1 text-xs" dir="ltr">
            {snap.interfaces.map((net) => (
              <li key={net.name} className="flex justify-between gap-2">
                <span className="font-mono">{net.name}</span>
                <span className="text-slate-400">
                  ↓ {formatBytes(net.rx_mb * 1024 * 1024)} · ↑ {formatBytes(net.tx_mb * 1024 * 1024)}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function Metric({ icon, label, percent, hint }: {
  icon: React.ReactNode;
  label: string;
  percent: number;
  hint?: string;
}) {
  const tone = percent >= 90 ? "bg-danger" : percent >= 75 ? "bg-warn" : "bg-ok";
  return (
    <div className="panel p-4">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-xs font-medium text-slate-400">
          {icon}{label}
        </span>
        <span className="text-sm font-semibold tabular-nums">{percent}%</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-ink-800">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      {hint && <p className="mt-1 truncate text-[10px] text-slate-400" dir="ltr">{hint}</p>}
    </div>
  );
}

function Sparkline({ samples }: {
  samples: { t: number; cpu: number; ram: number; disk: number }[];
}) {
  if (samples.length < 2) {
    return <p className="py-6 text-center text-xs text-slate-400">—</p>;
  }
  const width = 100;
  const height = 32;
  const path = (pick: (sample: { cpu: number; ram: number; disk: number }) => number) =>
    samples
      .map((sample, index) => {
        const x = (index / (samples.length - 1)) * width;
        const y = height - (Math.max(0, Math.min(100, pick(sample))) / 100) * height;
        return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-16 w-full" preserveAspectRatio="none"
         role="img" aria-hidden>
      <path d={path((sample) => sample.cpu)} fill="none" stroke="#3B82F6" strokeWidth="1.5" />
      <path d={path((sample) => sample.ram)} fill="none" stroke="#22C55E" strokeWidth="1.5" />
      <path d={path((sample) => sample.disk)} fill="none" stroke="#F59E0B" strokeWidth="1.5" />
    </svg>
  );
}

// --- Services ---------------------------------------------------------------------

function ServicesTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const [error, setError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["services", serverId],
    queryFn: () => operations.services(serverId),
    refetchInterval: 20_000,
  });

  const action = useMutation({
    mutationFn: ({ name, act }: { name: string; act: string }) =>
      operations.serviceAction(serverId, name, act),
    onSuccess: () => void query.refetch(),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });

  const onAction = async (name: string, act: string) => {
    if (act === "stop" || act === "restart" || act === "disable") {
      const ok = await confirm({
        title: t("common.confirm"),
        body: `${act} ${name}?`,
        danger: act === "stop",
        confirmLabel: act,
      });
      if (!ok) return;
    }
    action.mutate({ name, act });
  };

  if (query.isLoading) return <Spinner label={t("common.loading")} />;
  const list = query.data ?? [];

  return (
    <Card>
      {error && <p className="mb-2 text-xs text-danger">{error}</p>}
      {list.length === 0 ? (
        <EmptyState title={t("states.emptyTitle")} />
      ) : (
        <div className="overflow-x-auto">
          <table className="table-base">
            <thead>
              <tr>
                <th className="table-th">{t("detail.serviceName")}</th>
                <th className="table-th">{t("detail.active")}</th>
                <th className="table-th">{t("detail.description")}</th>
                {can("services.control") && <th className="table-th">{t("common.actions")}</th>}
              </tr>
            </thead>
            <tbody>
              {list.map((unit) => (
                <tr key={unit.unit}>
                  <td className="table-td font-mono text-xs" dir="ltr">{unit.unit}</td>
                  <td className="table-td">
                    <Badge tone={unit.active === "active" ? "ok" : unit.active === "failed" ? "danger" : "neutral"}>
                      {unit.active}
                    </Badge>
                  </td>
                  <td className="table-td max-w-xs truncate text-xs text-slate-400">
                    {unit.description}
                  </td>
                  {can("services.control") && (
                    <td className="table-td">
                      <div className="flex flex-wrap gap-1">
                        {(["start", "stop", "restart"] as const).map((act) => (
                          <Button key={act} size="sm" variant="ghost"
                                  onClick={() => void onAction(unit.unit, act)}>
                            {act === "start" ? <Play className="h-3.5 w-3.5" aria-hidden />
                              : act === "stop" ? <Square className="h-3.5 w-3.5" aria-hidden />
                                : <RefreshCcw className="h-3.5 w-3.5" aria-hidden />}
                          </Button>
                        ))}
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// --- Processes ----------------------------------------------------------------------

function ProcessesTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("cpu");

  const query = useQuery({
    queryKey: ["processes", serverId, search, sort],
    queryFn: () => operations.processes(serverId, search, sort),
    refetchInterval: 15_000,
  });

  const kill = useMutation({
    mutationFn: (pid: number) => operations.killProcess(serverId, pid, "TERM"),
    onSuccess: () => void query.refetch(),
  });

  const onKill = async (pid: number) => {
    const ok = await confirm({
      title: t("detail.killConfirmTitle"),
      body: t("detail.killConfirmBody", { signal: "TERM", pid }),
      danger: true,
      confirmLabel: t("detail.killProcess"),
    });
    if (ok) kill.mutate(pid);
  };

  const list = query.data ?? [];

  return (
    <Card>
      <div className="mb-3 flex flex-wrap gap-2">
        <input
          className="input max-w-xs" dir="ltr" placeholder={t("common.search")}
          value={search} onChange={(event) => setSearch(event.target.value)}
        />
        <select className="input max-w-[10rem]" value={sort}
                onChange={(event) => setSort(event.target.value)}>
          <option value="cpu">CPU</option>
          <option value="mem">MEM</option>
          <option value="pid">PID</option>
        </select>
      </div>
      {query.isLoading ? (
        <Spinner label={t("common.loading")} />
      ) : list.length === 0 ? (
        <EmptyState title={t("states.emptyTitle")} />
      ) : (
        <div className="overflow-x-auto">
          <table className="table-base">
            <thead>
              <tr>
                <th className="table-th">{t("detail.pid")}</th>
                <th className="table-th">{t("common.name")}</th>
                <th className="table-th">{t("detail.cpuPct")}</th>
                <th className="table-th">{t("detail.memPct")}</th>
                {can("processes.kill") && <th className="table-th">{t("common.actions")}</th>}
              </tr>
            </thead>
            <tbody>
              {list.map((process) => (
                <tr key={process.pid}>
                  <td className="table-td font-mono text-xs" dir="ltr">{process.pid}</td>
                  <td className="table-td max-w-sm truncate font-mono text-xs" dir="ltr"
                      title={`${process.user}: ${process.command}`}>
                    {process.command}
                  </td>
                  <td className="table-td tabular-nums">{process.cpu}%</td>
                  <td className="table-td tabular-nums">{process.mem}%</td>
                  {can("processes.kill") && (
                    <td className="table-td">
                      <Button size="sm" variant="ghost" onClick={() => void onKill(process.pid)}>
                        <X className="h-3.5 w-3.5 text-danger" aria-hidden />
                      </Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// --- Docker ----------------------------------------------------------------------

function DockerTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [logs, setLogs] = useState<{ name: string; text: string } | null>(null);

  const containers = useQuery({
    queryKey: ["docker", "containers", serverId],
    queryFn: () => operations.dockerContainers(serverId),
    refetchInterval: 20_000,
  });
  const images = useQuery({
    queryKey: ["docker", "images", serverId],
    queryFn: () => operations.dockerImages(serverId),
  });

  const action = useMutation({
    mutationFn: ({ name, act }: { name: string; act: string }) =>
      operations.dockerAction(serverId, name, act),
    onSuccess: () => void containers.refetch(),
  });
  const showLogs = useMutation({
    mutationFn: (name: string) => operations.dockerLogs(serverId, name),
    onSuccess: (result, name) => setLogs({ name, text: result.logs }),
  });

  const list = containers.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Card title={t("detail.containers")}>
        {containers.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : list.length === 0 ? (
          <EmptyState title={t("states.emptyTitle")} />
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th className="table-th">{t("detail.container")}</th>
                  <th className="table-th">{t("detail.state")}</th>
                  {can("docker.control") && <th className="table-th">{t("common.actions")}</th>}
                </tr>
              </thead>
              <tbody>
                {list.map((container) => (
                  <tr key={container.id}>
                    <td className="table-td">
                      <span className="font-mono text-xs" dir="ltr">{container.name}</span>
                      <span className="block text-[10px] text-slate-400" dir="ltr">{container.image}</span>
                    </td>
                    <td className="table-td">
                      <Badge tone={container.state === "running" ? "ok" : "neutral"}>
                        {container.state}
                      </Badge>
                    </td>
                    <td className="table-td">
                      <div className="flex flex-wrap gap-1">
                        {can("docker.control") && (["start", "stop", "restart"] as const).map((act) => (
                          <Button key={act} size="sm" variant="ghost"
                                  onClick={() => action.mutate({ name: container.name, act })}>
                            {act}
                          </Button>
                        ))}
                        <Button size="sm" variant="ghost"
                                onClick={() => showLogs.mutate(container.name)}>
                          {t("detail.logs")}
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

      <Card title={t("detail.images")}>
        {(images.data ?? []).length === 0 ? (
          <EmptyState title={t("states.emptyTitle")} />
        ) : (
          <ul className="flex flex-col gap-1 text-xs" dir="ltr">
            {(images.data ?? []).map((image) => (
              <li key={image.id} className="flex justify-between gap-2">
                <span className="truncate font-mono">{image.repository}:{image.tag}</span>
                <span className="shrink-0 text-slate-400">{image.size}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Modal open={logs !== null} onClose={() => setLogs(null)}
             title={`${t("detail.logs")} — ${logs?.name ?? ""}`}>
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-ink-950 p-3
          font-mono text-[11px] text-slate-300" dir="ltr">
          {logs?.text}
        </pre>
      </Modal>
    </div>
  );
}

// --- Firewall ----------------------------------------------------------------------

function FirewallTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const [rule, setRule] = useState("");
  const [direction, setDirection] = useState<"allow" | "deny">("allow");
  const [error, setError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["firewall", serverId],
    queryFn: () => operations.firewallRules(serverId),
  });

  const add = useMutation({
    mutationFn: () => operations.firewallAdd(serverId, rule, direction),
    onSuccess: () => {
      setRule("");
      void query.refetch();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });
  const remove = useMutation({
    mutationFn: (target: string) => operations.firewallDelete(serverId, target),
    onSuccess: () => void query.refetch(),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });

  const onDelete = async (target: string) => {
    const ok = await confirm({
      title: t("detail.deleteRuleConfirmTitle"),
      body: t("detail.deleteRuleConfirmBody", { rule: target }),
      danger: true,
      confirmLabel: t("common.delete"),
    });
    if (ok) remove.mutate(target);
  };

  return (
    <div className="flex flex-col gap-4">
      {can("firewall.modify") && (
        <Card title={t("detail.addRule")}>
          <div className="flex flex-wrap items-end gap-2">
            <input
              className="input max-w-xs font-mono" dir="ltr"
              placeholder={t("detail.rulePlaceholder")} value={rule}
              onChange={(event) => setRule(event.target.value)}
            />
            <select className="input max-w-[8rem]" value={direction}
                    onChange={(event) => setDirection(event.target.value as "allow" | "deny")}>
              <option value="allow">{t("detail.allow")}</option>
              <option value="deny">{t("detail.deny")}</option>
            </select>
            <Button size="sm" loading={add.isPending} disabled={!rule.trim()}
                    onClick={() => add.mutate()}>
              <Plus className="h-4 w-4" aria-hidden />
              {t("common.add")}
            </Button>
            <Button size="sm" variant="secondary" loading={remove.isPending} disabled={!rule.trim()} onClick={() => void onDelete(rule.trim())}>
              <Trash2 className="h-4 w-4" aria-hidden />
              {t("common.delete")}
            </Button>
          </div>
          <p className="mt-2 text-xs text-slate-400">{t("detail.firewallDisabledHint")}</p>
          {error && <p className="mt-2 text-xs text-danger">{error}</p>}
        </Card>
      )}

      <Card title={t("detail.firewallRaw")}>
        {query.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : (
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-ink-950 p-3
            font-mono text-[11px] text-slate-300" dir="ltr">
            {query.data?.raw}
          </pre>
        )}
      </Card>
    </div>
  );
}

// --- Files ----------------------------------------------------------------------

function FilesTab({ serverId }: { serverId: number }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const [path, setPath] = useState("/");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const query = useQuery({
    queryKey: ["files", serverId, path],
    queryFn: () => filesApi.list(serverId, path),
    retry: 1,
  });

  const invalidate = () => void query.refetch();

  const mkdir = useMutation({
    mutationFn: (name: string) => filesApi.mkdir(serverId, joinPath(path, name)),
    onSuccess: () => {
      setMkdirOpen(false);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });
  const remove = useMutation({
    mutationFn: (target: string) => filesApi.remove(serverId, target),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : t("errors.generic")),
  });

  const onDelete = async (entry: FileEntry) => {
    const ok = await confirm({
      title: t("detail.deleteFileConfirmTitle"),
      body: t("detail.deleteFileConfirmBody", { name: entry.name }),
      danger: true,
      confirmLabel: t("common.delete"),
    });
    if (ok) remove.mutate(joinPath(path, entry.name));
  };

  const onUpload = async (file: File) => {
    if (file.size > 50 * 1024 * 1024) {
      setError(t("detail.fileTooLarge"));
      return;
    }
    setUploading(true);
    setError(null);
    try {
      await filesApi.upload(serverId, path, file);
      setNotice(t("detail.downloadStart"));
      invalidate();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errors.generic"));
    } finally {
      setUploading(false);
    }
  };

  const listing: FileListing | undefined = query.data;
  const crumbs = useMemo(() => splitPath(path), [path]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-1.5 text-xs" dir="ltr">
          {crumbs.map((crumb) => (
            <button
              key={crumb.path}
              type="button"
              onClick={() => setPath(crumb.path)}
              className="rounded px-1.5 py-0.5 font-mono text-accent hover:bg-accent-soft"
            >
              {crumb.label}/
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {query.data && path !== "/" && (
            <Button size="sm" variant="secondary"
                    onClick={() => setPath(parentPath(path))}>
              <ArrowUp className="h-3.5 w-3.5" aria-hidden />
              ..
            </Button>
          )}
          {can("files.write") && (
            <Button size="sm" variant="secondary" onClick={() => setMkdirOpen(true)}>
              <FolderPlus className="h-3.5 w-3.5" aria-hidden />
              {t("detail.newFolder")}
            </Button>
          )}
          {can("files.upload") && (
            <>
              <input
                ref={inputRef} type="file" className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void onUpload(file);
                  event.target.value = "";
                }}
              />
              <Button size="sm" variant="secondary" loading={uploading}
                      onClick={() => inputRef.current?.click()}>
                <Upload className="h-3.5 w-3.5" aria-hidden />
                {t("common.upload")}
              </Button>
            </>
          )}
        </div>
        {error && <p className="mt-2 text-xs text-danger">{error}</p>}
        {notice && <p className="mt-2 text-xs text-ok">{notice}</p>}
      </Card>

      <Card>
        {query.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : query.isError ? (
          <ErrorState message={t("errors.notFound")} onRetry={() => void query.refetch()} />
        ) : (listing?.entries ?? []).length === 0 ? (
          <EmptyState title={t("detail.emptyDir")} />
        ) : (
          <table className="table-base">
            <thead>
              <tr>
                <th className="table-th">{t("common.name")}</th>
                <th className="table-th">{t("common.size")}</th>
                <th className="table-th">{t("common.modified")}</th>
                <th className="table-th">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {(listing?.entries ?? []).map((entry) => (
                <tr key={entry.name}>
                  <td className="table-td">
                    {entry.type === "dir" ? (
                      <button
                        type="button"
                        className="font-mono text-xs text-accent hover:underline"
                        onClick={() => setPath(joinPath(path, entry.name))}
                      >
                        {entry.name}/
                      </button>
                    ) : (
                      <span className="font-mono text-xs" dir="ltr">{entry.name}</span>
                    )}
                  </td>
                  <td className="table-td text-xs tabular-nums">
                    {entry.type === "dir" ? "—" : formatBytes(entry.size)}
                  </td>
                  <td className="table-td text-xs text-slate-400">
                    {entry.mtime ? timeAgo(entry.mtime) : "—"}
                  </td>
                  <td className="table-td">
                    <div className="flex gap-1">
                      {entry.type === "file" && can("files.download") && (
                        <button
                          type="button"
                          aria-label={t("common.download")}
                          className="rounded p-1 text-slate-400 hover:bg-slate-100
                            dark:hover:bg-ink-800"
                          onClick={() => void downloadFile(serverId, joinPath(path, entry.name))}
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden />
                        </button>
                      )}
                      {can("files.delete") && (
                        <button
                          type="button"
                          aria-label={t("common.delete")}
                          className="rounded p-1 text-slate-400 hover:bg-slate-100
                            dark:hover:bg-ink-800"
                          onClick={() => void onDelete(entry)}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-danger" aria-hidden />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <MkdirModal open={mkdirOpen} onClose={() => setMkdirOpen(false)}
                  onCreate={(name) => mkdir.mutate(name)} creating={mkdir.isPending} />
    </div>
  );
}

async function downloadFile(serverId: number, path: string) {
  const blob = await filesApi.download(serverId, path);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = path.split("/").pop() || "download";
  anchor.click();
  URL.revokeObjectURL(url);
}

function MkdirModal({ open, onClose, onCreate, creating }: {
  open: boolean;
  onClose: () => void;
  onCreate: (name: string) => void;
  creating: boolean;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  return (
    <Modal open={open} onClose={onClose} title={t("detail.newFolder")}>
      <div className="flex flex-col gap-3">
        <input
          className="input" dir="ltr" placeholder={t("detail.folderName")} value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>{t("common.cancel")}</Button>
          <Button loading={creating} disabled={!name.trim()}
                  onClick={() => onCreate(name.trim())}>
            {t("common.create")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function joinPath(base: string, name: string): string {
  return base.endsWith("/") ? base + name : base + "/" + name;
}

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return "/" + parts.join("/");
}

function splitPath(path: string): { label: string; path: string }[] {
  const crumbs = [{ label: "", path: "/" }];
  const parts = path.split("/").filter(Boolean);
  let acc = "";
  for (const part of parts) {
    acc += "/" + part;
    crumbs.push({ label: part, path: acc });
  }
  return crumbs;
}
