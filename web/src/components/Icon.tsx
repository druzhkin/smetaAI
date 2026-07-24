type IconName =
  | "portfolio"
  | "tasks"
  | "search"
  | "arrow"
  | "shield"
  | "logout"
  | "warning"
  | "check"
  | "trace"
  | "refresh";

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "square" as const,
    strokeLinejoin: "miter" as const,
    "aria-hidden": true,
  };

  switch (name) {
    case "portfolio":
      return (
        <svg {...common}>
          <path d="M4 5h16v14H4zM4 10h16M9 10v9" />
        </svg>
      );
    case "tasks":
      return (
        <svg {...common}>
          <path d="M5 4h14v16H5zM8 9l2 2 5-5M8 16h8" />
        </svg>
      );
    case "search":
      return (
        <svg {...common}>
          <circle cx="10.5" cy="10.5" r="6.5" />
          <path d="m15.5 15.5 4 4" />
        </svg>
      );
    case "arrow":
      return (
        <svg {...common}>
          <path d="M5 12h14M14 7l5 5-5 5" />
        </svg>
      );
    case "shield":
      return (
        <svg {...common}>
          <path d="M12 3 5 6v5c0 5 3 8 7 10 4-2 7-5 7-10V6z" />
          <path d="m9 12 2 2 4-5" />
        </svg>
      );
    case "logout":
      return (
        <svg {...common}>
          <path d="M10 4H5v16h5M14 8l4 4-4 4M8 12h10" />
        </svg>
      );
    case "warning":
      return (
        <svg {...common}>
          <path d="M12 3 2.5 20h19zM12 9v5M12 17v.1" />
        </svg>
      );
    case "check":
      return (
        <svg {...common}>
          <path d="m5 12 4 4L19 6" />
        </svg>
      );
    case "trace":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="2" />
          <circle cx="18" cy="18" r="2" />
          <path d="M8 6h5a3 3 0 0 1 3 3v1M16 14v-4M8 18h8" />
        </svg>
      );
    case "refresh":
      return (
        <svg {...common}>
          <path d="M20 7v5h-5M4 17v-5h5M18 10a7 7 0 0 0-12-3l-2 2M6 14a7 7 0 0 0 12 3l2-2" />
        </svg>
      );
  }
}
