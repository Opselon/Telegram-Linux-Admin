/**
 * Typed API surface — mirrors src/api/schemas.py and the route modules 1:1.
 * Keep in sync with the backend; do not invent fields the API never returns.
 */

// --- auth --------------------------------------------------------------------

export interface AccountOut {
  account_id: number;
  username: string;
  role: string;
  permissions: string[];
  mfa_enabled: boolean;
}

export interface SetupStatus {
  required: boolean;
  token_required: boolean;
}

// --- servers -------------------------------------------------------------------

export interface ServerOut {
  id: number;
  alias: string;
  hostname: string;
  user: string;
  port: number;
  owner: boolean;
  host_key: string | null;
  created_at: string | null;
}

export interface ServerCreateInput {
  alias: string;
  hostname: string;
  user: string;
  password?: string;
  key_path?: string;
  port: number;
}

export interface ServerUpdateInput {
  hostname?: string;
  user?: string;
  password?: string;
  key_path?: string;
  port?: number;
}

export interface OkOut {
  ok: boolean;
  error: string | null;
}

export interface HostKeyStatus {
  [key: string]: unknown;
}

// --- monitoring ------------------------------------------------------------------

export interface DiskInfo {
  fs: string;
  mount: string;
  size: string;
  used: string;
  avail: string;
  used_percent: number;
}

export interface MonitoringSnapshot {
  cpu_percent: number;
  load: { "1": number; "5": number; "15": number };
  ram_total_mb: number;
  ram_used_mb: number;
  ram_percent: number;
  swap_total_mb: number;
  swap_percent: number;
  disks: DiskInfo[];
  os: string;
  kernel: string;
  interfaces: { name: string; rx_mb: number; tx_mb: number }[];
  sampled_at: number;
  stale?: boolean;
  error?: string;
}

export interface MonitoringSample {
  t: number;
  cpu: number;
  ram: number;
  disk: number;
}

// --- operations -------------------------------------------------------------------

export interface ServiceUnit {
  unit: string;
  load: string;
  active: string;
  sub: string;
  description: string;
}

export interface ProcessInfo {
  pid: number;
  ppid: number;
  user: string;
  cpu: number;
  mem: number;
  elapsed: string;
  command: string;
}

export interface ContainerInfo {
  id: string;
  name: string;
  image: string;
  state: string;
  status: string;
}

export interface ImageInfo {
  repository: string;
  tag: string;
  id: string;
  size: string;
}

export interface FirewallStatus {
  raw: string;
}

// --- files -----------------------------------------------------------------------

export type FileEntryType = "dir" | "link" | "file";

export interface FileEntry {
  name: string;
  type: FileEntryType;
  size: number;
  mtime: number;
  owner: string;
  perms: string;
}

export interface FileListing {
  path: string;
  entries: FileEntry[];
}

// --- commands / jobs ----------------------------------------------------------------

export type PlanKind = "executed" | "confirm" | "denied";

export interface PendingPlan {
  token: string;
  server_id: number;
  alias: string;
  command: string;
  risk: string;
  reason: string;
  expires_at: number;
}

export interface JobOut {
  id: string;
  alias: string;
  state: string;
  command: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  exit_ok: boolean | null;
  error: string | null;
}

export interface PlanOut {
  kind: PlanKind;
  reason: string;
  risk: string;
  rule: string;
  job: JobOut | null;
  pending: PendingPlan | null;
}

// --- backups ----------------------------------------------------------------------

export interface BackupOut {
  backup_id: string;
  created_at: string | null;
  key_version: string | null;
  schema_version: string | null;
  size: number;
  sha256: string;
  path: string;
}

export interface BackupVerifyResult {
  ok: boolean;
  issues: string[];
}

export interface BackupRestoreResult {
  ok: boolean;
  backup_id: string;
  safety_backup: string;
}

export interface BackupCreateResult {
  backup_id: string;
  file: string;
  size: number;
  sha256: string;
}

// --- security --------------------------------------------------------------------

export interface SecurityAccount {
  account_id: number;
  kind: string;
  external_id: string;
  display_name: string;
  role: string;
}

export interface SecurityOverview {
  access_mode: string;
  admins: number[];
  whitelisted: number[];
  accounts: SecurityAccount[];
}

export interface ManagedUser {
  account_id: number;
  kind: string;
  external_id: string;
  display_name: string;
  role: string;
  has_web_login: boolean;
  web_username: string | null;
  disabled: boolean;
}

/** Audit records: `{event, ...context}` — context keys vary per event type. */
export interface AuditEvent {
  event: string;
  [key: string]: unknown;
}

export interface LinkCodeResult {
  code: string;
  ttl_seconds: number;
}

export interface NotificationPrefs {
  disabled_categories: string[];
}

export interface AlertEvent {
  kind: string;
  scope: string;
  severity: string;
  at: number;
  [key: string]: unknown;
}

// --- realtime ----------------------------------------------------------------------

export interface StreamEvent {
  kind: string;
  [key: string]: unknown;
}
