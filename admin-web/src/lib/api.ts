export const API_BASE = "/admin/api";
export const APP_BASE = "/admin";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, message: string, code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )tm_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  silent?: boolean;
  redirectOn401?: boolean;
};

export async function apiRequest<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const {
    method = "GET",
    body,
    signal,
    silent = false,
    redirectOn401 = true,
  } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: "same-origin",
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, "网络异常或服务不可达");
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    let code = "";
    if (payload && typeof payload === "object") {
      const data = payload as Record<string, unknown>;
      const nested = data.error;
      if (nested && typeof nested === "object") {
        const error = nested as Record<string, unknown>;
        if (typeof error.message === "string") message = error.message;
        if (typeof error.code === "string") code = error.code;
      } else if (typeof data.detail === "string") {
        message = data.detail;
      } else if (typeof data.message === "string") {
        message = data.message;
      }
    }
    if (response.status === 401 && redirectOn401) {
      const next = window.location.pathname + window.location.search;
      window.location.assign(
        `${APP_BASE}/login?next=${encodeURIComponent(next)}`,
      );
    }
    if (!silent) {
      // Error presentation is handled by callers through the shared toast.
    }
    throw new ApiError(response.status, message, code);
  }
  return payload as T;
}

export function apiGet<T>(path: string, options: Omit<RequestOptions, "method"> = {}) {
  return apiRequest<T>(path, { ...options, method: "GET" });
}

export function apiPost<T>(path: string, body: unknown = {}, options: Omit<RequestOptions, "method" | "body"> = {}) {
  return apiRequest<T>(path, { ...options, method: "POST", body });
}

export function apiPatch<T>(path: string, body: unknown, options: Omit<RequestOptions, "method" | "body"> = {}) {
  return apiRequest<T>(path, { ...options, method: "PATCH", body });
}

export function apiDelete<T>(path: string, options: Omit<RequestOptions, "method"> = {}) {
  return apiRequest<T>(path, { ...options, method: "DELETE" });
}

export function queryString(values: Record<string, string | number | undefined | null>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}
