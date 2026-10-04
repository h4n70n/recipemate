import { config } from "../config";
import { fetchAuthSession } from "aws-amplify/auth";

/**
 * Error thrown for any non-2xx API response. `message` is taken from the
 * API's `{"error": "..."}` body when present (see docs/api.md), falling back
 * to the HTTP status text.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Resolve the current Cognito access token, or null when unauthenticated. */
async function getAccessToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.accessToken?.toString() ?? null;
  } catch {
    return null;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** JSON-serializable request body. */
  body?: unknown;
  /** Query string params; undefined/null values are skipped. */
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
}

function appendParam(
  params: URLSearchParams,
  key: string,
  value: string | number | boolean,
): void {
  params.append(key, String(value));
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const base = `${config.apiBaseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  if (!query) return base;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) {
      appendParam(params, key, value);
    }
  }
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

async function parseError(response: Response): Promise<ApiError> {
  let message = response.statusText || `HTTP ${response.status}`;
  try {
    const data = (await response.json()) as { error?: string };
    if (data && typeof data.error === "string") {
      message = data.error;
    }
  } catch {
    // Non-JSON error body — keep the status text.
  }
  return new ApiError(response.status, message);
}

/**
 * Core request helper. Prefixes the configured base URL, injects the Cognito
 * Bearer token, serializes/parses JSON, and throws {@link ApiError} on non-2xx.
 *
 * The generic `T` is the expected success payload. `204 No Content` resolves
 * to `undefined`.
 */
export async function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, query, signal } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  const token = await getAccessToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  let payload: BodyInit | undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const response = await fetch(buildUrl(path, query), {
    method,
    headers,
    body: payload,
    signal,
  });

  if (!response.ok) {
    throw await parseError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  // Some endpoints may legitimately return an empty body on 200/201.
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// ===========================================================================
// Shared types (mirror docs/api.md and app/schemas/*)
// ===========================================================================

/** The five accepted recipe origins (docs/api.md POST /recipes). */
export type RecipeOrigin =
  | "instagram"
  | "web"
  | "cookbook"
  | "manual"
  | "ios_share";

/** Async extraction states (docs/api.md GET .../extraction-status). */
export type ExtractionStatus =
  | "pending"
  | "processing"
  | "complete"
  | "failed";

/** MIME types accepted by POST /recipes/upload. */
export type UploadContentType =
  | "image/jpeg"
  | "image/png"
  | "image/heic"
  | "application/pdf";

/** Sort options for GET /search. */
export type SearchSort =
  | "newest"
  | "oldest"
  | "most_cooked"
  | "highest_rated";

/** Compact recipe projection used in list and search responses. */
export interface RecipeCard {
  id: string;
  title: string;
  thumbnail_url: string | null;
  total_time_min: number | null;
  cook_count: number | null;
  avg_rating: number | null;
  tags: string[];
  created_at: string;
}

export interface RecipeListResponse {
  recipes: RecipeCard[];
  total: number;
}

export interface IngredientDetail {
  id: string;
  name: string;
  quantity: number | null;
  unit: string | null;
  preparation: string | null;
  sort_order: number;
}

export interface ToolDetail {
  id: string;
  name: string;
  sort_order: number;
}

export interface InstructionDetail {
  id: string;
  step_number: number;
  body: string;
}

export interface CookSummary {
  cook_count: number;
  avg_rating: number | null;
}

/** Full recipe object returned by POST / GET {id} / PATCH {id}. */
export interface RecipeDetail {
  id: string;
  title: string;
  description: string | null;
  prep_time_min: number | null;
  cook_time_min: number | null;
  total_time_min: number | null;
  servings: number | null;
  origin: string | null;
  source_url: string | null;
  source_citation: string | null;
  image_url: string | null;
  thumbnail_url: string | null;
  extraction_status: ExtractionStatus | null;
  cook_count: number | null;
  avg_rating: number | null;
  created_at: string;
  updated_at: string;
  ingredients: IngredientDetail[];
  tools: ToolDetail[];
  instructions: InstructionDetail[];
  tags: string[];
  cook_summary: CookSummary;
}

/** Request body for POST /recipes and (all-optional) PATCH /recipes/{id}. */
export interface RecipeWriteBody {
  title?: string;
  description?: string | null;
  prep_time_min?: number | null;
  cook_time_min?: number | null;
  total_time_min?: number | null;
  servings?: number | null;
  origin?: RecipeOrigin | null;
  source_url?: string | null;
  source_citation?: string | null;
}

export interface Tag {
  id: string;
  name: string;
}

export interface TagListResponse {
  tags: Tag[];
}

export interface UploadResponse {
  upload_url: string;
  s3_key: string;
}

export interface ExtractResponse {
  recipe_id: string;
  extraction_status: ExtractionStatus;
}

export interface ExtractionStatusResponse {
  extraction_status: ExtractionStatus;
}

export interface CookLog {
  id: string;
  cooked_at: string;
  notes: string | null;
  rating: number | null;
  photo_url: string | null;
  created_at: string;
}

export interface CookLogListResponse {
  cooks: CookLog[];
}

export interface CookLogCreateBody {
  cooked_at: string;
  notes?: string | null;
  rating?: number | null;
  photo_s3_key?: string | null;
}

/** PATCH cook log — docs/api.md: notes, rating, or photo only (no cooked_at). */
export interface CookLogUpdateBody {
  notes?: string | null;
  rating?: number | null;
  photo_s3_key?: string | null;
}

/** LLM search result card: a RecipeCard plus a match explanation line. */
export interface LlmRecipeCard extends RecipeCard {
  match_explanation: string;
}

export interface LlmSearchResponse {
  recipes: LlmRecipeCard[];
}

export interface SearchParams {
  /** Repeatable; recipes must contain ALL listed ingredients. */
  ingredient?: string[];
  /** Repeatable; recipes must have ALL listed tags. */
  tag?: string[];
  /** Repeatable; recipes must require ALL listed tools. */
  tool?: string[];
  max_time?: number;
  q?: string;
  sort?: SearchSort;
  limit?: number;
  offset?: number;
}

// ===========================================================================
// Recipes
// ===========================================================================

export interface ListRecipesParams {
  limit?: number;
  offset?: number;
}

/** `GET /recipes` — the authenticated user's recipes, paginated. */
export function listRecipes(
  params: ListRecipesParams = {},
  signal?: AbortSignal,
): Promise<RecipeListResponse> {
  return request<RecipeListResponse>("/recipes", {
    query: { limit: params.limit, offset: params.offset },
    signal,
  });
}

/** `GET /recipes/{id}` — the full recipe object. */
export function getRecipe(
  id: string,
  signal?: AbortSignal,
): Promise<RecipeDetail> {
  return request<RecipeDetail>(`/recipes/${encodeURIComponent(id)}`, { signal });
}

/** `POST /recipes` — create a manual-entry recipe. */
export function createRecipe(
  body: RecipeWriteBody,
  signal?: AbortSignal,
): Promise<RecipeDetail> {
  return request<RecipeDetail>("/recipes", { method: "POST", body, signal });
}

/** `PATCH /recipes/{id}` — partial update of recipe-level scalar fields. */
export function updateRecipe(
  id: string,
  body: RecipeWriteBody,
  signal?: AbortSignal,
): Promise<RecipeDetail> {
  return request<RecipeDetail>(`/recipes/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
    signal,
  });
}

