import { useId, useState } from "react";

interface DisplayProps {
  /** Average/value to display (may be fractional for display). */
  value: number | null | undefined;
  /** Interactive input is disabled in display mode. */
  readOnly: true;
  size?: number;
}

interface InputProps {
  /** Current integer rating 1..5 (0/undefined = unrated). */
  value: number | null | undefined;
  readOnly?: false;
  /** Called with the chosen integer rating (1..5). */
  onChange: (rating: number) => void;
  size?: number;
  /** Accessible label for the radiogroup. */
  label?: string;
}

type StarRatingProps = DisplayProps | InputProps;

const STARS = [1, 2, 3, 4, 5];

/**
 * StarRating (web-ui-spec §6) — a 5-star control with two variants:
 *   - display (`readOnly`): shows a fractional average via partial fills.
 *   - interactive input: a keyboard-operable radiogroup (1..5) that calls
 *     `onChange`. Arrow keys and digit keys move the selection; filled stars
 *     use the primary color.
 */
export function StarRating(props: StarRatingProps) {
  const size = props.size ?? 18;

  if (props.readOnly) {
    return <DisplayStars value={props.value ?? 0} size={size} />;
  }

  return (
    <InputStars
      value={props.value ?? 0}
      size={size}
      label={props.label ?? "Rating"}
      onChange={props.onChange}
    />
  );
}

function Star({ fill, size }: { fill: number; size: number }) {
  // `fill` is 0..1 — how much of this star is filled.
  const clamped = Math.max(0, Math.min(1, fill));
  return (
    <span
      aria-hidden
      style={{
        position: "relative",
        display: "inline-block",
        width: size,
        height: size,
        lineHeight: `${size}px`,
        fontSize: size,
      }}
    >
      <span style={{ color: "rgba(31, 27, 22, 0.2)" }}>★</span>
      <span
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          overflow: "hidden",
          width: `${clamped * 100}%`,
          color: "var(--color-rating)",
        }}
      >
        ★
      </span>
    </span>
  );
}

function DisplayStars({ value, size }: { value: number; size: number }) {
  return (
    <span
      role="img"
      aria-label={`${value.toFixed(1)} out of 5 stars`}
      style={{ display: "inline-flex", gap: 2 }}
    >
      {STARS.map((n) => (
        <Star key={n} fill={value - (n - 1)} size={size} />
      ))}
    </span>
  );
}

function InputStars({
  value,
  size,
  label,
  onChange,
}: {
  value: number;
  size: number;
  label: string;
  onChange: (rating: number) => void;
}) {
  const groupId = useId();
  const [focusValue, setFocusValue] = useState(value || 1);

  const handleKeyDown = (event: React.KeyboardEvent) => {
    let next = focusValue;
    if (event.key === "ArrowRight" || event.key === "ArrowUp") {
      next = Math.min(5, focusValue + 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
      next = Math.max(1, focusValue - 1);
    } else if (/^[1-5]$/.test(event.key)) {
      next = Number(event.key);
    } else {
      return;
    }
    event.preventDefault();
    setFocusValue(next);
    onChange(next);
  };

  return (
    <span
      role="radiogroup"
      aria-label={label}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      onFocus={() => setFocusValue(value || 1)}
      style={{ display: "inline-flex", gap: 4, cursor: "pointer" }}
    >
      {STARS.map((n) => (
        <span
          key={n}
          role="radio"
          id={`${groupId}-${n}`}
          aria-checked={value === n}
          aria-label={`${n} star${n > 1 ? "s" : ""}`}
          onClick={() => {
            setFocusValue(n);
            onChange(n);
          }}
        >
          <Star fill={n <= value ? 1 : 0} size={size} />
        </span>
      ))}
    </span>
  );
}
