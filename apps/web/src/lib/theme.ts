"use client";

import { useSyncExternalStore } from "react";

import { THEME_KEY } from "./theme-script";

export type ThemePref = "dark" | "light" | "system";

const EVENT = "scout-theme-change";

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "system" ? v : "dark";
  } catch {
    return "dark";
  }
}

function resolve(pref: ThemePref): "dark" | "light" {
  if (pref !== "system") return pref;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function apply(pref: ThemePref) {
  const t = resolve(pref);
  document.documentElement.dataset.theme = t;
  document.documentElement.style.colorScheme = t;
}

function subscribe(cb: () => void) {
  const mq = matchMedia("(prefers-color-scheme: light)");
  const onSystem = () => {
    if (readPref() === "system") apply("system");
    cb();
  };
  mq.addEventListener("change", onSystem);
  window.addEventListener(EVENT, cb);
  window.addEventListener("storage", cb);
  return () => {
    mq.removeEventListener("change", onSystem);
    window.removeEventListener(EVENT, cb);
    window.removeEventListener("storage", cb);
  };
}

/** Theme preference persisted per browser; the resolved theme is applied to <html data-theme>. */
export function useTheme(): [ThemePref, (p: ThemePref) => void, "dark" | "light"] {
  const pref = useSyncExternalStore(subscribe, readPref, () => "dark" as ThemePref);
  const resolved = useSyncExternalStore(
    subscribe,
    () => resolve(readPref()),
    () => "dark" as const,
  );
  const set = (p: ThemePref) => {
    try {
      localStorage.setItem(THEME_KEY, p);
    } catch {
      /* storage blocked: still apply for this session */
    }
    apply(p);
    window.dispatchEvent(new Event(EVENT));
  };
  return [pref, set, resolved];
}
