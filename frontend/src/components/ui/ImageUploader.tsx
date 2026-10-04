import { useRef, useState } from "react";
import { ACCEPT_ATTR, validateUploadFile } from "./uploadConstraints";

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading"; fraction: number }
  | { phase: "error"; message: string };

/**
 * ImageUploader / dropzone (web-ui-spec §6) — pick or drop a file. Renders
 * idle, uploading-with-progress, and error states. Validation runs on
 * selection; the parent handles the actual presigned PUT and feeds progress
 * back via the `state` prop.
 */
export function ImageUploader({
  state,
  onFileSelected,
  disabled,
}: {
  state: UploadState;
  /** Called with a validated file; parent kicks off the upload. */
  onFileSelected: (file: File) => void;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleFile = (file: File | undefined) => {
    if (!file) return;
    const error = validateUploadFile(file);
    if (error) {
      setLocalError(error);
      return;
    }
    setLocalError(null);
    onFileSelected(file);
  };

  const errorMessage =
    localError ?? (state.phase === "error" ? state.message : null);

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload a recipe photo"
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !disabled) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!disabled) handleFile(e.dataTransfer.files?.[0]);
        }}
        style={{
          border: `2px dashed ${
            dragging ? "var(--color-primary)" : "rgba(31, 27, 22, 0.2)"
          }`,
          borderRadius: "var(--radius-card)",
          padding: "var(--space-8)",
          textAlign: "center",
          background: dragging ? "rgba(232, 86, 42, 0.04)" : "var(--color-surface)",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.6 : 1,
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT_ATTR}
          hidden
          onChange={(e) => handleFile(e.target.files?.[0])}
        />

        {state.phase === "uploading" ? (
          <div>
            <p style={{ margin: "0 0 var(--space-3)", fontWeight: 600 }}>
              Uploading… {Math.round(state.fraction * 100)}%
            </p>
            <div
              role="progressbar"
              aria-valuenow={Math.round(state.fraction * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
              style={{
                height: 8,
                borderRadius: "var(--radius-pill)",
                background: "rgba(31, 27, 22, 0.1)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${state.fraction * 100}%`,
                  background: "var(--color-primary)",
                  transition: "width 0.2s ease",
                }}
              />
            </div>
          </div>
        ) : (
          <>
            <div aria-hidden style={{ fontSize: "2rem", marginBottom: "var(--space-2)" }}>
              📷
            </div>
            <p style={{ margin: "0 0 var(--space-1)", fontWeight: 600 }}>
              Drop a photo here, or click to pick
            </p>
            <p style={{ margin: 0, color: "var(--color-text-secondary)", fontSize: "0.8125rem" }}>
              JPEG, PNG, HEIC, or PDF · up to 20 MB
            </p>
          </>
        )}
      </div>

      {errorMessage && (
        <p role="alert" style={{ color: "var(--color-error)", marginTop: "var(--space-2)" }}>
          {errorMessage}
        </p>
      )}
    </div>
  );
}