/** `DELETE /recipes/{id}` — soft-delete. Resolves on 204. */
export function deleteRecipe(id: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/recipes/${encodeURIComponent(id)}`, {
    method: "DELETE",
    signal,
  });
}

// ===========================================================================
// Image upload / extraction
// ===========================================================================

export interface UploadUrlBody {
  filename: string;
  content_type: UploadContentType;
}

/** `POST /recipes/upload` — get a presigned S3 PUT URL + target key. */
export function uploadUrl(
  body: UploadUrlBody,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return request<UploadResponse>("/recipes/upload", {
    method: "POST",
    body,
    signal,
  });
}

export interface ExtractBody {
  s3_key: string;
  origin?: RecipeOrigin;
  source_url?: string;
}

/** `POST /recipes/extract` — trigger async extraction for an uploaded image. */
export function extract(
  body: ExtractBody,
  signal?: AbortSignal,
): Promise<ExtractResponse> {
  return request<ExtractResponse>("/recipes/extract", {
    method: "POST",
    body,
    signal,
  });
}

/** `GET /recipes/{id}/extraction-status` — poll extraction progress. */
export function extractionStatus(
  id: string,
  signal?: AbortSignal,
): Promise<ExtractionStatusResponse> {
  return request<ExtractionStatusResponse>(
    `/recipes/${encodeURIComponent(id)}/extraction-status`,
    { signal },
  );
}

/**
 * PUT a file straight to a presigned S3 URL. This is a bare `fetch` — the
 * presigned URL carries its own auth signature, so NO Authorization header is
 * sent (adding one would break the signature). The `Content-Type` MUST match
 * the `content_type` declared to `POST /recipes/upload`.
 *
 * Progress is reported via the optional `onProgress` callback (0..1). Because
 * `fetch` has no upload-progress event, this uses `XMLHttpRequest` under the
 * hood when a progress callback is supplied, and plain `fetch` otherwise.
 */
export function putToPresignedUrl(
  uploadUrlValue: string,
  file: Blob,
  contentType: string,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!onProgress) {
    return fetch(uploadUrlValue, {
      method: "PUT",
      headers: { "Content-Type": contentType },
      body: file,
      signal,
    }).then((res) => {
      if (!res.ok) {
        throw new ApiError(res.status, `Upload failed (${res.status})`);
      }
    });
  }

  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrlValue, true);
    xhr.setRequestHeader("Content-Type", contentType);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(new ApiError(xhr.status, `Upload failed (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "Upload failed"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));

    if (signal) {
      if (signal.aborted) {
        xhr.abort();
      } else {
        signal.addEventListener("abort", () => xhr.abort(), { once: true });
      }
    }

    xhr.send(file);
  });
}

