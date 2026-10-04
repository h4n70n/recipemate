import { Link } from "react-router-dom";
import { Button } from "../components/ui/Button";
import { EmptyState } from "../components/ui/states";

/** Not-found screen (web-ui-spec §7, 404 handling). */
export function NotFoundScreen() {
  return (
    <main style={{ minHeight: "60vh", display: "grid", placeItems: "center", padding: "var(--space-6)" }}>
      <EmptyState
        icon="🧭"
        title="Page not found"
        message="This page doesn't exist."
        action={
          <Link to="/" style={{ textDecoration: "none" }}>
            <Button variant="secondary">Back to Library</Button>
          </Link>
        }
      />
    </main>
  );
}
