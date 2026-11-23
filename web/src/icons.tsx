import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function IconBase({ children, ...rest }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.85"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function MatrixIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
    </IconBase>
  );
}

export function PhoneIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="6" y="2.5" width="12" height="19" rx="2.75" />
      <line x1="10" y1="5.5" x2="14" y2="5.5" />
      <line x1="11" y1="18.5" x2="13" y2="18.5" />
    </IconBase>
  );
}

export function TabletIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="4" y="2.5" width="16" height="19" rx="2.5" />
      <line x1="11" y1="18.5" x2="13" y2="18.5" />
    </IconBase>
  );
}

export function SummaryIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M5 4.5h14a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V6A1.5 1.5 0 0 1 5 4.5Z" />
      <path d="M7.5 9.5h9M7.5 13h6M7.5 16.5h4" />
    </IconBase>
  );
}

export function CompareIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3" y="4.5" width="7.5" height="15" rx="1.5" />
      <rect x="13.5" y="4.5" width="7.5" height="15" rx="1.5" />
      <path d="M10.5 9.5h3M10.5 14.5h3" />
    </IconBase>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M20 11a8 8 0 0 0-14.3-4.9L4 8" />
      <path d="M4 4v4h4" />
      <path d="M4 13a8 8 0 0 0 14.3 4.9L20 16" />
      <path d="M20 20v-4h-4" />
    </IconBase>
  );
}

export function FileMarkdownIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
      <path d="M14 3v5h5" />
      <path d="M8.5 15.5v-4l2 2 2-2v4" />
      <path d="M15.5 11.5v4" />
    </IconBase>
  );
}

export function StreamIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M13 2 4.5 13.5H12L11 22l8.5-11.5H12L13 2Z" />
    </IconBase>
  );
}

export function PollIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M8 5v14M16 5v14M3 9.5h5M16 9.5h5M3 14.5h5M16 14.5h5" />
    </IconBase>
  );
}

export function PauseIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="6" y="4.5" width="4" height="15" rx="1" />
      <rect x="14" y="4.5" width="4" height="15" rx="1" />
    </IconBase>
  );
}

export function PlayIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <polygon points="7 4.5 19.5 12 7 19.5 7 4.5" fill="currentColor" stroke="none" />
    </IconBase>
  );
}

export function HandIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M18 11V6a1.5 1.5 0 0 0-3 0v4" />
      <path d="M15 10V4.5a1.5 1.5 0 0 0-3 0V10" />
      <path d="M12 10V5.5a1.5 1.5 0 0 0-3 0V11" />
      <path d="M9 11V8a1.5 1.5 0 0 0-3 0v6a6 6 0 0 0 12 0v-3Z" />
    </IconBase>
  );
}

export function HomeIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M3.5 10.75 12 3.75l8.5 7" />
      <path d="M6 9.25V19a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 18 19V9.25" />
    </IconBase>
  );
}

export function LockIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="5" y="10.5" width="14" height="10" rx="2.5" />
      <path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3" />
    </IconBase>
  );
}

export function MoonSunIcon({ isDark, ...props }: IconProps & { isDark?: boolean }) {
  if (isDark) {
    return (
      <IconBase {...props}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2.5v2M12 19.5v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2.5 12h2M19.5 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
      </IconBase>
    );
  }
  return (
    <IconBase {...props}>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a7.5 7.5 0 1 0 10.5 10.5Z" />
    </IconBase>
  );
}

export function KeyboardIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="2.5" y="5.5" width="19" height="13" rx="2.25" />
      <path d="M6.5 9.5h.01M10 9.5h.01M13.5 9.5h.01M17 9.5h.01M6.5 13h.01M10 13h.01M13.5 13h.01M17 13h.01M8.5 16h7" />
    </IconBase>
  );
}

export function CameraIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M4 8.5A2 2 0 0 1 6 6.5h2l1.5-2h5l1.5 2h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-9Z" />
      <circle cx="12" cy="12.5" r="3.2" />
    </IconBase>
  );
}

export function StopCircleIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="9" />
      <rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor" stroke="none" />
    </IconBase>
  );
}

export function A11yIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="7.5" r="1.1" fill="currentColor" stroke="none" />
      <path d="M8 10.5h8M12 10.5v3.5M10 17.5l2-3.5 2 3.5" />
    </IconBase>
  );
}

export function SparkleInspectIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M11 3.5 12.6 8l4.4 1.6-4.4 1.6-1.6 4.5-1.6-4.5L5 9.6 9.4 8 11 3.5Z" />
      <path d="M18.5 14.5 19.3 16.7l2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z" />
    </IconBase>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M21 3 10 14" />
      <path d="M21 3 14.5 21 10 14 3 9.5 21 3Z" />
    </IconBase>
  );
}

export function ReturnKeyIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M19 6v5a3 3 0 0 1-3 3H5.5" />
      <path d="m9 10.5-3.5 3.5L9 17.5" />
    </IconBase>
  );
}

export function BackspaceIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M20 5.5H9.5L3.5 12l6 6.5H20a1.5 1.5 0 0 0 1.5-1.5V7A1.5 1.5 0 0 0 20 5.5Z" />
      <path d="m12 9.5 5 5M17 9.5l-5 5" />
    </IconBase>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M6 6 18 18M18 6 6 18" />
    </IconBase>
  );
}

export function AlertTriangleIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M10.3 4.3 2.5 17.8A1.8 1.8 0 0 0 4.1 20.5h15.8a1.8 1.8 0 0 0 1.6-2.7L13.7 4.3a1.9 1.9 0 0 0-3.4 0Z" />
      <path d="M12 9.5v4.5M12 17h.01" />
    </IconBase>
  );
}

export function BugIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="8" y="8" width="8" height="10" rx="4" />
      <path d="M9.5 8V6.5a2.5 2.5 0 0 1 5 0V8" />
      <path d="M4 10h4M16 10h4M4 14h4M16 14h4M5.5 18l2.8-1.5M18.5 18l-2.8-1.5M5.5 6.5l2.5 1.5M18.5 6.5 16 8" />
    </IconBase>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="m6 9 6 6 6-6" />
    </IconBase>
  );
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="m9 6 6 6-6 6" />
    </IconBase>
  );
}

export function ArrowLeftIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </IconBase>
  );
}

export function SwapIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M7 8h13m0 0-3.5-3.5M20 8l-3.5 3.5" />
      <path d="M17 16H4m0 0 3.5-3.5M4 16l3.5 3.5" />
    </IconBase>
  );
}

export function TerminalIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <path d="m7.5 10 3 2.5-3 2.5M12.5 15h4" />
    </IconBase>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </IconBase>
  );
}

export function CheckCircleIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.2 2.3 2.3 4.7-4.8" />
    </IconBase>
  );
}
