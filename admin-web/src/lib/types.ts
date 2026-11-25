export type AccountStatus =
  | "active"
  | "cooldown"
  | "relogin_required"
  | "invalid"
  | "disabled";

export type Account = {
  id: string;
  user_id: string;
  label: string;
  username: string;
  avatar_url: string;
  edition: string;
  host: string;
  client_id: string;
  plan: string;
  status: AccountStatus | string;
  status_reason: string;
  cooldown_until: number | null;
  concurrency_limit: number;
  last_used_at: number | null;
  last_refreshed_at: number | null;
  last_error: string;
  source: string;
  created_at: number;
  updated_at: number;
  token_expires_at: number | null;
  refresh_expires_at: number | null;
  has_refresh_token: boolean;
  note?: string;
  credits?: number | null;
};

export type ApiKey = {
  id: string;
  name: string;
  note?: string;
  prefix: string;
  scopes: string[];
  allowed_ips: string;
  quota_tokens: number;
  quota_requests: number;
  used_tokens: number;
  used_requests: number;
  created_at: number;
  updated_at: number;
  last_used_at: number | null;
  expires_at: number | null;
  disabled: boolean;
};

export type AdminLog = {
  id: number;
  request_id: string;
  time: number;
  key_id: string;
  ip: string;
  account_id: string;
  account_label: string;
  endpoint: string;
  model: string;
  reasoning_effort: string;
  context_window: number | null;
  first_token_ms: number;
  total_ms: number;
  credits: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  status: number;
  error_code: string;
};

export type AdminModel = {
  id: string;
  name: string;
  series: string;
  channels: string[];
  context_window: number | null;
  max_context: number | null;
  max_output: number | null;
  prompt_max_tokens: number | null;
  reasoning_levels: string[];
  default_reasoning_level: string;
  multimodal: boolean;
  tools_supported: boolean;
  max_mode: boolean;
  account_count: number;
  provider_models: string[];
};

export type AdminSettings = {
  ok: boolean;
  version: string;
  relay: string;
  auth_host: string;
  model_gateway: string;
  web_api_base: string;
  refresh_window: number;
  refresh_interval: number;
  default_concurrency: number;
  allow_local_import: boolean;
  client_id: string;
  instance?: InstanceInfo;
};

export type InstanceInfo = {
  version: string;
  started_at: number;
  uptime_seconds: number;
  mode: string;
  pid: number;
  accounts: number;
  active_accounts: number;
  database: string;
};

export type ConcurrencyAccount = {
  id: string;
  name: string;
  limit: number;
  inflight: number;
  status: string;
};

export type ConcurrencyInfo = {
  default_limit: number;
  accounts: ConcurrencyAccount[];
  updated_at: number;
};

export type DashboardMetrics = {
  range: string;
  generated_at: number;
  accounts: {
    total: number;
    active: number;
    cooldown: number;
    expiring: number;
    relogin: number;
    disabled: number;
    archived: number;
  };
  usage: {
    requests: number;
    errors: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    avg_first_token_ms: number;
    success_rate: number;
  };
  today: {
    requests: number;
    tokens: number;
    errors: number;
  };
  trend: {
    day: string;
    requests: number;
    errors: number;
    total_tokens: number;
  }[];
  credits: {
    available: number | null;
    used_today: number | null;
    changed: number | null;
    supported: boolean;
  };
  upstream: {
    status: string;
    healthy_accounts: number;
    cooldown_accounts: number;
    relogin_accounts: number;
    sticky_sessions: number;
    redis_mode: string;
  };
  version: string;
};

export type CreditEntry = {
  id: number;
  kind: "income" | "expense" | string;
  account_id: string;
  account_name: string;
  amount: number;
  balance: number | null;
  reason: string;
  created_at: number;
};

export type CreditSummary = {
  supported: boolean;
  available: number | null;
  today_used: number | null;
  week_net: number | null;
  month_net: number | null;
  entries: CreditEntry[];
};

export type UpdateInfo = {
  ok: boolean;
  current: string;
  latest: string;
  available: boolean;
  notes?: string;
  release_url?: string;
  published_at?: string;
  error?: string;
};

export type UpdateJob = {
  id: string;
  target: string;
  from: string;
  status: "downloading" | "installing" | "restarting" | "done" | "failed";
  /** 0-100；无法计算时为 null */
  percent: number | null;
  detail?: string;
  error?: string;
  started_at: number;
  updated_at: number;
};

export type UpdateStatus = {
  ok: boolean;
  version: string;
  job: UpdateJob | null;
};

export type PlaygroundResult = {
  content: string;
  reasoning: string;
  tool_calls: unknown[];
  usage: Record<string, unknown> | null;
};
