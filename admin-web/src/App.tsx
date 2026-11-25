import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "./components/layout/app-shell";
import { apiGet } from "./lib/api";
import { LoginPage } from "./pages/login-page";
import { DashboardPage } from "./pages/dashboard-page";
import { AccountsPage } from "./pages/accounts-page";
import { ModelsPage } from "./pages/models-page";
import { KeysPage } from "./pages/keys-page";
import { LogsPage } from "./pages/logs-page";
import { SettingsPage } from "./pages/settings-page";
import { PlaygroundPage } from "./pages/playground-page";

type Me = { ok: boolean; username: string };

function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  const query = useQuery({
    queryKey: ["admin", "me"],
    queryFn: () => apiGet<Me>("/me", { redirectOn401: false, silent: true }),
    retry: false,
    staleTime: 30_000,
  });

  if (query.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-13 text-muted">
        正在验证登录状态…
      </div>
    );
  }
  if (query.isError) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search }}
      />
    );
  }
  return <>{children}</>;
}

function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="text-28 font-semibold">404</p>
      <p className="text-13 text-muted">页面不存在或已被移动。</p>
      <a className="text-13 text-accent underline-offset-4 hover:underline" href="/admin/dashboard">
        返回仪表盘
      </a>
    </div>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/keys" element={<KeysPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/playground" element={<PlaygroundPage />} />
      </Route>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
