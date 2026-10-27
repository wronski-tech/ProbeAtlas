"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { cx } from "./ui";

type Ws = { id: string; name: string; role: string | null };

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/monitors", label: "Monitors" },
  { href: "/measurements", label: "Measurements" },
  { href: "/incidents", label: "Incidents" },
  { href: "/agents", label: "Agents" },
  { href: "/settings", label: "Settings" },
];

export function Sidebar({ user, workspaces, current }: { user: { name: string; email: string }; workspaces: Ws[]; current: Ws }) {
  const path = usePathname();
  const router = useRouter();

  async function switchWorkspace(id: string) {
    await fetch("/api/session/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-probeatlas-csrf": "1" },
      body: JSON.stringify({ workspace_id: id }),
    });
    router.push("/");
    router.refresh();
  }

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    router.push("/login");
    router.refresh();
  }

  return (
    <aside className="flex w-full shrink-0 flex-col border-b border-slate-200 bg-white md:h-screen md:w-56 md:border-b-0 md:border-r">
      <div className="px-4 py-4">
        <Link href="/" className="text-base font-semibold tracking-tight text-slate-900">
          ProbeAtlas
        </Link>
        <label className="mt-3 block">
          <span className="sr-only">Workspace</span>
          <select
            value={current.id}
            onChange={(e) => switchWorkspace(e.target.value)}
            className="w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
          >
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-1 md:flex-col md:overflow-visible">
        {NAV.map((n) => {
          const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
          return (
            <Link
              key={n.href}
              href={n.href}
              aria-current={active ? "page" : undefined}
              className={cx("whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium", active ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100")}
            >
              {n.label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-slate-100 px-4 py-3 text-xs">
        <div className="truncate font-medium text-slate-800">{user.name}</div>
        <div className="truncate text-slate-500">{user.email}</div>
        <button onClick={logout} className="mt-2 text-slate-600 hover:text-slate-900 hover:underline">
          Sign out
        </button>
      </div>
    </aside>
  );
}
