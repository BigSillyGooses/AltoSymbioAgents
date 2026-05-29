import { useAppStore, type ActiveView } from "@/stores/appStore";

interface NavItem {
  id: ActiveView;
  label: string;
  hint: string;
  studioOnly?: boolean;
}

interface NavSection {
  id: string;
  label: string;
  items: NavItem[];
}

// E carry-over: 15 flat items → grouped into four sections so the
// related controls aren't scattered across the list. "Review queue"
// gathers everything that gates a pending human decision so the
// total-badge sum at the section header tells the user at a glance
// whether anything needs attention.
const NAV_SECTIONS: NavSection[] = [
  {
    id: "workspace",
    label: "Workspace",
    items: [
      { id: "chat",   label: "Chat",   hint: "Talk to your team" },
      { id: "agents", label: "Agents", hint: "Define agents and teams" },
      { id: "models", label: "Models", hint: "Browse and switch local models" },
    ],
  },
  {
    id: "knowledge",
    label: "Knowledge",
    items: [
      { id: "rag",            label: "Documents",     hint: "Index files and folders" },
      { id: "memory",         label: "Memory",        hint: "Search session facts" },
      { id: "saved_prompts",  label: "Saved prompts", hint: "Snippets and system prompt templates" },
      { id: "prompts",        label: "Prompts",       hint: "Manage system prompts", studioOnly: true },
    ],
  },
  {
    id: "review",
    label: "Review queue",
    items: [
      { id: "memory_review", label: "Memory writes",   hint: "Approve memory writes" },
      { id: "escalations",   label: "Paused actions",  hint: "Approve paused actions" },
      { id: "safety",        label: "Safety",          hint: "Escalations, gate, canary, voting" },
    ],
  },
  {
    id: "system",
    label: "System",
    items: [
      { id: "mcp",         label: "Tool servers", hint: "MCP-managed external tools", studioOnly: true },
      { id: "security",    label: "Security",     hint: "Firewall + scan log", studioOnly: true },
      { id: "usage",       label: "Usage",        hint: "Token consumption and cost" },
      { id: "settings",    label: "Settings",     hint: "API keys, models, routing" },
      { id: "diagnostics", label: "Diagnostics",  hint: "Health + error logs", studioOnly: true },
    ],
  },
];

export function Sidebar() {
  const active = useAppStore((s) => s.activeView);
  const studio = useAppStore((s) => s.studioMode);
  const setActive = useAppStore((s) => s.setActiveView);
  const setStudio = useAppStore((s) => s.setStudioMode);
  const pendingCount = useAppStore((s) => s.pendingEscalations.length);
  const memoryReviewCount = useAppStore((s) => s.pendingMemoryWrites.length);

  const badgeCount = (id: ActiveView): number => {
    if (id === "escalations") return pendingCount;
    if (id === "memory_review") return memoryReviewCount;
    return 0;
  };

  return (
    <aside className="flex flex-col w-56 min-w-56 bg-gradient-to-b from-white to-bg-2/60">
      <div className="px-4 py-5">
        <div className="flex items-center gap-3">
          {/* Soft sunrise mark — lavender → blush → peach, no letters */}
          <div
            className="h-9 w-9 rounded-2xl shadow-soft-2"
            style={{
              backgroundImage:
                "linear-gradient(135deg, #c4b8ff 0%, #e8d4f0 55%, #f4c8b8 100%)",
            }}
            aria-hidden
          />
          <div className="leading-tight">
            <div className="text-[15px] font-semibold tracking-tight">
              alto
            </div>
            <div className="text-[11px] text-ink-faint -mt-0.5">
              symbiosis
            </div>
          </div>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-2">
        {NAV_SECTIONS.map((section, sectionIdx) => {
          const visibleItems = section.items.filter(
            (n) => studio || !n.studioOnly,
          );
          if (visibleItems.length === 0) return null;
          const sectionBadge = visibleItems.reduce(
            (sum, n) => sum + badgeCount(n.id),
            0,
          );
          return (
            <div
              key={section.id}
              data-testid={`sidebar-section-${section.id}`}
              className={sectionIdx === 0 ? "" : "mt-3 pt-3 border-t border-line/40"}
            >
              <div className="px-3 pt-1 pb-1 text-[10px] uppercase tracking-wider text-ink-faint flex items-center justify-between">
                <span>{section.label}</span>
                {sectionBadge > 0 && (
                  <span
                    className="inline-flex items-center justify-center min-w-[18px] h-[16px] px-1 text-[10px] font-medium rounded-full bg-accent/20 text-ink"
                    data-testid={`sidebar-section-badge-${section.id}`}
                  >
                    {sectionBadge}
                  </span>
                )}
              </div>
              {visibleItems.map((item) => {
                const isActive = active === item.id;
                const count = badgeCount(item.id);
                const showBadge = count > 0;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setActive(item.id)}
                    aria-label={item.label}
                    aria-current={isActive ? "page" : undefined}
                    title={item.hint}
                    className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between rounded-lg my-0.5 transition ${
                      isActive
                        ? "bg-white text-ink shadow-soft-1"
                        : "text-ink-dim hover:bg-white/60 hover:text-ink"
                    }`}
                  >
                    <span className="font-medium">{item.label}</span>
                    {showBadge && (
                      <span className="inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 text-[11px] font-medium rounded-full bg-accent/20 text-ink">
                        {count}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          );
        })}
      </nav>

      <div className="px-4 py-3 flex items-center justify-between text-xs text-ink-dim">
        <span>Studio mode</span>
        <button
          type="button"
          onClick={() => setStudio(!studio)}
          className={`h-5 w-9 rounded-full transition relative shadow-soft-inset ${
            studio ? "bg-accent" : "bg-bg-3"
          }`}
          aria-pressed={studio}
          aria-label="Toggle studio mode"
        >
          <span
            className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow-soft-1 transition-all ${
              studio ? "left-4" : "left-0.5"
            }`}
          />
        </button>
      </div>
    </aside>
  );
}
