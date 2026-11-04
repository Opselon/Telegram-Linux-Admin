import { useEffect, useState } from "react";

/**
 * Returns true while the document is visible. Use with React Query's
 * `refetchInterval` to poll only when the page is actually shown
 * (React Query already supports `refetchIntervalInBackground: false`).
 */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(() =>
    typeof document === "undefined" ? true : document.visibilityState === "visible",
  );

  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  return visible;
}

/** Poll interval that stops when the tab is hidden. */
export function usePollInterval(intervalMs: number): number | false {
  const visible = usePageVisible();
  return visible ? intervalMs : false;
}
