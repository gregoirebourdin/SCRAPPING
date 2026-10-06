"use client";

import { Button, Input } from "@scout/design-system";
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
        <div className="mb-6 flex items-center gap-2.5">
          <Logo />
          <span className="text-heading text-fg">Scout</span>
        </div>
        <h1 className="text-display font-semibold tracking-[-0.01em] text-fg">{mode === "sign-in" ? "Welcome back" : "Create your workspace"}</h1>
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

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="8" fill="var(--surface-3)" />
      <path
        d="M10 20.5c1.4 1.3 3.2 2 5.6 2 3 0 5-1.5 5-3.8 0-2.2-1.6-3.1-4.6-3.8l-1.4-.3c-1.7-.4-2.4-.9-2.4-1.8 0-1 1-1.7 2.6-1.7 1.5 0 2.7.5 3.8 1.5l1.6-2c-1.4-1.3-3.2-2-5.3-2-2.9 0-4.9 1.6-4.9 3.9 0 2.1 1.4 3.1 4.3 3.8l1.4.3c1.9.4 2.6.9 2.6 1.9 0 1.1-1 1.8-2.7 1.8-1.8 0-3.2-.6-4.4-1.8z"
        fill="var(--accent)"
      />
    </svg>
  );
}
