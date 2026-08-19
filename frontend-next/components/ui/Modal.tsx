"use client";

/**
 * Dialog.
 *
 * Does the three things a dialog has to do and that a styled <div> does not:
 * traps Tab inside itself, restores focus to whatever opened it on close, and
 * marks the rest of the page inert to assistive tech via role="dialog" +
 * aria-modal. Escape and backdrop-click both close.
 */
import { useCallback, useEffect, useRef } from "react";
import { IconX } from "./icons";
import { Button } from "./index";

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export default function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "lg",
}: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const panel = useRef<HTMLDivElement>(null);
  const restoreTo = useRef<HTMLElement | null>(null);

  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel.current) return;

      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null
      );
      if (!items.length) return;

      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;

      // Wrap at both ends so focus cannot escape into the page behind.
      if (e.shiftKey && (active === first || !panel.current.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [onClose]
  );

  useEffect(() => {
    if (!open) return;

    restoreTo.current = document.activeElement as HTMLElement | null;
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";   // the page must not scroll behind
    document.addEventListener("keydown", onKeyDown, true);

    // Focus the panel itself rather than the first control: a dialog opening
    // with the close button focused reads as "close me".
    panel.current?.focus();

    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = overflow;
      restoreTo.current?.focus?.();
    };
  }, [open, onKeyDown]);

  if (!open) return null;

  const width = { sm: "max-w-md", md: "max-w-2xl", lg: "max-w-4xl" }[size];

  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-black/40 p-3 backdrop-blur-[2px] sm:p-6"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        className={`rise mx-auto w-full ${width} overflow-hidden rounded-[var(--radius-xl)]
          border border-border bg-surface shadow-[var(--shadow-lg)] outline-none`}
      >
        <header className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-[15px] leading-tight font-semibold tracking-[-0.01em] break-words">
              {title}
            </h2>
            {description && <div className="mt-1 text-[13px] text-muted">{description}</div>}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label="Close dialog"
            className="-mt-1 -mr-1 px-2"
            icon={<IconX size={15} />}
          />
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>

        {footer && (
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-surface2 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
