import { LockIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

const COPY = {
  viewer: {
    description: (
      <>
        There&apos;s no separate login here. Open this page from the Insurance
        Portal sidebar (Life Payout Grid) while signed in there.
      </>
    ),
    footer: <>Don&apos;t see that link? You need to be logged into the Insurance Portal.</>,
  },
  admin: {
    description: (
      <>
        There&apos;s no separate login here. Open this page from the Insurance
        Portal sidebar (Administration → Update Payout Rates) while signed in
        there.
      </>
    ),
    footer: (
      <>
        Don&apos;t see that link? Ask your portal admin to grant you the
        &quot;Update Payout Rates&quot; permission from User Management.
      </>
    ),
  },
} as const;

export function PortalAccessOnly({ variant = "viewer" }: { variant?: "viewer" | "admin" }) {
  const copy = COPY[variant];
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="flex size-9 items-center justify-center rounded-full bg-primary/10 text-primary">
            <LockIcon className="size-4" />
          </div>
          <CardTitle className="mt-2">Access via the Insurance Portal</CardTitle>
          <CardDescription>{copy.description}</CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{copy.footer}</p>
        </CardContent>
      </Card>
    </div>
  );
}
