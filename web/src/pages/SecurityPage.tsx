import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { KeyRound } from "lucide-react";
import { security as securityApi } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Badge, Card, EmptyState, Spinner } from "@/components/ui/Primitives";
import { useAuth } from "@/hooks/useAuth";
import { useConfirm } from "@/hooks/useConfirm";
import { timeAgo } from "@/lib/format";

export function SecurityPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const confirm = useConfirm();
  const queryClient = useQueryClient();

  const overview = useQuery({ queryKey: ["security", "overview"], queryFn: securityApi.overview });
  const events = useQuery({ queryKey: ["security", "events"], queryFn: () => securityApi.events(100) });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: securityApi.users,
    enabled: can("users.manage"),
  });

  const [showCreate, setShowCreate] = useState(false);
  const [linkCode, setLinkCode] = useState<string | null>(null);

  const invalidateUsers = () => void queryClient.invalidateQueries({ queryKey: ["users"] });
  const invalidateOverview = () =>
    void queryClient.invalidateQueries({ queryKey: ["security"] });

  const modeMutation = useMutation({
    mutationFn: securityApi.setAccessMode,
    onSuccess: invalidateOverview,
  });
  const whitelistMutation = useMutation({
    mutationFn: securityApi.setWhitelist,
    onSuccess: invalidateOverview,
  });
  const roleMutation = useMutation({
    mutationFn: ({ id, role }: { id: number; role: string }) => securityApi.setRole(id, role),
    onSuccess: () => {
      invalidateUsers();
      invalidateOverview();
    },
  });
  const disableMutation = useMutation({
    mutationFn: ({ id, disabled }: { id: number; disabled: boolean }) =>
      securityApi.setDisabled(id, disabled),
    onSuccess: invalidateUsers,
  });
  const deleteMutation = useMutation({
    mutationFn: securityApi.deleteUser,
    onSuccess: () => {
      invalidateUsers();
      invalidateOverview();
    },
  });
  const linkCodeMutation = useMutation({
    mutationFn: () => securityApi.createLinkCode(undefined),
    onSuccess: (result) => setLinkCode(result.code),
  });

  const onSetMode = async (mode: string) => {
    const ok = await confirm({
      title: t("security.accessMode"),
      body: t("security.roleChangeWarning"),
      danger: true,
      confirmLabel: t("common.save"),
    });
    if (ok) modeMutation.mutate(mode);
  };

  const onDeleteUser = async (name: string, id: number) => {
    const ok = await confirm({
      title: t("security.deleteConfirmTitle"),
      body: t("security.deleteConfirmBody", { name }),
      danger: true,
      confirmLabel: t("common.delete"),
    });
    if (ok) deleteMutation.mutate(id);
  };

  const data = overview.data;
  const canManage = can("security.manage");

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">{t("security.title")}</h1>
      </header>

      {data && (
        <Card title={t("security.accessMode")}>
          <div className="flex flex-wrap gap-2">
            {(["admin_only", "whitelist", "open"] as const).map((mode) => (
              <Button
                key={mode}
                size="sm"
                variant={data.access_mode === mode ? "primary" : "secondary"}
                disabled={!canManage}
                onClick={() => void onSetMode(mode)}
              >
                {t(`security.modes.${mode}`)}
              </Button>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-400">{t("security.accessModeHint")}</p>
        </Card>
      )}

      {data && canManage && (
        <Card title={t("security.whitelist")}>
          <WhitelistEditor
            initialAdmins={data.admins}
            initialWhitelist={data.whitelisted}
            onSave={(admins, whitelisted) => whitelistMutation.mutate({ admins, whitelisted })}
            saving={whitelistMutation.isPending}
          />
        </Card>
      )}

      {can("users.manage") && (
        <Card
          title={t("security.users")}
          actions={
            <Button size="sm" variant="secondary" onClick={() => setShowCreate(true)}>
              {t("security.createUser")}
            </Button>
          }
        >
          {users.isLoading ? (
            <Spinner label={t("common.loading")} />
          ) : (users.data ?? []).length === 0 ? (
            <EmptyState title={t("states.emptyUsers")} />
          ) : (
            <div className="overflow-x-auto">
              <table className="table-base">
                <thead>
                  <tr>
                    <th className="table-th">{t("security.account")}</th>
                    <th className="table-th">{t("security.kind")}</th>
                    <th className="table-th">{t("security.role")}</th>
                    <th className="table-th">{t("common.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(users.data ?? []).map((user) => (
                    <tr key={user.account_id}>
                      <td className="table-td">
                        <span className="font-medium">{user.display_name ?? user.external_id}</span>
                        {user.web_username && (
                          <span className="block text-xs text-slate-400" dir="ltr">
                            {user.web_username}
                          </span>
                        )}
                      </td>
                      <td className="table-td"><Badge tone="neutral">{user.kind}</Badge></td>
                      <td className="table-td">
                        <select
                          className="input py-1 text-xs"
                          value={user.role}
                          disabled={!canManage}
                          onChange={(event) =>
                            roleMutation.mutate({ id: user.account_id, role: event.target.value })
                          }
                        >
                          {["owner", "admin", "operator", "read_only"].map((role) => (
                            <option key={role} value={role}>
                              {t(`roles.${role}`)}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="table-td">
                        <div className="flex flex-wrap gap-1">
                          {user.has_web_login && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => disableMutation.mutate({
                                id: user.account_id, disabled: !user.disabled,
                              })}
                            >
                              {user.disabled ? t("security.enable") : t("security.disable")}
                            </Button>
                          )}
                          {user.role !== "owner" && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => void onDeleteUser(
                                user.display_name ?? user.external_id, user.account_id)}
                            >
                              {t("common.delete")}
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {can("security.read") && (
        <Card title={t("security.baleLink")}>
          <div className="flex flex-col gap-2">
            <Button
              size="sm"
              variant="secondary"
              className="w-fit"
              loading={linkCodeMutation.isPending}
              onClick={() => linkCodeMutation.mutate()}
            >
              <KeyRound className="h-3.5 w-3.5" aria-hidden />
              {t("security.generateLinkCode")}
            </Button>
            {linkCode && (
              <p className="rounded-lg bg-accent-soft px-3 py-2 text-sm">
                <span className="font-mono text-lg font-bold tracking-widest" dir="ltr">
                  {linkCode}
                </span>
                <span className="mt-1 block text-xs text-slate-400">
                  {t("security.linkCodeHint")}
                </span>
              </p>
            )}
          </div>
        </Card>
      )}

      <Card title={t("security.auditEvents")}>
        {(events.data ?? []).length === 0 ? (
          <EmptyState title={t("states.emptyAudit")} />
        ) : (
          <ul className="flex max-h-80 flex-col gap-1 overflow-y-auto font-mono text-xs" dir="ltr">
            {(events.data ?? []).map((event, index) => (
              <li key={index} className="flex items-center justify-between gap-2 border-b
                border-slate-100 py-1 dark:border-ink-800">
                <span className="truncate">{event.event}</span>
                <span className="shrink-0 text-slate-400">
                  {event.ts ? timeAgo(Date.parse(String(event.ts)) / 1000) : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <CreateUserModal open={showCreate} onClose={() => setShowCreate(false)} onCreated={invalidateUsers} />
    </div>
  );
}

function WhitelistEditor({ initialAdmins, initialWhitelist, onSave, saving }: {
  initialAdmins: number[];
  initialWhitelist: number[];
  onSave: (admins: number[], whitelist: number[]) => void;
  saving: boolean;
}) {
  const { t } = useTranslation();
  const [admins, setAdmins] = useState(initialAdmins.join(", "));
  const [whitelist, setWhitelist] = useState(initialWhitelist.join(", "));

  const parse = (value: string): number[] =>
    value.split(",").map((part) => Number(part.trim())).filter((n) => Number.isInteger(n) && n > 0);

  return (
    <div className="flex flex-col gap-3">
      <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
        {t("security.admins")}
        <input className="input mt-1" dir="ltr" value={admins}
               onChange={(event) => setAdmins(event.target.value)} />
      </label>
      <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
        {t("security.whitelist")}
        <input className="input mt-1" dir="ltr" value={whitelist}
               onChange={(event) => setWhitelist(event.target.value)} />
      </label>
      <p className="text-xs text-slate-400">{t("security.commaSeparated")}</p>
      <Button size="sm" className="w-fit" loading={saving}
              onClick={() => onSave(parse(admins), parse(whitelist))}>
        {t("security.saveWhitelist")}
      </Button>
    </div>
  );
}

function CreateUserModal({ open, onClose, onCreated }: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await securityApi.createUser({ username, password, role });
      onCreated();
      setUsername("");
      setPassword("");
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errors.generic"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={t("security.createUser")}>
      <div className="flex flex-col gap-3">
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("auth.username")}
          <input className="input mt-1" dir="ltr" value={username}
                 onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("auth.password")}
          <input className="input mt-1" type="password" dir="ltr" value={password}
                 onChange={(event) => setPassword(event.target.value)} />
        </label>
        <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
          {t("security.role")}
          <select className="input mt-1" value={role}
                  onChange={(event) => setRole(event.target.value)}>
            <option value="admin">{t("roles.admin")}</option>
            <option value="operator">{t("roles.operator")}</option>
            <option value="read_only">{t("roles.read_only")}</option>
          </select>
        </label>
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>{t("common.cancel")}</Button>
          <Button loading={busy} onClick={() => void submit()}>{t("common.create")}</Button>
        </div>
      </div>
    </Modal>
  );
}
