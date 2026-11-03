import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppLayout } from "@/components/layout/AppLayout";
import { Spinner } from "@/components/ui/Primitives";
import { AuthProvider, useAuth } from "@/hooks/useAuth";
import { ConfirmProvider } from "@/hooks/useConfirm";
import { LoginPage } from "@/pages/LoginPage";
import { SetupPage } from "@/pages/SetupPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { ServersPage } from "@/pages/ServersPage";
import { ServerDetailPage } from "@/pages/ServerDetailPage";
import { ConsolePage } from "@/pages/ConsolePage";
import { JobsPage } from "@/pages/JobsPage";
import { BackupsPage } from "@/pages/BackupsPage";
import { SecurityPage } from "@/pages/SecurityPage";
import { SettingsPage } from "@/pages/SettingsPage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { authenticated, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Spinner />;
  if (!authenticated) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <AuthProvider>
      <ConfirmProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route
            element={
              <RequireAuth>
                <AppLayout />
              </RequireAuth>
            }
          >
            <Route index element={<DashboardPage />} />
            <Route path="servers" element={<ServersPage />} />
            <Route path="servers/:serverId" element={<ServerDetailPage />} />
            <Route path="console" element={<ConsolePage />} />
            <Route path="jobs" element={<JobsPage />} />
            <Route path="backups" element={<BackupsPage />} />
            <Route path="security" element={<SecurityPage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ConfirmProvider>
    </AuthProvider>
  );
}
