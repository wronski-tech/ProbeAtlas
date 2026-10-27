import { redirect } from "next/navigation";
import { Sidebar } from "@/components/Sidebar";
import { sessionContext } from "@/lib/server";

export const dynamic = "force-dynamic";

/** Authenticated shell: redirects to /login without a session and renders the sidebar. */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const ctx = await sessionContext();
  if (!ctx) redirect("/login");
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Sidebar user={ctx.me.user} workspaces={ctx.me.workspaces} current={ctx.workspace} />
      <main className="min-w-0 flex-1 px-4 py-6 md:h-screen md:overflow-y-auto md:px-8">
        <div className="mx-auto max-w-7xl">{children}</div>
      </main>
    </div>
  );
}
