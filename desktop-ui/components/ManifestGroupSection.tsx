import { useEffect, useState } from "react";

import type { ManifestField as MField, SettingsManifest } from "@/api/client";

interface Props {
  groupId: string;
  manifest: SettingsManifest;
  onSave: (key: string, value: unknown) => Promise<void>;
}

export function ManifestGroupSection({ groupId, manifest, onSave }: Props) {
  const group = manifest.groups.find((g) => g.id === groupId);
  const fields = Object.values(manifest.fields)
    .filter((f) => f.group === groupId)
    .sort((a, b) => a.label.localeCompare(b.label));

  if (!group || fields.length === 0) return null;

  return (
    <section className="card">
      <h3 className="font-semibold mb-1">{group.label}</h3>
      {group.description && (
        <p className="text-xs text-ink-dim mb-3">{group.description}</p>
      )}
      <div className="space-y-4">
        {fields.map((field) => (
          <FieldRenderer key={field.key} field={field} onSave={onSave} />
        ))}
      </div>
    </section>
  );
}

// ── Per-field renderer ────────────────────────────────────────────────────────

interface FieldRendererProps {
  field: MField;
  onSave: (key: string, value: unknown) => Promise<void>;
}

function FieldRenderer({ field, onSave }: FieldRendererProps) {
  const currentVal = field.value ?? field.default;

  // Local text/number state — syncs when the manifest value changes externally.
  const [localVal, setLocalVal] = useState(String(currentVal ?? ""));
  useEffect(() => {
    if (field.type !== "bool" && field.type !== "enum") {
      setLocalVal(String(currentVal ?? ""));
    }
  }, [field.key, currentVal, field.type]);

  const label = (
    <div className="flex items-baseline gap-1.5 mb-0.5">
      <span className="text-sm font-medium">{field.label}</span>
      {field.unit && (
        <span className="text-xs text-ink-dim">({field.unit})</span>
      )}
    </div>
  );

  const help = field.description && (
    <p className="text-xs text-ink-dim mt-0.5">{field.description}</p>
  );

  // ── bool ──────────────────────────────────────────────────────────────────
  if (field.type === "bool") {
    return (
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={!!currentVal}
          onChange={(e) => onSave(field.key, e.target.checked)}
        />
        <span>
          {label}
          {help}
        </span>
      </label>
    );
  }

  // ── enum ──────────────────────────────────────────────────────────────────
  if (field.type === "enum") {
    return (
      <div>
        {label}
        <select
          className="input"
          value={String(currentVal ?? "")}
          onChange={(e) => onSave(field.key, e.target.value)}
        >
          {field.options?.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {help}
      </div>
    );
  }

  // ── textarea ──────────────────────────────────────────────────────────────
  if (field.type === "textarea") {
    if (field.read_only) {
      return (
        <div>
          {label}
          <div className="input min-h-[80px] font-mono text-xs bg-bg-2 text-ink-dim whitespace-pre-wrap">
            {localVal}
          </div>
          {help}
        </div>
      );
    }
    return (
      <div>
        {label}
        <textarea
          className="input min-h-[120px] font-mono text-xs"
          value={localVal}
          onChange={(e) => setLocalVal(e.target.value)}
          onBlur={() => onSave(field.key, localVal)}
          placeholder={field.placeholder}
        />
        {help}
      </div>
    );
  }

  // ── read_only display for any other type ──────────────────────────────────
  if (field.read_only) {
    return (
      <div>
        {label}
        <div className="input bg-bg-2 text-ink-dim text-sm">
          {localVal || <span className="italic">not set</span>}
        </div>
        {help}
      </div>
    );
  }

  // ── int / float ───────────────────────────────────────────────────────────
  if (field.type === "int" || field.type === "float") {
    const step = field.type === "int" ? 1 : "any";
    return (
      <div>
        {label}
        <input
          className="input"
          type="number"
          step={step}
          min={field.min}
          max={field.max}
          value={localVal}
          onChange={(e) => setLocalVal(e.target.value)}
          onBlur={() => {
            const parsed =
              field.type === "int"
                ? parseInt(localVal, 10)
                : parseFloat(localVal);
            if (!isNaN(parsed)) onSave(field.key, parsed);
          }}
        />
        {help}
      </div>
    );
  }

  // ── string / url (default) ────────────────────────────────────────────────
  return (
    <div>
      {label}
      <input
        className="input"
        type={field.type === "url" ? "url" : "text"}
        value={localVal}
        placeholder={field.placeholder}
        onChange={(e) => setLocalVal(e.target.value)}
        onBlur={() => onSave(field.key, localVal)}
      />
      {help}
    </div>
  );
}
