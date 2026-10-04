import { useState } from "react";
import {
  createCook,
  updateCook,
  uploadUrl,
  putToPresignedUrl,
  type CookLog,
  type UploadContentType,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { Modal } from "../components/ui/Modal";
import { FormField } from "../components/ui/FormField";
import { inputStyle } from "../components/ui/styles";
import { StarRating } from "../components/ui/StarRating";
import { ImageUploader } from "../components/ui/ImageUploader";
import { validateUploadFile } from "../components/ui/uploadConstraints";
import { fieldErrorMessage } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";
import { todayIso } from "../components/ui/format";

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading"; fraction: number }
  | { phase: "error"; message: string };

/**
 * Cook log add/edit form (web-ui-spec §5.7) — a modal with a required date
 * picker, a 1–5 star rating, a notes textarea, and an optional photo upload
 * that reuses the presigned-URL flow (→ `photo_s3_key`). On a successful save
 * it calls `onSaved` so the detail screen can refetch (updating cook_count /
 * avg_rating).
 */
export function CookLogForm({
  recipeId,
  existing,
  onSaved,
  onClose,
}: {
  recipeId: string;
  /** When present the form edits this entry (PATCH); else it adds (POST). */
  existing?: CookLog;
  onSaved: () => void;
  onClose: () => void;
}) {
  const handleError = useApiErrorHandler();
  const editing = Boolean(existing);

  const [cookedAt, setCookedAt] = useState(existing?.cooked_at ?? todayIso());
  const [rating, setRating] = useState<number | null>(existing?.rating ?? null);
  const [notes, setNotes] = useState(existing?.notes ?? "");
  // The already-uploaded key for the photo (new uploads overwrite this).
  const [photoKey, setPhotoKey] = useState<string | null>(null);
  const [photoPreviewUrl, setPhotoPreviewUrl] = useState<string | null>(
    existing?.photo_url ?? null,
  );
  const [upload, setUpload] = useState<UploadState>({ phase: "idle" });

  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [dateError, setDateError] = useState<string | null>(null);

  const handlePhoto = async (file: File) => {
    const validationError = validateUploadFile(file);
    if (validationError) {
      setUpload({ phase: "error", message: validationError });
      return;
    }
    setUpload({ phase: "uploading", fraction: 0 });
    try {
      const { upload_url, s3_key } = await uploadUrl({
        filename: file.name,
        content_type: file.type as UploadContentType,
      });
      await putToPresignedUrl(upload_url, file, file.type, (fraction) =>
        setUpload({ phase: "uploading", fraction }),
      );
      setPhotoKey(s3_key);
      setPhotoPreviewUrl(URL.createObjectURL(file));
      setUpload({ phase: "idle" });
    } catch (err) {
      setUpload({ phase: "error", message: handleError(err) });
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError(null);
    setDateError(null);

    if (!cookedAt) {
      setDateError("A cook date is required.");
      return;
    }

    setSaving(true);
    try {
      if (editing && existing) {
        // PATCH does not accept cooked_at (fixed at creation); send the
        // editable fields only. Only send photo_s3_key when a new photo was
        // uploaded this session.
        await updateCook(recipeId, existing.id, {
          notes: notes.trim() || null,
          rating,
          ...(photoKey ? { photo_s3_key: photoKey } : {}),
        });
      } else {
        await createCook(recipeId, {
          cooked_at: cookedAt,
          notes: notes.trim() || null,
          rating,
          ...(photoKey ? { photo_s3_key: photoKey } : {}),
        });
      }
      onSaved();
    } catch (err) {
      const fieldMsg = fieldErrorMessage(err);
      setFormError(fieldMsg ?? handleError(err));
      setSaving(false);
    }
  };

  return (
    <Modal title={editing ? "Edit cook" : "Log a cook"} onClose={onClose}>
      <form
        onSubmit={handleSubmit}
        style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}
      >
        <FormField label="Date cooked" required error={dateError}>
          {(props) => (
            <input
              {...props}
              type="date"
              value={cookedAt}
              max={todayIso()}
              disabled={editing}
              onChange={(e) => setCookedAt(e.target.value)}
              style={inputStyle}
            />
          )}
        </FormField>
        {editing && (
          <p style={{ margin: "-8px 0 0", color: "var(--color-text-secondary)", fontSize: "0.8125rem" }}>
            The cook date is fixed at creation and can't be changed.
          </p>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          <span style={{ fontWeight: 600, fontSize: "0.875rem" }}>Rating</span>
          <StarRating
            value={rating ?? 0}
            label="Rating"
            onChange={(value) => setRating(value)}
          />
        </div>

        <FormField label="Notes">
          {(props) => (
            <textarea
              {...props}
              value={notes}
              rows={3}
              onChange={(e) => setNotes(e.target.value)}
              style={{ ...inputStyle, resize: "vertical" }}
            />
          )}
        </FormField>

        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <span style={{ fontWeight: 600, fontSize: "0.875rem" }}>Photo (optional)</span>
          {photoPreviewUrl && (
            <img
              src={photoPreviewUrl}
              alt="Cook photo preview"
              style={{
                maxHeight: 160,
                borderRadius: "var(--radius-input)",
                objectFit: "cover",
              }}
            />
          )}
          <ImageUploader
            state={upload}
            onFileSelected={handlePhoto}
            disabled={upload.phase === "uploading"}
          />
        </div>

        {formError && (
          <p role="alert" style={{ color: "var(--color-error)", margin: 0 }}>
            {formError}
          </p>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--space-2)" }}>
          <Button variant="secondary" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" loading={saving} disabled={upload.phase === "uploading"}>
            {editing ? "Save changes" : "Log cook"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
