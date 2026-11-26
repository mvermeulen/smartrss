import { useEffect } from "react";

export interface KeyboardShortcutHandlers {
  onNext: () => void;
  onPrev: () => void;
  onToggleRead: () => void;
  onToggleStar: () => void;
  onOpenExternal: () => void;
  onFocusSearch: () => void;
  onSync: () => void;
  onBulkRead: () => void;
  onToggleHelp: () => void;
  onEscape: () => void;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  return target.isContentEditable;
}

export function useKeyboardShortcuts(handlers: KeyboardShortcutHandlers, enabled = true): void {
  useEffect(() => {
    if (!enabled) {
      return;
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handlers.onEscape();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) {
        return;
      }
      if (isEditableTarget(e.target)) {
        return;
      }

      switch (e.key) {
        case "j":
        case "ArrowDown":
          e.preventDefault();
          handlers.onNext();
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          handlers.onPrev();
          break;
        case "m":
          e.preventDefault();
          handlers.onToggleRead();
          break;
        case "s":
          e.preventDefault();
          handlers.onToggleStar();
          break;
        case "o":
        case "Enter":
          e.preventDefault();
          handlers.onOpenExternal();
          break;
        case "/":
          e.preventDefault();
          handlers.onFocusSearch();
          break;
        case "r":
          e.preventDefault();
          handlers.onSync();
          break;
        case "A":
          if (e.shiftKey) {
            e.preventDefault();
            handlers.onBulkRead();
          }
          break;
        case "?":
          e.preventDefault();
          handlers.onToggleHelp();
          break;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled, handlers]);
}
