"use client";

/**
 * Toasts.
 *
 * For outcomes the user should notice but not have to acknowledge — a review
 * recorded, a refresh failing. Anything that needs a decision belongs in a
 * dialog; anything permanent belongs inline on the page. Toasts here are
 * announced politely to assistive tech and auto-dismiss.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { IconAlert, IconCheck, IconX } from "./icons";
import type { Tone } from "./index";

interface Toast {
  id: number;
  tone: Tone;
  title: string;
  detail?: string;
}

const Ctx = createContext<{ push: (t: Omit<Toast, "id">) => void } | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);

  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-2), { ...t, id }]);
  }, []);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const value = useMemo(() => ({ push }), [push]);

  return (
    <Ctx.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2"
        role="region"
        aria-label="Notifications"
      >
        {items.map((t) => (
          <ToastRow key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </Ctx.Provider>
  );
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const accent = {
    neutral: "var(--line-strong)",
    ok: "var(--ok-vivid)",
    warn: "var(--warn-vivid)",
    bad: "var(--bad-vivid)",
    accent: "var(--accent)",
  }[toast.tone];

  const Icon = toast.tone === "bad" || toast.tone === "warn" ? IconAlert : IconCheck;

  return (
    <div
      role="status"
      aria-live="polite"
      className="slide-in pointer-events-auto flex items-start gap-2.5 rounded-[var(--radius-md)]
        border border-line bg-raised p-3 shadow-[var(--shadow-lg)]"
      style={{ borderLeft: `2px solid ${accent}` }}
    >
      <span className="mt-px shrink-0" style={{ color: accent }}>
        <Icon size={14} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[13.5px] font-medium">{toast.title}</p>
        {toast.detail && <p className="t-meta mt-0.5">{toast.detail}</p>}
      </div>
      <button
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 rounded-[var(--radius-xs)] p-0.5 text-faint transition-colors hover:text-fg"
      >
        <IconX size={13} />
      </button>
    </div>
  );
}

/** Safe outside a provider: returns a no-op so a component can be rendered in
 *  isolation without blowing up. */
export function useToast() {
  return useContext(Ctx) ?? { push: () => {} };
}
