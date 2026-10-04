import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";

const navLinkStyle = ({ isActive }: { isActive: boolean }) => ({
  padding: "var(--space-2) var(--space-3)",
  borderRadius: "var(--radius-input)",
  textDecoration: "none",
  fontWeight: 600,
  color: isActive ? "var(--color-primary)" : "var(--color-text-primary)",
  background: isActive ? "rgba(232, 86, 42, 0.08)" : "transparent",
});

/**
 * App shell (web-ui-spec section 4): top nav with Library · Search · Add +,
 * and a user menu with Sign out. Child routes render through <Outlet />.
 */
export function AppShell() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);

  const handleSignOut = async () => {
    setMenuOpen(false);
    await signOut();
    navigate("/signin", { replace: true });
  };

  const label =
    (user && (user.signInDetails?.loginId ?? user.username)) ?? "Account";

  return (
    <div style={{ minHeight: "100vh" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-4)",
          padding: "var(--space-3) var(--space-6)",
          background: "var(--color-surface)",
          borderBottom: "1px solid rgba(31, 27, 22, 0.08)",
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        <NavLink
          to="/"
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 700,
            fontSize: "1.25rem",
            color: "var(--color-primary)",
            textDecoration: "none",
          }}
        >
          RecipeMate
        </NavLink>

        <nav
          aria-label="Primary"
          style={{ display: "flex", gap: "var(--space-1)" }}
        >
          <NavLink to="/" end style={navLinkStyle}>
            Library
          </NavLink>
          <NavLink to="/search" style={navLinkStyle}>
            Search
          </NavLink>
          <NavLink to="/add" style={navLinkStyle}>
            Add +
          </NavLink>
        </nav>

        <div style={{ marginLeft: "auto", position: "relative" }}>
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            style={{
              padding: "var(--space-2) var(--space-3)",
              borderRadius: "var(--radius-pill)",
              border: "1px solid rgba(31, 27, 22, 0.12)",
              background: "var(--color-surface)",
              cursor: "pointer",
            }}
          >
            {label}
          </button>
          {menuOpen && (
            <div
              role="menu"
              style={{
                position: "absolute",
                right: 0,
                marginTop: "var(--space-2)",
                background: "var(--color-surface)",
                borderRadius: "var(--radius-input)",
                boxShadow: "0 4px 16px rgba(31, 27, 22, 0.12)",
                minWidth: 160,
                padding: "var(--space-1)",
              }}
            >
              <button
                type="button"
                role="menuitem"
                onClick={handleSignOut}
                style={{
                  width: "100%",
                  textAlign: "left",
                  padding: "var(--space-2) var(--space-3)",
                  border: "none",
                  background: "transparent",
                  borderRadius: "var(--radius-input)",
                  cursor: "pointer",
                }}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>

      <main style={{ padding: "var(--space-6)" }}>
        <Outlet />
      </main>
    </div>
  );
}
