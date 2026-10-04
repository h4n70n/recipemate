import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  createRecipe,
  extract,
  extractionStatus,
  putToPresignedUrl,
  uploadUrl,
  type ExtractionStatus,
  type RecipeOrigin,
  type UploadContentType,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { FormField } from "../components/ui/FormField";
import { inputStyle } from "../components/ui/styles";
import { ImageUploader } from "../components/ui/ImageUploader";
import { validateUploadFile } from "../components/ui/uploadConstraints";
import { ExtractionStatusIndicator } from "../components/ui/ExtractionStatus";
import { fieldErrorMessage } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";
import { useToast } from "../components/ui/Toast";
import { RecipeForm } from "./RecipeForm";
import {
  EMPTY_RECIPE_FORM,
  toWriteBody,
  type RecipeFormValues,
} from "./recipeFormValues";

type Mode = "upload" | "manual";

const ORIGINS: RecipeOrigin[] = ["instagram", "web", "cookbook", "manual", "ios_share"];

const POLL_INTERVAL_MS = 2000;

/** Which step of the guided upload flow we're on. */
type UploadStep =
  | { kind: "pick" }
  | { kind: "uploading"; fraction: number }
  | { kind: "source"; s3Key: string }
  | { kind: "extracting"; recipeId: string; status: ExtractionStatus }
  | { kind: "failed"; recipeId: string };

/**
 * Add recipe (web-ui-spec §5.4 + §5.5). Two modes in one screen:
 *   - Upload photo (primary): pick/drop → presigned PUT (progress) → optional
 *     source → POST /recipes/extract → poll extraction-status with the four
 *     states; on complete route to the recipe's edit/review screen, on failed
 *     offer retry or manual entry.
 *   - Manual entry: POST /recipes with the scalar fields (inline 400
 *     validation), then route to the new recipe's detail screen.
 */
export function AddRecipeScreen() {
  const navigate = useNavigate();
  const toast = useToast();
  const handleError = useApiErrorHandler();

  const [mode, setMode] = useState<Mode>("upload");

  // --- Manual entry state ---
  const [manualSubmitting, setManualSubmitting] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);

  const handleManualSubmit = async (values: RecipeFormValues) => {
    setManualSubmitting(true);
    setManualError(null);
    try {
      const recipe = await createRecipe(toWriteBody(values));
      toast.show("Recipe created.", "success");
      navigate(`/recipes/${recipe.id}`);
    } catch (err) {
      const fieldMsg = fieldErrorMessage(err);
      setManualError(fieldMsg ?? handleError(err));
      setManualSubmitting(false);
    }
  };

  return (
    <section style={{ maxWidth: 720, margin: "0 auto" }}>
      <h1>Add recipe</h1>

      <div role="tablist" aria-label="Add recipe mode" style={{ display: "flex", gap: "var(--space-2)", marginBottom: "var(--space-6)" }}>
        <Button
          variant={mode === "upload" ? "primary" : "secondary"}
          role="tab"
          aria-selected={mode === "upload"}
          onClick={() => setMode("upload")}
        >
          Upload a photo
        </Button>
        <Button
          variant={mode === "manual" ? "primary" : "secondary"}
          role="tab"
          aria-selected={mode === "manual"}
          onClick={() => setMode("manual")}
        >
          Enter manually
        </Button>
      </div>

      {mode === "upload" ? (
        <UploadFlow onSwitchToManual={() => setMode("manual")} />
      ) : (
        <>
          <p style={{ color: "var(--color-text-secondary)" }}>
            Enter the recipe details. Ingredients, tools, instructions, and tags
            are managed on the recipe page after it's created.
          </p>
          <RecipeForm
            initial={EMPTY_RECIPE_FORM}
            submitLabel="Create recipe"
            submitting={manualSubmitting}
            formError={manualError}
            onSubmit={handleManualSubmit}
          />
        </>
      )}
    </section>
  );
}

