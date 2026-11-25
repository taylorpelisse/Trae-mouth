import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { LoaderCircle, Lock } from "lucide-react";
import { apiGet, apiPost, ApiError } from "../lib/api";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";

type Status = { initialized: boolean };

export function LoginPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [initialized, setInitialized] = useState<boolean | null>(null);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    apiGet<Status>("/status", { silent: true, redirectOn401: false })
      .then((data) => {
        if (!cancelled) setInitialized(Boolean(data.initialized));
      })
      .catch(() => {
        if (!cancelled) setInitialized(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!password) {
      setError("请输入密码");
      return;
    }
    if (!initialized) {
      if (password.length < 8) {
        setError("密码至少 8 位");
        return;
      }
      if (password !== confirm) {
        setError("两次输入的密码不一致");
        return;
      }
    }
    setPending(true);
    setError("");
    try {
      if (!initialized) {
        await apiPost("/setup", {
          username: username.trim() || "admin",
          password,
        });
      } else {
        await apiPost("/login", {
          username: username.trim() || "admin",
          password,
        });
      }
      const next = params.get("next") || "/dashboard";
      navigate(next.startsWith("/admin") ? next.replace(/^\/admin/, "") : next, {
        replace: true,
      });
    } catch (caught) {
      if (caught instanceof ApiError) {
        setError(
          caught.status === 401
            ? "用户名或密码错误"
            : caught.message || "登录失败，请稍后重试",
        );
      } else {
        setError("网络异常或服务不可达");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="mb-1 flex h-10 w-10 items-center justify-center rounded-chip bg-accent/10 text-accent">
            <Lock className="h-5 w-5" />
          </div>
          <CardTitle className="text-xl">Trae Mouth 管理控制台</CardTitle>
          <CardDescription>
            {initialized === false ? "创建管理员账号以继续" : "请输入管理员账号以继续"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="admin-username">管理员账号</Label>
              <Input
                id="admin-username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="admin"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="admin-password">管理员密码</Label>
              <Input
                id="admin-password"
                type="password"
                autoComplete={initialized === false ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="请输入密码"
              />
            </div>
            {initialized === false ? (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="admin-confirm">确认密码</Label>
                <Input
                  id="admin-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder="再次输入密码"
                />
              </div>
            ) : null}
            {error ? (
              <p className="text-xs text-danger" role="alert">
                {error}
              </p>
            ) : null}
            <Button type="submit" disabled={pending || initialized === null} className="w-full">
              {pending ? (
                <>
                  <LoaderCircle className="h-4 w-4 animate-spin" />
                  {initialized === false ? "创建中…" : "验证中…"}
                </>
              ) : initialized === false ? (
                "创建并进入"
              ) : (
                "验证并进入"
              )}
            </Button>
            <p className="text-xs text-muted">
              登录状态由 HttpOnly Cookie 保存，关闭浏览器后按服务端会话策略失效。
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