// ===========================================================================
// Tags
// ===========================================================================

/**
 * `POST /recipes/{id}/tags` — add a tag to a recipe (idempotent). Returns the
 * tag object. 201 when newly associated, 200 when already present.
 */
export function addTag(
  recipeId: string,
  name: string,
  signal?: AbortSignal,
): Promise<Tag> {
  return request<Tag>(`/recipes/${encodeURIComponent(recipeId)}/tags`, {
    method: "POST",
    body: { name },
    signal,
  });
}

/** `DELETE /recipes/{id}/tags/{tag_id}` — remove a tag association (204). */
export function removeTag(
  recipeId: string,
  tagId: string,
  signal?: AbortSignal,
): Promise<void> {
  return request<void>(
    `/recipes/${encodeURIComponent(recipeId)}/tags/${encodeURIComponent(tagId)}`,
    { method: "DELETE", signal },
  );
}

/** `GET /tags` — the caller's distinct used tags, for autocomplete. */
export function listTags(signal?: AbortSignal): Promise<TagListResponse> {
  return request<TagListResponse>("/tags", { signal });
}

// ===========================================================================
// Cook logs
// ===========================================================================

/** `GET /recipes/{id}/cooks` — cook logs, reverse-chronological. */
export function listCooks(
  recipeId: string,
  signal?: AbortSignal,
): Promise<CookLogListResponse> {
  return request<CookLogListResponse>(
    `/recipes/${encodeURIComponent(recipeId)}/cooks`,
    { signal },
  );
}

/** `POST /recipes/{id}/cooks` — add a cook log (201). */
export function createCook(
  recipeId: string,
  body: CookLogCreateBody,
  signal?: AbortSignal,
): Promise<CookLog> {
  return request<CookLog>(`/recipes/${encodeURIComponent(recipeId)}/cooks`, {
    method: "POST",
    body,
    signal,
  });
}

/** `PATCH /recipes/{id}/cooks/{cook_id}` — update notes, rating, or photo. */
export function updateCook(
  recipeId: string,
  cookId: string,
  body: CookLogUpdateBody,
  signal?: AbortSignal,
): Promise<CookLog> {
  return request<CookLog>(
    `/recipes/${encodeURIComponent(recipeId)}/cooks/${encodeURIComponent(cookId)}`,
    { method: "PATCH", body, signal },
  );
}

/** `DELETE /recipes/{id}/cooks/{cook_id}` — delete a cook log (204). */
export function deleteCook(
  recipeId: string,
  cookId: string,
  signal?: AbortSignal,
): Promise<void> {
  return request<void>(
    `/recipes/${encodeURIComponent(recipeId)}/cooks/${encodeURIComponent(cookId)}`,
    { method: "DELETE", signal },
  );
}

// ===========================================================================
// Search
// ===========================================================================

/**
 * `GET /search` — heuristic filter search. `ingredient`, `tag`, and `tool`
 * are repeatable (AND logic), so they are expanded into multiple query params
 * of the same name rather than passed through the scalar `query` map.
 */
export function search(
  params: SearchParams,
  signal?: AbortSignal,
): Promise<RecipeListResponse> {
  const search = new URLSearchParams();
  for (const value of params.ingredient ?? []) search.append("ingredient", value);
  for (const value of params.tag ?? []) search.append("tag", value);
  for (const value of params.tool ?? []) search.append("tool", value);
  if (params.max_time != null) search.append("max_time", String(params.max_time));
  if (params.q) search.append("q", params.q);
  if (params.sort) search.append("sort", params.sort);
  if (params.limit != null) search.append("limit", String(params.limit));
  if (params.offset != null) search.append("offset", String(params.offset));

  const qs = search.toString();
  return request<RecipeListResponse>(`/search${qs ? `?${qs}` : ""}`, { signal });
}

/** `POST /search/llm` — natural-language search. Falls back server-side. */
export function llmSearch(
  query: string,
  signal?: AbortSignal,
): Promise<LlmSearchResponse> {
  return request<LlmSearchResponse>("/search/llm", {
    method: "POST",
    body: { query },
    signal,
  });
}

export const api = {
  request,
  listRecipes,
  getRecipe,
  createRecipe,
  updateRecipe,
  deleteRecipe,
  uploadUrl,
  extract,
  extractionStatus,
  putToPresignedUrl,
  addTag,
  removeTag,
  listTags,
  listCooks,
  createCook,
  updateCook,
  deleteCook,
  search,
  llmSearch,
};
