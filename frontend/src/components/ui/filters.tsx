import { useId, useState } from "react";
import type { SearchSort } from "../../api/client";
import { TagPill } from "./TagPill";
import { inputStyle } from "./styles";

/**
 * ChipInput (web-ui-spec §6 filter controls) — a repeatable text entry that
 * accumulates values as removable chips (AND logic). Used for ingredients,
 * tags, and tools in the filter sidebar. Enter (or comma) commits the current
 * text; duplicates and blanks are ignored.
 */
export function ChipInput({
  label,
  placeholder,
  values,
  onChange,
  suggestions,
}: {
  label: string;
  placeholder?: string;
  values: string[];
  onChange: (next: string[]) => void;
  /** Optional datalist suggestions (e.g. the user's known tags). */
  suggestions?: string[];
}) {
  const id = useId();
  const listId = `${id}-list`;
  const [draft, setDraft] = useState("");

  const commit = (raw: string) => {
    const value = raw.trim();
    if (!value) return;
    if (values.some((v) => v.toLowerCase() === value.toLowerCase())) {
      setDraft("");
      return;
    }
    onChange([...values, value]);
    setDraft("");
  };

  const remove = (value: string) => {
    onChange(values.filter((v) => v !== value));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <label htmlFor={id} style={{ fontWeight: 600, fontSize: "0.875rem" }}>
        {label}
      </label>
      <input
        id={id}
        value={draft}
        placeholder={placeholder}
        list={suggestions && suggestions.length > 0 ? listId : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit(draft);
          } else if (e.key === "Backspace" && draft === "" && values.length > 0) {
            remove(values[values.length - 1]);
          }
        }}
        onBlur={() => commit(draft)}
        style={inputStyle}
      />
      {suggestions && suggestions.length > 0 && (
        <datalist id={listId}>
          {suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      )}
      {values.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)", marginTop: "var(--space-1)" }}>
          {values.map((value) => (
            <TagPill key={value} label={value} selected onRemove={() => remove(value)} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * MultiSelect (web-ui-spec §6) — pick several values from a fixed option list
 * (AND logic). Rendered as toggleable pills. Used for tag/tool filters when a
 * known option list is available.
 */
export function MultiSelect({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (option: string) => {
    if (selected.includes(option)) {
      onChange(selected.filter((v) => v !== option));
    } else {
      onChange([...selected, option]);
    }
  };

  return (
    <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
      <legend style={{ fontWeight: 600, fontSize: "0.875rem", padding: 0 }}>{label}</legend>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)", marginTop: "var(--space-2)" }}>
        {options.length === 0 ? (
          <span style={{ color: "var(--color-text-secondary)", fontSize: "0.8125rem" }}>
            No options yet.
          </span>
        ) : (
          options.map((option) => (
            <TagPill
              key={option}
              label={option}
              selected={selected.includes(option)}
              onClick={() => toggle(option)}
            />
          ))
        )}
      </div>
    </fieldset>
  );
}

const SORT_OPTIONS: { value: SearchSort; label: string }[] = [
  { value: "newest", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "most_cooked", label: "Most cooked" },
  { value: "highest_rated", label: "Highest rated" },
];

/** SortDropdown (web-ui-spec §6) — maps to GET /search `sort`. */
export function SortDropdown({
  value,
  onChange,
}: {
  value: SearchSort;
  onChange: (next: SearchSort) => void;
}) {
  const id = useId();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <label htmlFor={id} style={{ fontWeight: 600, fontSize: "0.875rem" }}>
        Sort
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value as SearchSort)}
        style={inputStyle}
      >
        {SORT_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/**
 * TimeStepper (web-ui-spec §6) — a stepper for the `max_time` filter (minutes).
 * `null` means "no limit". Steps by 5 minutes, floored at 5.
 */
export function TimeStepper({
  value,
  onChange,
  step = 5,
}: {
  value: number | null;
  onChange: (next: number | null) => void;
  step?: number;
}) {
  const id = useId();
  const current = value ?? 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <label htmlFor={id} style={{ fontWeight: 600, fontSize: "0.875rem" }}>
        Max total time
      </label>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
        <button
          type="button"
          aria-label="Decrease max time"
          onClick={() => {
            const next = current - step;
            onChange(next <= 0 ? null : next);
          }}
          style={stepperButton}
        >
          −
        </button>
        <input
          id={id}
          type="number"
          min={0}
          step={step}
          value={value ?? ""}
          placeholder="Any"
          onChange={(e) => {
            const raw = e.target.value;
            onChange(raw === "" ? null : Math.max(0, Number(raw)) || null);
          }}
          style={{ ...inputStyle, width: 90, textAlign: "center" }}
        />
        <button
          type="button"
          aria-label="Increase max time"
          onClick={() => onChange(current + step)}
          style={stepperButton}
        >
          +
        </button>
        <span style={{ color: "var(--color-text-secondary)", fontSize: "0.8125rem" }}>min</span>
      </div>
    </div>
  );
}

const stepperButton: React.CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: "var(--radius-input)",
  border: "1px solid rgba(31, 27, 22, 0.18)",
  background: "var(--color-surface)",
  cursor: "pointer",
  fontSize: "1.125rem",
  lineHeight: 1,
};
