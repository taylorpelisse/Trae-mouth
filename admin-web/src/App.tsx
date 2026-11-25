import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "./components/layout/app-shell";
import { apiGet } from "./lib/api";
import { LoginPage } from "./pages/login-page";

const DashboardPage = lazy(() =>
  import("./pages/dashboard-page").then((module) => ({
    default: module.DashboardPage,
  })),
);
const AccountsPage = lazy(() =>
  import("./pages/accounts-page").then((module) => ({
    default: module.AccountsPage,
  })),
);
const ModelsPage = lazy(() =>
  import("./pages/models-page").then((module) => ({
    default: module.ModelsPage,
  })),
);
const KeysPage = lazy(() =>
  import("./pages/keys-page").then((module) => ({ default: module.KeysPage })),
);
const LogsPage = lazy(() =>
  import("./pages/logs-page").then((module) => ({ default: module.LogsPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/settings-page").then((module) => ({
    default: module.SettingsPage,
  })),
);
const PlaygroundPage = lazy(() =>
  import("./pages/playground-page").then((module) => ({
    default: module.PlaygroundPage,
  })),
);

type Me = { ok: boolean; username: string };

function RequireAuth({ children }: { children: React.ReactNode }) {
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
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center text-13 text-muted">
          加载中…
        </div>
      }
    >
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
    </Suspense>
  );
}
