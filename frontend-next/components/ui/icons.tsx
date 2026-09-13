/**
 * Icon set — inline SVG, no dependency.
 *
 * One geometry for all of them: 16px grid, 1.5 stroke, round caps, currentColor.
 * A mixed-weight icon set is one of the fastest ways for an interface to look
 * assembled rather than designed.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconOverview = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2" y="2" width="5" height="5" rx="1.2" />
    <rect x="9" y="2" width="5" height="5" rx="1.2" />
    <rect x="2" y="9" width="5" height="5" rx="1.2" />
    <rect x="9" y="9" width="5" height="5" rx="1.2" />
  </Svg>
);

export const IconUpload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 10.5V2.5" />
    <path d="M5 5.5 8 2.5l3 3" />
    <path d="M2.5 10v2A1.5 1.5 0 0 0 4 13.5h8a1.5 1.5 0 0 0 1.5-1.5v-2" />
  </Svg>
);

export const IconInvoice = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3.5 1.5h6l3 3v10h-9z" />
    <path d="M9.5 1.5v3h3" />
    <path d="M5.5 8h5M5.5 10.5h3" />
  </Svg>
);

export const IconLedger = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
    <path d="M2 6h12M6 6v7.5" />
  </Svg>
);

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 8.5 6.5 12 13 4.5" />
  </Svg>
);

export const IconAlert = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 5v3.5M8 11h.01" />
  </Svg>
);

export const IconX = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Svg>
);

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="7" cy="7" r="4.5" />
    <path d="M10.5 10.5 14 14" />
  </Svg>
);

export const IconChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 6l4 4 4-4" />
  </Svg>
);

export const IconChevronLeft = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 3 5 8l5 5" />
  </Svg>
);

export const IconChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 3l5 5-5 5" />
  </Svg>
);

export const IconArrowUp = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 13V3M4 7l4-4 4 4" />
  </Svg>
);

export const IconMenu = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
  </Svg>
);

export const IconSignOut = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 14H3.5A1.5 1.5 0 0 1 2 12.5v-9A1.5 1.5 0 0 1 3.5 2H6" />
    <path d="M10.5 11 14 8l-3.5-3M14 8H6" />
  </Svg>
);

export const IconRefresh = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13.5 7a5.5 5.5 0 1 0-.7 3.4" />
    <path d="M13.5 3.5V7H10" />
  </Svg>
);

export const IconFile = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 1.5h5l3 3v10H4z" />
    <path d="M9 1.5v3h3" />
  </Svg>
);

export const IconClock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 4.5V8l2.5 1.5" />
  </Svg>
);

export const IconUser = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="5.5" r="2.5" />
    <path d="M3 13.5a5 5 0 0 1 10 0" />
  </Svg>
);

export const IconLink = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6.5 9.5a2.5 2.5 0 0 0 3.5 0l2-2a2.5 2.5 0 0 0-3.5-3.5l-.8.8" />
    <path d="M9.5 6.5a2.5 2.5 0 0 0-3.5 0l-2 2A2.5 2.5 0 0 0 7.5 12l.8-.8" />
  </Svg>
);

export const IconShield = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 1.8 13 3.5v4c0 3.2-2.1 5.7-5 6.7-2.9-1-5-3.5-5-6.7v-4z" />
    <path d="M6 8l1.5 1.5L10.5 6.5" />
  </Svg>
);

export const IconSun = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="3" />
    <path d="M8 1.5v1.5M8 13v1.5M2.6 2.6l1.1 1.1M12.3 12.3l1.1 1.1M1.5 8h1.5M13 8h1.5M2.6 13.4l1.1-1.1M12.3 3.7l1.1-1.1" />
  </Svg>
);

export const IconMoon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13.5 9.8A5.8 5.8 0 1 1 6.2 2.5a4.6 4.6 0 0 0 7.3 7.3z" />
  </Svg>
);

export const IconEmpty = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2" y="3.5" width="12" height="9" rx="1.5" strokeDasharray="2.5 2" />
    <path d="M5.5 8h5" />
  </Svg>
);

/** Analytics: three bars of different heights. Distinct at 15px from
 *  IconOverview, which is the one it sits next to in the rail. */
export const IconAnalytics = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 13.5h12" />
    <path d="M4.5 11V8" />
    <path d="M8 11V4.5" />
    <path d="M11.5 11V6.5" />
  </Svg>
);

/** The assistant (Phase K2). A speech bubble, because that is what every
 *  interface in the world uses for one and inventing a novel glyph here would
 *  only make the row harder to find. */
export const IconChat = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13.5 9.5a1.5 1.5 0 0 1-1.5 1.5H6l-3 2.5V4a1.5 1.5 0 0 1 1.5-1.5h7.5A1.5 1.5 0 0 1 13.5 4z" />
  </Svg>
);

/** The client portal (Phase J). A building, because the row it labels is the
 *  supplier's own company rather than a document or a report — and because it
 *  has to be distinguishable at 15px from IconInvoice and IconLedger, which
 *  are both page-shaped. */
export const IconBuilding = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 14V3.5a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1V14" />
    <path d="M9.5 6.5h3a1 1 0 0 1 1 1V14" />
    <path d="M1 14h14" />
    <path d="M5 5.5h2M5 8h2M5 10.5h2" />
  </Svg>
);

/** Email integration settings (Phase G2). A cog, because the row it labels is
 *  configuration rather than a place invoices live — and specifically NOT an
 *  envelope, which would read as "somewhere to look at email" next to the
 *  Invoices and Review queue rows rather than as somewhere to connect a
 *  mailbox. */
export const IconSettings = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="2.25" />
    <path d="M8 1.5v1.75M8 12.75v1.75M14.5 8h-1.75M3.25 8H1.5M12.6 3.4l-1.24 1.24M4.64 11.36 3.4 12.6M12.6 12.6l-1.24-1.24M4.64 4.64 3.4 3.4" />
  </Svg>
);

/** Language (Phase L). A globe with a meridian and a parallel — the one mark
 *  people already read as "change the language" without a word beside it,
 *  which matters on a control whose whole job is to be findable by someone who
 *  cannot read the current one. */
export const IconGlobe = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M1.75 8h12.5" />
    <path d="M8 1.75c1.7 1.85 2.6 3.95 2.6 6.25S9.7 12.4 8 14.25C6.3 12.4 5.4 10.3 5.4 8s.9-4.4 2.6-6.25Z" />
  </Svg>
);

/** An envelope, for the held-message review queue -- distinct from
 *  IconSettings (the mailbox CONNECTION) because this is about individual
 *  MESSAGES that arrived through it. */
export const IconMail = (p: IconProps) => (
  <Svg {...p}>
    <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.5" />
    <path d="M2.25 4.25 8 8.75l5.75-4.5" />
  </Svg>
);

/** A downward arrow into a tray -- the audit-report export buttons. Distinct
 *  from IconLink (open in a new tab): this one is specifically "save a file
 *  to this device". */
export const IconDownload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 1.75v7.5M8 9.25 5 6.25M8 9.25l3-3" />
    <path d="M2.25 11v1.75a1 1 0 0 0 1 1h9.5a1 1 0 0 0 1-1V11" />
  </Svg>
);
