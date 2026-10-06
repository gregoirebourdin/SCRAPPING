import "server-only";

import { betterAuth } from "better-auth";
import { nextCookies } from "better-auth/next-js";
import { Pool } from "pg";

/** Better Auth (self-hosted, no paid service) on the same Postgres as the API.
 * Tables are created by the API's Alembic migration; field names are mapped to snake_case. */
const globalForPool = globalThis as unknown as { scoutAuthPool?: Pool };

const pool =
  globalForPool.scoutAuthPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 5,
    ssl: process.env.DATABASE_URL?.includes("sslmode=require") ? { rejectUnauthorized: true } : undefined,
  });
if (process.env.NODE_ENV !== "production") globalForPool.scoutAuthPool = pool;

/* On Vercel, fall back to the system URLs so production, branch and preview deployments all work. */
const https = (host?: string) => (host ? `https://${host}` : undefined);
const vercelOrigins = [process.env.VERCEL_PROJECT_PRODUCTION_URL, process.env.VERCEL_BRANCH_URL, process.env.VERCEL_URL].map(https).filter((u): u is string => Boolean(u));
const baseURL =
  process.env.BETTER_AUTH_URL ?? (process.env.VERCEL_ENV === "production" ? https(process.env.VERCEL_PROJECT_PRODUCTION_URL) : https(process.env.VERCEL_BRANCH_URL ?? process.env.VERCEL_URL));

const googleId = process.env.GOOGLE_CLIENT_ID;
const googleSecret = process.env.GOOGLE_CLIENT_SECRET;

export const auth = betterAuth({
  database: pool,
  secret: process.env.BETTER_AUTH_SECRET,
  baseURL,
  trustedOrigins: vercelOrigins,
  emailAndPassword: { enabled: true, minPasswordLength: 8, autoSignIn: true },
  socialProviders: googleId && googleSecret ? { google: { clientId: googleId, clientSecret: googleSecret } } : undefined,
  session: {
    modelName: "auth_sessions",
    expiresIn: 60 * 60 * 24 * 30,
    updateAge: 60 * 60 * 24,
    cookieCache: { enabled: true, maxAge: 300 },
    fields: {
      expiresAt: "expires_at",
      createdAt: "created_at",
      updatedAt: "updated_at",
      ipAddress: "ip_address",
      userAgent: "user_agent",
      userId: "user_id",
    },
  },
  user: {
    modelName: "users",
    fields: { emailVerified: "email_verified", createdAt: "created_at", updatedAt: "updated_at" },
  },
  account: {
    modelName: "auth_accounts",
    fields: {
      accountId: "account_id",
      providerId: "provider_id",
      userId: "user_id",
      accessToken: "access_token",
      refreshToken: "refresh_token",
      idToken: "id_token",
      accessTokenExpiresAt: "access_token_expires_at",
      refreshTokenExpiresAt: "refresh_token_expires_at",
      createdAt: "created_at",
      updatedAt: "updated_at",
    },
  },
  verification: {
    modelName: "auth_verifications",
    fields: { expiresAt: "expires_at", createdAt: "created_at", updatedAt: "updated_at" },
  },
  advanced: { cookiePrefix: "scout" },
  plugins: [nextCookies()],
});

export type Session = typeof auth.$Infer.Session;
