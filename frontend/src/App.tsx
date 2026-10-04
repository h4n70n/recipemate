import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import { RequireAuth } from "./auth/RequireAuth";
import { AppShell } from "./components/AppShell";
import { ToastProvider } from "./components/ui/Toast";
import { SignIn } from "./screens/SignIn";
import { LibraryScreen } from "./screens/Library";
import { SearchScreen } from "./screens/Search";
import { AddRecipeScreen } from "./screens/AddRecipe";
import { RecipeDetailScreen } from "./screens/RecipeDetail";
import { EditRecipeScreen } from "./screens/EditRecipe";
import { NotFoundScreen } from "./screens/NotFound";

export default function App() {
  return (
    <AuthProvider>
      <ToastProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/signin" element={<SignIn />} />

            {/* Everything under the shell requires an authenticated session. */}
            <Route
              element={
                <RequireAuth>
                  <AppShell />
                </RequireAuth>
              }
            >
              <Route index element={<LibraryScreen />} />
              <Route path="search" element={<SearchScreen />} />
              <Route path="add" element={<AddRecipeScreen />} />
              <Route path="recipes/:id" element={<RecipeDetailScreen />} />
              <Route path="recipes/:id/edit" element={<EditRecipeScreen />} />
            </Route>

            <Route path="/404" element={<NotFoundScreen />} />
            <Route path="*" element={<Navigate to="/404" replace />} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  );
}
