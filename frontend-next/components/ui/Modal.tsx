"use client";

/**
 * Dialog.
 *
 * Does the three things a dialog has to do and that a styled <div> does not:
 * traps Tab inside itself, restores focus to whatever opened it on close, and
 * marks the rest of the page inert to assistive tech via role="dialog" +
 * aria-modal. Escape and backdrop-click both close.
 *
 * WHY THIS RENDERS THROUGH A PORTAL, AND WHY IT IS NOT OPTIONAL
 *
 * `position: fixed` is resolved against the viewport ONLY while no ancestor
 * establishes a containing block for it — and `backdrop-filter` (any value but
 * `none`) does exactly that, along with `transform`, `filter`, `perspective`
 * and `contain`.
 *
 * `AppShell.PageHeader` is `sticky ... backdrop-blur-md`, and both call sites of
 * ResetDemoButton render into its `actions` slot. So `fixed inset-0` was being
 * resolved against the PAGE HEADER's box: the overlay covered a ~100px band of
 * the title bar instead of the screen, the panel was clipped to it, and the
 * footer buttons — Cancel and Clear history — were cut off entirely. The dialog
 * looked broken and could not be completed, only dismissed.
 *
 * Portalling to <body> puts the overlay outside every such ancestor, so a modal
 * is correct wherever it is mounted rather than only where the surrounding
 * markup happens to permit it. Do not "simplify" this back to an inline render.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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

  // This is a static export, so the tree is prerendered at build time where
  // there is no `document` to portal into. Render nothing until we are on a
  // client.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

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
    if (!open || !mounted) return;

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
  }, [open, mounted, onKeyDown]);

  if (!open || !mounted) return null;

  const width = { sm: "max-w-md", md: "max-w-2xl", lg: "max-w-4xl" }[size];

  return createPortal(
    <div className="fixed inset-0 z-50 overflow-y-auto bg-black/50 backdrop-blur-[2px]">
      {/* min-h-full + items-center centres a short dialog in the viewport and
          lets a tall one scroll from the top instead of overflowing upwards
          out of reach. The backdrop-close listener lives here rather than on
          the parent because this is the element the click actually lands on. */}
      <div
        className="flex min-h-full items-center justify-center p-3 sm:p-6"
        onMouseDown={(e) => e.target === e.currentTarget && onClose()}
      >
        <div
          ref={panel}
          role="dialog"
          aria-modal="true"
          aria-label={typeof title === "string" ? title : undefined}
          tabIndex={-1}
          className={`rise w-full ${width} overflow-hidden rounded-[var(--radius-lg)]
            border border-line bg-surface shadow-[var(--shadow-lg)] outline-none`}
        >
          <header className="flex items-start justify-between gap-4 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <h2 className="t-section break-words">{title}</h2>
              {description && <div className="t-meta mt-0.5">{description}</div>}
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={onClose}
              aria-label="Close dialog"
              className="-mt-0.5 -mr-1"
              icon={<IconX size={14} />}
            />
          </header>

          <div className="max-h-[72vh] overflow-y-auto px-4 py-4">{children}</div>

          {footer && (
            <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-sunken px-4 py-3">
              {footer}
            </footer>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
}
