"use client";

import { useSyncExternalStore } from "react";

/** Subscribe to a CSS media query. Server render assumes desktop (the product is desktop-first). */
export function useMediaQuery(query: string, serverValue = true): boolean {
  return useSyncExternalStore(
    (cb) => {
      const mq = matchMedia(query);
      mq.addEventListener("change", cb);
      return () => mq.removeEventListener("change", cb);
    },
    () => matchMedia(query).matches,
    () => serverValue,
  );
}

export const DESKTOP = "(min-width: 1024px)";
export const TABLET_UP = "(min-width: 768px)";
