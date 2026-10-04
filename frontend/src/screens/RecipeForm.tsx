import { useState } from "react";
import type { RecipeOrigin } from "../api/client";
import { Button } from "../components/ui/Button";
import { FormField } from "../components/ui/FormField";
import { inputStyle } from "../components/ui/styles";
import type { RecipeFormValues } from "./recipeFormValues";

const ORIGINS: RecipeOrigin[] = ["instagram", "web", "cookbook", "manual", "ios_share"];

/**
 * RecipeForm — the shared scalar-field form used by both the manual Add flow
 * (§5.5) and the Edit flow (§5.6). Covers title (required), description, the
 * three time fields, servings, origin, source URL, and source citation. The
 * caller owns submission; this component surfaces a form-level error (from a
 * 400) and a per-title inline error for the required-field case.
 */
export function RecipeForm({
  initial,
  submitLabel,
  submitting,
  formError,
  onSubmit,
  secondaryAction,
}: {
  initial: RecipeFormValues;
  submitLabel: string;
  submitting: boolean;
  formError: string | null;
  onSubmit: (values: RecipeFormValues) => void;
  secondaryAction?: React.ReactNode;
}) {
  const [values, setValues] = useState<RecipeFormValues>(initial);
  const [titleError, setTitleError] = useState<string | null>(null);

  const set = <K extends keyof RecipeFormValues>(key: K, value: RecipeFormValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!values.title.trim()) {
      setTitleError("Title is required.");
      return;
    }
    setTitleError(null);
    onSubmit(values);
  };

  return (
    <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", maxWidth: 640 }}>
      <FormField label="Title" required error={titleError}>
        {(props) => (
          <input
            {...props}
            value={values.title}
            onChange={(e) => set("title", e.target.value)}
            style={inputStyle}
          />
        )}
      </FormField>

      <FormField label="Description">
        {(props) => (
          <textarea
            {...props}
            value={values.description}
            rows={3}
            onChange={(e) => set("description", e.target.value)}
            style={{ ...inputStyle, resize: "vertical" }}
          />
        )}
      </FormField>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-3)" }}>
        <FormField label="Prep (min)">
          {(props) => (
            <input
              {...props}
              type="number"
              min={0}
              value={values.prep_time_min}
              onChange={(e) => set("prep_time_min", e.target.value)}
              style={inputStyle}
            />
          )}
        </FormField>
        <FormField label="Cook (min)">
          {(props) => (
            <input
              {...props}
              type="number"
              min={0}
              value={values.cook_time_min}
              onChange={(e) => set("cook_time_min", e.target.value)}
              style={inputStyle}
            />
          )}
        </FormField>
        <FormField label="Total (min)">
          {(props) => (
            <input
              {...props}
              type="number"
              min={0}
              value={values.total_time_min}
              onChange={(e) => set("total_time_min", e.target.value)}
              style={inputStyle}
            />
          )}
        </FormField>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)" }}>
        <FormField label="Servings">
          {(props) => (
            <input
              {...props}
              type="number"
              min={0}
              value={values.servings}
              onChange={(e) => set("servings", e.target.value)}
              style={inputStyle}
            />
          )}
        </FormField>
        <FormField label="Origin">
          {(props) => (
            <select
              {...props}
              value={values.origin}
              onChange={(e) => set("origin", e.target.value as RecipeOrigin | "")}
              style={inputStyle}
            >
              <option value="">—</option>
              {ORIGINS.map((origin) => (
                <option key={origin} value={origin}>
                  {origin}
                </option>
              ))}
            </select>
          )}
        </FormField>
      </div>

      <FormField label="Source URL">
        {(props) => (
          <input
            {...props}
            type="url"
            value={values.source_url}
            onChange={(e) => set("source_url", e.target.value)}
            style={inputStyle}
          />
        )}
      </FormField>

      <FormField label="Source citation">
        {(props) => (
          <input
            {...props}
            value={values.source_citation}
            onChange={(e) => set("source_citation", e.target.value)}
            style={inputStyle}
          />
        )}
      </FormField>

      {formError && (
        <p role="alert" style={{ color: "var(--color-error)", margin: 0 }}>
          {formError}
        </p>
      )}

      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        <Button type="submit" loading={submitting}>
          {submitLabel}
        </Button>
        {secondaryAction}
      </div>
    </form>
  );
}
