import { loadGridData } from "@/lib/grid-data";
import { Dashboard } from "@/components/dashboard";
import { isAuthenticated } from "@/lib/admin-auth";
import { PortalAccessOnly } from "@/components/portal-access-only";

export const dynamic = "force-dynamic";

export default async function Home() {
  if (!(await isAuthenticated())) {
    return <PortalAccessOnly variant="viewer" />;
  }

  const data = loadGridData();
  return <Dashboard data={data} />;
}
