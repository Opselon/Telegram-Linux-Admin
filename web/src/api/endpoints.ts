/**
 * Typed endpoint wrappers — one function per API route, shaped after
 * src/api/routes_*.py. No mocking; every call hits /api/v1.
 */
import { api, apiBlob } from "./client";
import type {
  AccountOut,
  AlertEvent,
  AuditEvent,
  BackupCreateResult,
  BackupOut,
  BackupRestoreResult,
  BackupVerifyResult,
  ContainerInfo,
  FileListing,
  FirewallStatus,
  HostKeyStatus,
  ImageInfo,
  JobOut,
  LinkCodeResult,
  ManagedUser,
  MonitoringSample,
  MonitoringSnapshot,
  NotificationPrefs,
  OkOut,
  PlanOut,
  ProcessInfo,
  SecurityOverview,
  ServerCreateInput,
  ServerOut,
  ServerUpdateInput,
  ServiceUnit,
  SetupStatus,
} from "./types";

// --- auth --------------------------------------------------------------------

export const auth = {
  setupStatus: () => api<SetupStatus>("/auth/setup", { authRoute: true }),
  setup: (payload: { username: string; password: string; setup_token?: string }) =>
    api<AccountOut>("/auth/setup", { method: "POST", json: payload, authRoute: true }),
  login: (payload: { username: string; password: string }) =>
    api<AccountOut>("/auth/login", { method: "POST", json: payload, authRoute: true }),
  mfa: (payload: { pending_token: string; code: string }) =>
    api<AccountOut>("/auth/mfa", { method: "POST", json: payload, authRoute: true }),
  logout: () => api<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  me: () => api<AccountOut>("/auth/me"),
  changePassword: (payload: { current: string; new: string }) =>
    api<{ ok: boolean; sessions_revoked: boolean }>("/auth/password", { method: "POST", json: payload }),
  mfaEnable: () =>
    api<{ secret: string; otpauth: string }>("/auth/mfa/enable", { method: "POST" }),
  // NOTE: backend takes `code` as a query parameter here (bare str arg).
  mfaActivate: (code: string) =>
    api<{ ok: boolean }>("/auth/mfa/activate", { method: "POST", query: { code }, authRoute: false }),
  mfaDisable: () => api<{ ok: boolean }>("/auth/mfa/disable", { method: "POST" }),
};

// --- servers -------------------------------------------------------------------

export const servers = {
  list: () => api<ServerOut[]>("/servers"),
  get: (id: number) => api<ServerOut>(`/servers/${id}`),
  create: (payload: ServerCreateInput) =>
    api<ServerOut>("/servers", { method: "POST", json: payload }),
  update: (id: number, payload: ServerUpdateInput) =>
    api<ServerOut>(`/servers/${id}`, { method: "PATCH", json: payload }),
  rename: (id: number, alias: string) =>
    api<ServerOut>(`/servers/${id}/rename`, { method: "POST", json: { alias } }),
  remove: (id: number) => api<OkOut>(`/servers/${id}`, { method: "DELETE" }),
  connect: (id: number) => api<OkOut>(`/servers/${id}/connect`, { method: "POST" }),
  disconnect: (id: number) => api<OkOut>(`/servers/${id}/disconnect`, { method: "POST" }),
  test: (id: number) => api<OkOut>(`/servers/${id}/test`, { method: "POST" }),
  hostkey: (id: number) => api<HostKeyStatus>(`/servers/${id}/hostkey`),
};

// --- monitoring ------------------------------------------------------------------

export const monitoring = {
  snapshot: (serverId: number, refresh = false) =>
    api<MonitoringSnapshot>(`/servers/${serverId}/monitoring`, { query: { refresh } }),
  history: (serverId: number) =>
    api<MonitoringSample[]>(`/servers/${serverId}/monitoring/history`),
};

// --- operations --------------------------------------------------------------------

export const operations = {
  services: (serverId: number) => api<ServiceUnit[]>(`/servers/${serverId}/services`),
  serviceAction: (serverId: number, name: string, action: string) =>
    api<OkOut>(`/servers/${serverId}/services/action`, {
      method: "POST",
      json: { name, action },
    }),
  processes: (serverId: number, search = "", sort = "cpu") =>
    api<ProcessInfo[]>(`/servers/${serverId}/processes`, { query: { search, sort } }),
  killProcess: (serverId: number, pid: number, signal: "TERM" | "KILL") =>
    api<OkOut>(`/servers/${serverId}/processes/kill`, {
      method: "POST",
      json: { pid, signal },
    }),
  dockerContainers: (serverId: number) =>
    api<ContainerInfo[]>(`/servers/${serverId}/docker/containers`),
  dockerAction: (serverId: number, container: string, action: string) =>
    api<OkOut>(`/servers/${serverId}/docker/action`, {
      method: "POST",
      json: { container, action },
    }),
  dockerLogs: (serverId: number, container: string, tail = 200) =>
    api<{ container: string; logs: string }>(`/servers/${serverId}/docker/logs`, {
      query: { container, tail },
    }),
  dockerImages: (serverId: number) =>
    api<ImageInfo[]>(`/servers/${serverId}/docker/images`),
  firewallRules: (serverId: number) =>
    api<FirewallStatus>(`/servers/${serverId}/firewall`),
  firewallAdd: (serverId: number, rule: string, direction: "allow" | "deny") =>
    api<OkOut>(`/servers/${serverId}/firewall/rules`, {
      method: "POST",
      json: { rule, direction },
    }),
  firewallDelete: (serverId: number, rule: string) =>
    api<OkOut>(`/servers/${serverId}/firewall/delete`, {
      method: "POST",
      json: { rule },
    }),
  packageUpdate: (serverId: number) =>
    api<OkOut>(`/servers/${serverId}/packages/update`, { method: "POST" }),
  packageUpgrade: (serverId: number) =>
    api<OkOut>(`/servers/${serverId}/packages/upgrade`, { method: "POST" }),
  packageInstall: (serverId: number, name: string) =>
    api<OkOut>(`/servers/${serverId}/packages/install`, {
      method: "POST",
      json: { name },
    }),
};

