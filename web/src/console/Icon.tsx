import type { CSSProperties } from "react";

/** 静态图标 sprite：只包含本项目自带的固定图形，不渲染任何后端内容。 */
export function IconSprite() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true">
      <symbol id="i-edit" viewBox="0 0 24 24"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/></symbol>
      <symbol id="i-pencil" viewBox="0 0 24 24"><path d="M4 20h4L20 8a2.5 2.5 0 0 0-3.5-3.5L4 16Z"/><path d="m14.5 6.5 3.5 3.5"/></symbol>
      <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/></symbol>
      <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.7l1.8 2H18.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z"/></symbol>
      <symbol id="i-chev-d" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
      <symbol id="i-chev-r" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></symbol>
      <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
      <symbol id="i-arrow-up" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></symbol>
      <symbol id="i-link" viewBox="0 0 24 24"><path d="M10.5 13.5a5 5 0 0 0 7 0l2.5-2.5a5 5 0 0 0-7-7l-1 1"/><path d="M13.5 10.5a5 5 0 0 0-7 0L4 13a5 5 0 0 0 7 7l1-1"/></symbol>
      <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6Z"/><path d="m9.2 12.1 2 2 3.6-3.8"/></symbol>
      <symbol id="i-file" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/></symbol>
      <symbol id="i-activity" viewBox="0 0 24 24"><path d="M3 12h4l3 8 4-16 3 8h4"/></symbol>
      <symbol id="i-terminal" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7 9 3 3-3 3M13 15h4"/></symbol>
      <symbol id="i-coin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M14.4 9.4c-.6-.6-1.5-.9-2.4-.9-1.4 0-2.5.7-2.5 1.8s1 1.5 2.5 1.7 2.5.7 2.5 1.8-1.1 1.8-2.5 1.8c-1 0-1.9-.4-2.5-1"/></symbol>
      <symbol id="i-chart" viewBox="0 0 24 24"><path d="M4 20V11M10 20V5M16 20v-6M22 20H2"/></symbol>
      <symbol id="i-doc" viewBox="0 0 24 24"><path d="M8 3h6l5 5v13H8Z"/><path d="M14 3v5h5"/><path d="M11 13h5M11 17h3"/></symbol>
      <symbol id="i-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.6 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></symbol>
      <symbol id="i-close" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
      <symbol id="i-check" viewBox="0 0 24 24"><path d="m5 13 4 4L19 7"/></symbol>
      <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4.2l2.8 1.8"/></symbol>
      <symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 4 3.5 19.5h17Z"/><path d="M12 10v4M12 17h.01"/></symbol>
      <symbol id="i-pause" viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></symbol>
      <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/></symbol>
      <symbol id="i-play" viewBox="0 0 24 24"><path d="m8.5 5.5 10 6.5-10 6.5Z"/></symbol>
      <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 6.4"/><path d="M20 5v6h-6"/></symbol>
      <symbol id="i-db" viewBox="0 0 24 24"><ellipse cx="12" cy="5.5" rx="8" ry="2.8"/><path d="M4 5.5v6c0 1.6 3.6 2.9 8 2.9s8-1.3 8-2.9v-6"/><path d="M4 11.5v6c0 1.6 3.6 2.9 8 2.9s8-1.3 8-2.9v-6"/></symbol>
      <symbol id="i-spark" viewBox="0 0 24 24"><path d="M11 3.5 12.7 8 17 9.6 12.7 11.3 11 16l-1.7-4.7L5 9.6 9.3 8Z"/><path d="M18 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/></symbol>
      <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></symbol>
      <symbol id="i-panel" viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M9.5 4.5v15"/></symbol>
      <symbol id="i-send" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6"/></symbol>
      <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></symbol>
      <symbol id="i-more" viewBox="0 0 24 24"><circle cx="5.5" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.1" fill="currentColor" stroke="none"/></symbol>
      <symbol id="i-paperclip" viewBox="0 0 24 24"><path d="M20 11.5 12.5 19a5 5 0 0 1-7-7L13 4.5a3.3 3.3 0 0 1 4.7 4.7L10 16.9a1.7 1.7 0 0 1-2.4-2.4l6.6-6.6"/></symbol>
      <symbol id="i-layers" viewBox="0 0 24 24"><path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 13 9 5 9-5"/></symbol>
      <symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c2.4 3 2.4 13 0 16M12 4c-2.4 3-2.4 13 0 16"/></symbol>
      <symbol id="i-gauge" viewBox="0 0 24 24"><path d="M4 18a8 8 0 1 1 16 0"/><path d="m12 14 4-4"/><circle cx="12" cy="14" r="1.6"/></symbol>
      <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5c0-3.8 3.4-5.8 7.5-5.8s7.5 2 7.5 5.8"/></symbol>
      <symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/></symbol>
      <symbol id="i-bug" viewBox="0 0 24 24"><rect x="8" y="7" width="8" height="10" rx="4"/><path d="M8 11H4.5M19.5 11H16M8 15H4.5M19.5 15H16M12 7V3.5M9.2 7.6 7 5.4M14.8 7.6 17 5.4"/></symbol>
    </svg>
  );
}

export function Icon({
  name,
  size,
  className = "",
  style,
}: {
  name: string;
  size?: "sm" | "lg";
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg className={`ic ${size === "sm" ? "sm" : size === "lg" ? "lg" : ""} ${className}`.trim()} style={style} aria-hidden="true">
      <use href={`#i-${name}`} />
    </svg>
  );
}
