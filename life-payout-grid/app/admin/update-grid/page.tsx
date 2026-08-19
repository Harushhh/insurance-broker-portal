import { isAdminAuthenticated } from "@/lib/admin-auth";
import { loadGridData } from "@/lib/grid-data";
import { PortalAccessOnly } from "@/components/portal-access-only";
import { AdminUpload } from "@/components/admin/admin-upload";

export const dynamic = "force-dynamic";

export default async function UpdateGridPage() {
  const authed = await isAdminAuthenticated();
  if (!authed) {
    return <PortalAccessOnly variant="admin" />;
  }

  const data = loadGridData();
  return <AdminUpload meta={data.meta} />;
}
