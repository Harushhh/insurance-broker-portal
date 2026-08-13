import { isAuthenticated } from "@/lib/admin-auth";
import { loadGridData } from "@/lib/grid-data";
import { PortalAccessOnly } from "@/components/admin/portal-access-only";
import { AdminUpload } from "@/components/admin/admin-upload";

export const dynamic = "force-dynamic";

export default async function UpdateGridPage() {
  const authed = await isAuthenticated();
  if (!authed) {
    return <PortalAccessOnly />;
  }

  const data = loadGridData();
  return <AdminUpload meta={data.meta} />;
}