function UploadFlow({ onSwitchToManual }: { onSwitchToManual: () => void }) {
  const navigate = useNavigate();
  const toast = useToast();
  const handleError = useApiErrorHandler();

  const [step, setStep] = useState<UploadStep>({ kind: "pick" });
  const [origin, setOrigin] = useState<RecipeOrigin | "">("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  // Clear any pending poll timer on unmount.
  useEffect(() => {
    return () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    };
  }, []);

  const handleFile = async (file: File) => {
    const validationError = validateUploadFile(file);
    if (validationError) {
      setActionError(validationError);
      return;
    }
    setActionError(null);
    setStep({ kind: "uploading", fraction: 0 });
    try {
      const { upload_url, s3_key } = await uploadUrl({
        filename: file.name,
        content_type: file.type as UploadContentType,
      });
      await putToPresignedUrl(upload_url, file, file.type, (fraction) =>
        setStep({ kind: "uploading", fraction }),
      );
      setStep({ kind: "source", s3Key: s3_key });
    } catch (err) {
      setActionError(handleError(err));
      setStep({ kind: "pick" });
    }
  };

  const beginExtraction = async (s3Key: string) => {
    setActionError(null);
    try {
      const res = await extract({
        s3_key: s3Key,
        origin: origin || undefined,
        source_url: sourceUrl.trim() || undefined,
      });
      setStep({ kind: "extracting", recipeId: res.recipe_id, status: res.extraction_status });
      schedulePoll(res.recipe_id);
    } catch (err) {
      const fieldMsg = fieldErrorMessage(err);
      setActionError(fieldMsg ?? handleError(err));
    }
  };

  const schedulePoll = (recipeId: string) => {
    pollRef.current = window.setTimeout(async () => {
      try {
        const { extraction_status } = await extractionStatus(recipeId);
        if (extraction_status === "complete") {
          toast.show("Recipe extracted — review it below.", "success");
          // Route to the recipe in edit/review mode so the user confirms.
          navigate(`/recipes/${recipeId}/edit`);
          return;
        }
        if (extraction_status === "failed") {
          setStep({ kind: "failed", recipeId });
          return;
        }
        setStep({ kind: "extracting", recipeId, status: extraction_status });
        schedulePoll(recipeId);
      } catch (err) {
        setActionError(handleError(err));
      }
    }, POLL_INTERVAL_MS);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      {(step.kind === "pick" || step.kind === "uploading") && (
        <ImageUploader
          state={
            step.kind === "uploading"
              ? { phase: "uploading", fraction: step.fraction }
              : { phase: "idle" }
          }
          onFileSelected={handleFile}
          disabled={step.kind === "uploading"}
        />
      )}

      {step.kind === "source" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", maxWidth: 520 }}>
          <p style={{ margin: 0, color: "var(--color-success)", fontWeight: 600 }}>
            ✓ Photo uploaded.
          </p>
          <p style={{ margin: 0, color: "var(--color-text-secondary)" }}>
            Add an optional source, then start extraction.
          </p>
          <FormField label="Origin">
            {(props) => (
              <select
                {...props}
                value={origin}
                onChange={(e) => setOrigin(e.target.value as RecipeOrigin | "")}
                style={inputStyle}
              >
                <option value="">—</option>
                {ORIGINS.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField label="Source URL">
            {(props) => (
              <input
                {...props}
                type="url"
                value={sourceUrl}
                onChange={(e) => setSourceUrl(e.target.value)}
                style={inputStyle}
              />
            )}
          </FormField>
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            <Button onClick={() => beginExtraction(step.s3Key)}>Start extraction</Button>
            <Button variant="secondary" onClick={() => setStep({ kind: "pick" })}>
              Choose a different photo
            </Button>
          </div>
        </div>
      )}

      {step.kind === "extracting" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", alignItems: "flex-start" }}>
          <ExtractionStatusIndicator status={step.status} />
          <p style={{ color: "var(--color-text-secondary)", margin: 0 }}>
            This can take a moment. We'll take you to the recipe when it's ready.
          </p>
        </div>
      )}

      {step.kind === "failed" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", alignItems: "flex-start" }}>
          <ExtractionStatusIndicator status="failed" />
          <p style={{ color: "var(--color-text-secondary)", margin: 0 }}>
            We couldn't read that image. Try another photo, or enter the recipe
            manually.
          </p>
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            <Button onClick={() => setStep({ kind: "pick" })}>Try another photo</Button>
            <Button variant="secondary" onClick={onSwitchToManual}>
              Enter manually
            </Button>
          </div>
        </div>
      )}

      {actionError && (
        <p role="alert" style={{ color: "var(--color-error)", margin: 0 }}>
          {actionError}
        </p>
      )}
    </div>
  );
}
