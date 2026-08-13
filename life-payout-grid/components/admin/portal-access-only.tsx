import { LockIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

export function PortalAccessOnly() {
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="flex size-9 items-center justify-center rounded-full bg-primary/10 text-primary">
            <LockIcon className="size-4" />
          </div>
          <CardTitle className="mt-2">Access via the Insurance Portal</CardTitle>
          <CardDescription>
            There&apos;s no separate login here. Open this page from the Insurance
            Portal sidebar (Administration → Update Payout Rates) while signed in
            there.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Don&apos;t see that link? Ask your portal admin to grant you the
            &quot;Update Payout Rates&quot; permission from User Management.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
