/* Keyboard shortcuts (spec §167, §169). Single source for the command palette and help. */

export interface Shortcut {
  keys: string[];
  label: string;
  scope: "global" | "table" | "chat";
}

export const SHORTCUTS: Shortcut[] = [
  { keys: ["⌘", "K"], label: "Command palette", scope: "global" },
  { keys: ["⌘", "J"], label: "Focus the assistant", scope: "global" },
  { keys: ["⌘", "\\"], label: "Toggle the assistant panel", scope: "global" },
  { keys: ["⌘", "B"], label: "Toggle the sidebar", scope: "global" },
  { keys: ["Esc"], label: "Close drawer / dialog, clear selection", scope: "global" },
  { keys: ["↑", "↓", "←", "→"], label: "Move between cells", scope: "table" },
  { keys: ["↵"], label: "Open lead / edit cell", scope: "table" },
  { keys: ["F2"], label: "Edit cell", scope: "table" },
  { keys: ["Space"], label: "Select row", scope: "table" },
  { keys: ["⇧", "Space"], label: "Select range", scope: "table" },
  { keys: ["⌘", "C"], label: "Copy cell", scope: "table" },
  { keys: ["⌘", "A"], label: "Select all loaded rows", scope: "table" },
  { keys: ["↵"], label: "Send message", scope: "chat" },
  { keys: ["⇧", "↵"], label: "New line", scope: "chat" },
];
