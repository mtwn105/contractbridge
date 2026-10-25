const paths: Record<string, React.ReactNode> = {
  bridge: <><path d="M3 18V8m18 10V8M3 11c5 0 5-6 9-6s4 6 9 6M3 15h18M7 10v5m5-10v10m5-5v5" /></>,
  grid: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  chevron: <path d="m9 5 7 7-7 7" />,
  plus: <path d="M12 5v14M5 12h14" />,
  diff: <><path d="M6 3v12a3 3 0 0 0 3 3h9M18 21V9a3 3 0 0 0-3-3H6" /><path d="m14 14 4 4-4 4M10 2 6 6l4 4" /></>,
  map: <><circle cx="5" cy="6" r="2" /><circle cx="5" cy="18" r="2" /><circle cx="19" cy="12" r="2" /><path d="M7 6h3c5 0 1 6 7 6M7 18h3c5 0 1-6 7-6" /></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" /><path d="m8 12 3 3 5-6" /></>,
  play: <path d="m8 4 13 8-13 8z" />,
  code: <><path d="m8 5-6 7 6 7m8-14 6 7-6 7m-3-17-2 20" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  check: <path d="m5 12 4 4L20 5" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" /></>,
  file: <><path d="M5 3h9l5 5v13H5zM14 3v6h5M9 13h6m-6 4h6" /></>,
  warning: <><path d="m12 3 10 18H2zM12 9v5" /><path d="M12 17h.01" /></>,
  logout: <><path d="M9 4H3v16h6m7-15 6 7-6 7M8 12h14" /></>,
  terminal: <><rect x="2" y="4" width="20" height="16" rx="2" /><path d="m6 9 3 3-3 3m6 0h5" /></>,
  external: <><path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7" /></>,
};
export function Icon({ name, size = 18, className = "" }: { name: string; size?: number; className?: string }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>{paths[name] || paths.file}</svg>;
}
