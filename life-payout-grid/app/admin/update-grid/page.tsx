import { redirect } from "next/navigation";
import { isAdminAuthenticated, portalRedirectUrl } from "@/lib/admin-auth";
import { loadGridData } from "@/lib/grid-data";
import { AdminUpload } from "@/components/admin/admin-upload";

export const dynamic = "force-dynamic";

export default async function UpdateGridPage() {
  if (!(await isAdminAuthenticated())) {
    const url = portalRedirectUrl("admin");
    if (!url) {
      return (
        <div className="flex min-h-screen items-center justify-center px-4 text-center text-sm text-muted-foreground">
          INSURANCE_PORTAL_URL is not configured on this app -- can&apos;t redirect back to the portal.
        </div>
      );
    }
    redirect(url);
  }

  const data = loadGridData();
  return <AdminUpload meta={data.meta} />;
}
