"use client";

import { Button, Input, Logo } from "@scout/design-system";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { signIn, signUp } from "@/lib/auth-client";

export function AuthForm({ mode }: { mode: "sign-in" | "sign-up" }) {
  const router = useRouter();
  const params = useSearchParams();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setPending(true);
    const res = mode === "sign-up" ? await signUp.email({ email, password, name: name || email.split("@")[0] || "You" }) : await signIn.email({ email, password });
    setPending(false);
    if (res.error) {
      setError(res.error.message ?? "Authentication failed");
      return;
    }
    const next = params.get("next");
    router.replace(next && next.startsWith("/") ? next : "/discover");
    router.refresh();
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4">
      <div className="w-full max-w-[340px]">
        <div className="mb-8">
          <Logo size={20} />
        </div>
        <h1 className="title-gradient text-[26px] leading-[32px] font-bold tracking-[-0.015em]">{mode === "sign-in" ? "Welcome back" : "Create your workspace"}</h1>
        <p className="mt-1 text-body text-fg-3">{mode === "sign-in" ? "Sign in to continue to your lead workspace." : "Fresh, evidence-backed leads on demand."}</p>
        <form onSubmit={submit} className="mt-6 space-y-2.5">
          {mode === "sign-up" && (
            <label className="block">
              <span className="mb-1 block text-meta text-fg-2">Name</span>
              <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" placeholder="Ada Lovelace" className="h-8" />
            </label>
          )}
          <label className="block">
            <span className="mb-1 block text-meta text-fg-2">Work email</span>
            <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" placeholder="you@company.com" className="h-8" />
          </label>
          <label className="block">
            <span className="mb-1 block text-meta text-fg-2">Password</span>
            <Input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "sign-in" ? "current-password" : "new-password"}
              placeholder="At least 8 characters"
              className="h-8"
            />
          </label>
          {error && (
            <p className="rounded-sm bg-danger-soft px-2.5 py-1.5 text-meta text-danger" role="alert">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" size="md" className="mt-2 w-full" disabled={pending}>
            {pending ? "Please wait…" : mode === "sign-in" ? "Sign in" : "Create account"}
          </Button>
        </form>
        <p className="mt-5 text-meta text-fg-3">
          {mode === "sign-in" ? (
            <>
              No account yet?{" "}
              <Link href="/sign-up" className="text-accent-strong hover:underline">
                Create one
              </Link>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <Link href="/sign-in" className="text-accent-strong hover:underline">
                Sign in
              </Link>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
