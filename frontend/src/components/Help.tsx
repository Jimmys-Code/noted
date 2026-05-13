import { AnimatePresence, motion } from "framer-motion";

const GROUPS: { title: string; items: [string, string][] }[] = [
  {
    title: "Global",
    items: [
      ["?", "Toggle this help (outside editor)"],
      ["Ctrl+B", "Toggle sidebar"],
      ["Ctrl+E", "Jump to explorer (sidebar)"],
      ["Ctrl+1", "Jump to editor body"],
      ["Ctrl+T", "Jump to note title"],
      ["Ctrl+N", "New note in current folder"],
      ["Ctrl+Shift+N", "New folder"],
      ["Ctrl+Shift+P", "Toggle preview"],
      ["Ctrl+S", "Force save"],
      ["Esc", "Close dialogs / leave editor focus"],
    ],
  },
  {
    title: "Sidebar (when focused)",
    items: [
      ["j / ↓", "Next item"],
      ["k / ↑", "Previous item"],
      ["l / →", "Expand folder"],
      ["h / ←", "Collapse folder / jump to parent"],
      ["Enter / o", "Open note or toggle folder"],
      ["r", "Rename folder"],
      ["c", "Cycle folder color"],
      ["d", "Delete folder or note"],
      ["g g", "Jump to top"],
      ["G", "Jump to bottom"],
    ],
  },
  {
    title: "Editor",
    items: [
      ["Vim mode", "Full vim motions when the VIM pill is on"],
      ["Esc", "Return to normal mode (vim) / blur (non-vim)"],
    ],
  },
];

export default function Help({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="help-backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="help-modal"
            initial={{ opacity: 0, y: 16, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 320, damping: 28 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="help-header">
              <h2>Keyboard controls</h2>
              <button className="x-btn" onClick={onClose} title="Close (Esc)">×</button>
            </div>
            <div className="help-body">
              {GROUPS.map((g) => (
                <section key={g.title}>
                  <h3>{g.title}</h3>
                  <ul>
                    {g.items.map(([k, d]) => (
                      <li key={k}>
                        <kbd>{k}</kbd>
                        <span>{d}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