// --- files -----------------------------------------------------------------------

export const files = {
  list: (serverId: number, path: string) =>
    api<FileListing>(`/servers/${serverId}/files`, { query: { path } }),
  download: (serverId: number, path: string) =>
    apiBlob(`/servers/${serverId}/files/content`, { path }),
  upload: (serverId: number, dirPath: string, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return api<{ ok: boolean; path: string }>(`/servers/${serverId}/files/content`, {
      method: "POST",
      bodyInit: form,
      query: { path: dirPath },
    });
  },
  mkdir: (serverId: number, path: string) =>
    api<{ ok: boolean }>(`/servers/${serverId}/files/mkdir`, { method: "POST", json: { path } }),
  rename: (serverId: number, path: string, newName: string) =>
    api<{ ok: boolean }>(`/servers/${serverId}/files/rename`, {
      method: "POST",
      json: { path, new_name: newName },
    }),
  remove: (serverId: number, path: string) =>
    api<{ ok: boolean }>(`/servers/${serverId}/files/delete`, { method: "POST", json: { path } }),
};

// --- commands / jobs ----------------------------------------------------------------

export const commands = {
  execute: (serverId: number, command: string) =>
    api<PlanOut>("/commands", { method: "POST", json: { server_id: serverId, command } }),
  confirm: (token: string) =>
    api<PlanOut>("/commands/confirm", { method: "POST", json: { token } }),
};

export const jobs = {
  list: () => api<JobOut[]>("/jobs"),
  get: (jobId: string) => api<JobOut>(`/jobs/${encodeURIComponent(jobId)}`),
  cancel: (jobId: string) =>
    api<JobOut>(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
};

// --- backups ----------------------------------------------------------------------

export const backups = {
  list: () => api<BackupOut[]>("/backups"),
  create: () => api<BackupCreateResult>("/backups", { method: "POST" }),
  verify: (backupId: string) =>
    api<BackupVerifyResult>(`/backups/${encodeURIComponent(backupId)}/verify`, { method: "POST" }),
  restore: (backupId: string) =>
    api<BackupRestoreResult>(`/backups/${encodeURIComponent(backupId)}/restore`, {
      method: "POST",
      json: { confirm: true },
    }),
  remove: (backupId: string) =>
    api<{ ok: boolean }>(`/backups/${encodeURIComponent(backupId)}`, { method: "DELETE" }),
  download: (backupId: string) => apiBlob(`/backups/${encodeURIComponent(backupId)}/download`),
};

// --- security ---------------------------------------------------------------------

export const security = {
  overview: () => api<SecurityOverview>("/security/overview"),
  events: (limit = 100) => api<AuditEvent[]>("/security/events", { query: { limit } }),
  setAccessMode: (mode: string) =>
    api<{ ok: boolean; mode: string }>("/settings/access-mode", { method: "PUT", json: { mode } }),
  setWhitelist: (payload: { whitelisted?: number[]; admins?: number[] }) =>
    api<{ ok: boolean }>("/settings/whitelist", { method: "PUT", json: payload }),
  users: () => api<ManagedUser[]>("/users"),
  createUser: (payload: { username: string; password: string; role: string }) =>
    api<ManagedUser>("/users", { method: "POST", json: payload }),
  setRole: (accountId: number, role: string) =>
    api<{ ok: boolean }>(`/users/${accountId}/role`, { method: "POST", json: { role } }),
  setDisabled: (accountId: number, disabled: boolean) =>
    api<{ ok: boolean }>(`/users/${accountId}/disabled`, {
      method: "POST",
      json: { disabled },
    }),
  resetPassword: (accountId: number, password: string) =>
    api<{ ok: boolean; sessions_revoked: boolean }>(`/users/${accountId}/password`, {
      method: "POST",
      json: { password },
    }),
  deleteUser: (accountId: number) =>
    api<{ ok: boolean }>(`/users/${accountId}`, { method: "DELETE" }),
  createLinkCode: (accountId?: number) =>
    api<LinkCodeResult>("/bale/link-code", { method: "POST", json: { account_id: accountId ?? null } }),
};

// --- notifications ------------------------------------------------------------------

export const notifications = {
  prefs: () => api<NotificationPrefs>("/notifications/prefs"),
  setPrefs: (disabledCategories: string[]) =>
    api<{ ok: boolean }>("/notifications/prefs", {
      method: "PUT",
      json: { disabled_categories: disabledCategories },
    }),
  recent: (limit = 50) => api<AlertEvent[]>("/notifications/recent", { query: { limit } }),
};
