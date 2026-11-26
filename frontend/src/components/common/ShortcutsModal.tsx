const SHORTCUTS: ReadonlyArray<[string, string]> = [
  ["j / ↓", "Select next item"],
  ["k / ↑", "Select previous item"],
  ["m", "Toggle selected item read / unread"],
  ["s", "Toggle selected item starred"],
  ["o / Enter", "Open external article URL in new tab & mark read"],
  ["/", "Focus global search bar"],
  ["r", "Trigger manual sync (when online)"],
  ["Shift + A", "Bounded mark current view read"],
  ["?", "Toggle this shortcuts help modal"],
  ["Esc", "Close dialog or clear search focus"],
];

export function ShortcutsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) {
    return null;
  }
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4"
    >
      <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold">Keyboard Shortcuts</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            Close (Esc)
          </button>
        </div>
        <dl className="divide-y divide-slate-100 text-sm dark:divide-slate-800">
          {SHORTCUTS.map(([keys, desc]) => (
            <div key={keys} className="flex items-center justify-between py-2">
              <dt className="font-mono text-xs font-semibold text-sky-700 dark:text-sky-300">
                {keys}
              </dt>
              <dd className="text-right text-slate-700 dark:text-slate-300">{desc}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
