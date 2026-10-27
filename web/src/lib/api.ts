"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: { loc?: string[]; msg: string }[],
  ) {
    super(message);
  }

  /** Human-readable message including per-field validation details. */
  describe(): string {
    if (!this.details?.length) return this.message;
    return this.details
      .map((d) => `${(d.loc ?? []).filter((p) => p !== "body" && p !== "query").join(".") || "request"}: ${d.msg}`)
      .join("; ");
  }
}

/** Call the public API through the same-origin proxy (session cookie auth). */
export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const res = await fetch(`/api/proxy${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(method !== "GET" ? { "x-probeatlas-csrf": "1" } : {}) },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new ApiError(401, "unauthorized", "session expired");
  }
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data?.error ?? {};
    throw new ApiError(res.status, err.code ?? "error", err.message ?? `request failed (${res.status})`, err.details);
  }
  return data as T;
}

/** Fetch `path` (null = skip) and optionally refresh every `refreshMs`. */
export function useApi<T>(path: string | null, refreshMs?: number) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<ApiError | undefined>();
  const [loading, setLoading] = useState(Boolean(path));
  const current = useRef(path);
  current.current = path;

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const result = await api<T>(path);
      if (current.current === path) {
        setData(result);
        setError(undefined);
      }
    } catch (e) {
      if (current.current === path) setError(e instanceof ApiError ? e : new ApiError(0, "network", String(e)));
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setLoading(Boolean(path));
    load();
    if (!refreshMs || !path) return;
    const id = setInterval(load, refreshMs);
    return () => clearInterval(id);
  }, [path, refreshMs, load]);

  return { data, error, loading, reload: load };
}
