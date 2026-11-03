import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  FolderArchive, Gauge, Globe, LogOut, Menu, Moon, Server, Shield,
  SquareTerminal, Sun, X,
} from "lucide-react";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/lib/theme";

interface NavItem {
  to: string;
  label: string;
  icon: typeof Gauge;
  permission?: string;
}

function navItems(can: (permission: string) => boolean): NavItem[] {
  return [
    { to: "/", label: "nav.dashboard", icon: Gauge },
    { to: "/servers", label: "nav.servers", icon: Server, permission: "servers.read" },
    { to: "/console", label: "nav.console", icon: SquareTerminal, permission: "commands.execute" },
    { to: "/jobs", label: "nav.jobs", icon: SquareTerminal, permission: "jobs.read" },
    { to: "/backups", label: "nav.backups", icon: FolderArchive, permission: "backups.read" },
    { to: "/security", label: "nav.security", icon: Shield, permission: "security.read" },
    { to: "/settings", label: "nav.settings", icon: Globe },
  ].filter((item) => !item.permission || can(item.permission));
}

function Brand() {
  return (
    <div className="flex items-center gap-2 px-2">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-white">
        <Server className="h-4 w-4" aria-hidden />
      </div>
      <span className="text-sm font-semibold tracking-tight">Linux Admin</span>
    </div>
  );
}

export function AppLayout() {
  const { t } = useTranslation();
  const { account, can, signOut } = useAuth();
  const { dark, toggle } = useTheme();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const items = navItems(can);

  const nav = (
    <nav className="flex flex-col gap-1" aria-label="main">
      {items.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          onClick={() => setDrawerOpen(false)}
          className={({ isActive }) =>
            `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors
             ${isActive
              ? "bg-accent-soft text-accent"
              : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-ink-800"}`}
        >
          <Icon className="h-4 w-4" aria-hidden />
          {t(label)}
        </NavLink>
      ))}
    </nav>
  );

  return (
    <div className="flex min-h-dvh">
      {/* Desktop sidebar */}
      <aside
        className="sticky top-0 hidden h-dvh w-60 shrink-0 flex-col justify-between
          border-e border-slate-200 bg-white p-3 dark:border-ink-700 dark:bg-ink-900 lg:flex"
      >
        <div className="flex flex-col gap-6">
          <Brand />
          {nav}
        </div>
        <UserBox account={account?.username ?? ""} role={account?.role ?? ""} onSignOut={signOut} />
      </aside>

      {/* Mobile drawer */}
      {drawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 lg:hidden"
          onClick={() => setDrawerOpen(false)}
          aria-hidden
        />
      )}
      <div
        className={`fixed inset-y-0 z-50 w-64 border-e border-slate-200 bg-white p-3
          transition-transform dark:border-ink-700 dark:bg-ink-900 lg:hidden
          ${drawerOpen ? "translate-x-0" : "ltr:-translate-x-full rtl:translate-x-full"}`}
        style={{ insetInlineStart: 0 }}
        role="dialog"
        aria-modal="true"
        aria-label={t("nav.dashboard")}
      >
        <div className="flex flex-col gap-6">
          <div className="flex items-center justify-between">
            <Brand />
            <button
              type="button"
              onClick={() => setDrawerOpen(false)}
              aria-label={t("common.close")}
              className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-ink-800"
            >
              <X className="h-5 w-5" aria-hidden />
            </button>
          </div>
          {nav}
        </div>
      </div>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className="sticky top-0 z-30 flex items-center justify-between gap-2 border-b
            border-slate-200 bg-white/90 px-4 py-2.5 backdrop-blur dark:border-ink-700 dark:bg-ink-900/90"
        >
          <button
            type="button"
            className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-ink-800 lg:hidden"
            onClick={() => setDrawerOpen(true)}
            aria-label="menu"
          >
            <Menu className="h-5 w-5" aria-hidden />
          </button>
          <div className="flex-1" />
          <button
            type="button"
            onClick={toggle}
            className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-ink-800"
            aria-label={t("common.theme")}
          >
            {dark ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
          </button>
          {account && (
            <button
              type="button"
              onClick={() => void signOut()}
              className="hidden items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs
                font-medium text-slate-500 hover:bg-slate-100 dark:text-slate-300
                dark:hover:bg-ink-800 sm:inline-flex"
            >
              <LogOut className="h-3.5 w-3.5" aria-hidden />
              {t("common.signOut")}
            </button>
          )}
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-5 pb-safe">
          <Outlet />
        </main>

        {/* Mobile sign-out always reachable */}
        {account && (
          <button
            type="button"
            onClick={() => void signOut()}
            className="mx-4 mb-3 rounded-lg border border-slate-200 py-2 text-xs font-medium
              text-slate-500 dark:border-ink-700 dark:text-slate-300 sm:hidden"
          >
            {t("common.signOut")} ({account.username})
          </button>
        )}
      </div>
    </div>
  );
}

function UserBox({ account, role, onSignOut }: {
  account: string;
  role: string;
  onSignOut: () => Promise<void>;
}) {
  const { t } = useTranslation();
  return (
    <div className="border-t border-slate-200 pt-3 dark:border-ink-700">
      <p className="truncate px-2 text-xs font-medium" title={account}>{account}</p>
      <p className="px-2 text-[10px] uppercase tracking-wide text-slate-400">
        {t(`roles.${role}`, { defaultValue: role })}
      </p>
      <button
        type="button"
        onClick={() => void onSignOut()}
        className="mt-2 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-xs
          text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-ink-800"
      >
        <LogOut className="h-3.5 w-3.5" aria-hidden />
        {t("common.signOut")}
      </button>
      <a
        href="/settings"
        className="mt-1 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-xs
          text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-ink-800"
      >
        {t("nav.settings")}
      </a>
    </div>
  );
}
