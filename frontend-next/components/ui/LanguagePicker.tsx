"use client";

/**
 * The language control (Phase L).
 *
 * A native `<select>` rather than a custom menu, deliberately: it is
 * keyboard-navigable, screen-reader-labelled and searchable by typing on
 * every platform without this file having to reimplement any of that, and a
 * language picker is exactly the control someone reaches for when they cannot
 * read the interface well enough to work out a bespoke one.
 *
 * The options come from the SERVER when the caller is signed in
 * (`/api/auth/me` and `/api/portal/me` both carry `languages`), so a
 * deployment that ships without a catalogue never offers that language. On the
 * sign-in screen there is no token yet, so it falls back to the list this
 * bundle carries -- the one case where the two can disagree, and the worst it
 * can do is offer a language the server then answers in English.
 */
import { LOCALE_NAMES, LOCALES, useI18n } from "@/lib/i18n";
import { IconGlobe } from "./icons";
import type { LanguageOption } from "@/lib/types";

const FALLBACK: LanguageOption[] = LOCALES.map((tag) => ({
  tag,
  name: LOCALE_NAMES[tag] ?? tag,
  rtl: false,
  default: tag === "en",
}));

export default function LanguagePicker({
  options,
  className = "",
  compact = false,
}: {
  /** What the server said it can answer in. Omitted before sign-in. */
  options?: LanguageOption[];
  className?: string;
  /** Icon-and-code only, for a sidebar footer that is already crowded. */
  compact?: boolean;
}) {
  const { locale, setLocale, t } = useI18n();
  const list = options && options.length ? options : FALLBACK;

  return (
    <label className={`relative inline-flex shrink-0 items-center gap-1.5 ${className}`}>
      <span className="sr-only">{t("app.language.choose")}</span>
      <IconGlobe size={14} aria-hidden className="pointer-events-none text-faint" />
      <select
        value={locale}
        onChange={(e) => setLocale(e.target.value)}
        aria-label={t("app.language.choose")}
        className="cursor-pointer appearance-none bg-transparent py-1 pr-1 text-[13px] text-secondary outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
      >
        {list.map((o) => (
          <option key={o.tag} value={o.tag}>
            {compact ? o.tag.toUpperCase() : o.name}
          </option>
        ))}
      </select>
    </label>
  );
}
