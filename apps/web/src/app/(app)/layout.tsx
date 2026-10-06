import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { AppShell } from "@/components/shell/app-shell";
import { Providers } from "@/components/shell/providers";
import { auth } from "@/lib/auth";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) redirect("/sign-in");
  return (
    <Providers>
      <AppShell user={{ name: session.user.name, email: session.user.email }}>{children}</AppShell>
    </Providers>
  );
}
