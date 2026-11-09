import { sessionContext } from "@/lib/server";
import { SettingsSections } from "./SettingsSections";

/** Workspace settings page: reads the caller's role in the selected workspace on the server. */
export default async function SettingsPage() {
  const ctx = await sessionContext();
  return <SettingsSections isOwner={ctx?.workspace.role === "owner"} />;
}
